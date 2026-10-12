"""Writing the ticker history: new tickers, renames, delistings, and former tickers by hand.

``security_alias`` holds every ticker a security has traded under, each over a
half-open range of dates (:mod:`app.db.models.security_alias`). Three things
change it:

* **A new ticker.** :func:`record_tickers`, from ``resolve-cusips`` and
  ``resolve-cusip``. A security's first ticker opens a range with no start: as
  far back as we know, it traded as that. A different ticker later is a rename.
  The old range ends today and the new one starts today, since today is when
  we saw it. The real date of the rename is earlier, and ``ticker-alias``
  corrects it.
* **A delisting.** :func:`flag_delistings`, from the SEC's 13(f) lists, after
  every ``resolve-cusips``, ``resolve-cusip`` and ``import-13f-list``. A
  security on some list but not on the latest one gets ``delisted_by``, and its
  open range ends there. That is what keeps a recycled ticker from resolving to
  the company that gave it up.
* **A former ticker.** :func:`add_ticker_history`, from ``ticker-alias``. No
  source we read has history (OpenFIGI answers with today's ticker), so
  ``FB`` before 2022-06-09 is entered by hand. The ranges it overlaps are
  trimmed to make room.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.security import ResolutionSource, Security
from app.db.models.security_alias import SecurityAlias


class UnknownSecurityError(LookupError):
    """A CUSIP no filing has reported: there is no security to change."""


@dataclass(frozen=True, slots=True)
class TickerChange:
    """A security's ticker, newly set: its first, or a different one."""

    security_id: int
    ticker: str
    source: ResolutionSource


# --- new tickers --------------------------------------------------------------------

#: The changes as parallel arrays, and the day they take effect.
_CHANGES: Final = """
    unnest(CAST(:ids AS bigint[]), CAST(:tickers AS text[]), CAST(:sources AS text[]))
        AS v(id, ticker, source)
"""
_ON: Final = "coalesce(CAST(:on AS date), current_date)"

#: A range opened today and replaced today was never really valid: it takes
#: the new ticker instead of ending the day it began.
_REPLACE_TODAYS: Final = text(
    f"""
    UPDATE security_alias a
    SET ticker = v.ticker, source = v.source
    FROM {_CHANGES}
    WHERE a.security_id = v.id
      AND a.valid_to IS NULL
      AND a.valid_from >= {_ON}
      AND upper(a.ticker) <> upper(v.ticker)
    """
)
_CLOSE_RENAMED: Final = text(
    f"""
    UPDATE security_alias a
    SET valid_to = {_ON}
    FROM {_CHANGES}
    WHERE a.security_id = v.id
      AND a.valid_to IS NULL
      AND upper(a.ticker) <> upper(v.ticker)
      AND (a.valid_from IS NULL OR a.valid_from < {_ON})
    RETURNING a.security_id
    """
)
#: Starts where the security's history ends, or from the beginning of time if
#: it has none.
_OPEN_NEW: Final = text(
    f"""
    INSERT INTO security_alias (security_id, ticker, source, valid_from)
    SELECT v.id, v.ticker, v.source,
           (SELECT max(x.valid_to) FROM security_alias x WHERE x.security_id = v.id)
    FROM {_CHANGES}
    WHERE NOT EXISTS (
        SELECT 1 FROM security_alias o
        WHERE o.security_id = v.id AND o.valid_to IS NULL AND upper(o.ticker) = upper(v.ticker)
    )
    RETURNING security_id
    """
)


@dataclass(frozen=True, slots=True)
class Recorded:
    opened: int
    """Ranges opened: first tickers and renames."""
    renamed: int
    """Of those, renames: a range for another ticker was ended."""


async def record_tickers(
    session: AsyncSession, changes: Sequence[TickerChange], *, on: date | None = None
) -> Recorded:
    """Put each change in the history: end the old ticker's range, open the new one's.

    Only for a ticker that is new to its security. The caller knows which
    those are, and calling this for an unchanged one would reopen a range a
    delisting had ended. ``on`` defaults to today, in the database's clock.
    """
    if not changes:
        return Recorded(0, 0)
    params = {
        "ids": [c.security_id for c in changes],
        "tickers": [c.ticker for c in changes],
        "sources": [c.source.value for c in changes],
        "on": on,
    }
    await session.execute(_REPLACE_TODAYS, params)
    renamed = len((await session.execute(_CLOSE_RENAMED, params)).all())
    opened = len((await session.execute(_OPEN_NEW, params)).all())
    return Recorded(opened, renamed)


# --- delistings ---------------------------------------------------------------------

#: Every security any 13(f) list has had as listed or added: on the latest list,
#: not delisted; otherwise delisted by the first list after its last listing.
#: A security no list has had is left as it is.
_FLAG: Final = text(
    """
    WITH lists AS (
        SELECT DISTINCT period_of_report AS period FROM sec_13f_security
    ),
    last_listed AS (
        SELECT cusip, max(period_of_report) AS period
        FROM sec_13f_security
        WHERE status <> 'deleted'
        GROUP BY cusip
    ),
    flag AS (
        SELECT s.id,
               (SELECT min(l.period) FROM lists l WHERE l.period > ll.period) AS delisted_by
        FROM security s
        JOIN last_listed ll ON ll.cusip = s.cusip
    )
    UPDATE security s
    SET delisted_by = f.delisted_by
    FROM flag f
    JOIN security old ON old.id = f.id
    WHERE s.id = f.id
      AND s.delisted_by IS DISTINCT FROM f.delisted_by
    RETURNING s.id, old.delisted_by AS was, s.delisted_by AS now
    """
)
#: A relisted security's last range, ended by its delisting, runs on again.
_REOPEN: Final = text(
    """
    UPDATE security_alias a
    SET valid_to = NULL
    FROM unnest(CAST(:ids AS bigint[]), CAST(:ended AS date[])) AS r(id, ended)
    WHERE a.security_id = r.id
      AND a.valid_to = r.ended
      AND NOT EXISTS (
          SELECT 1 FROM security_alias later
          WHERE later.security_id = a.security_id AND later.valid_from >= r.ended
      )
    """
)
#: Idempotent: every open range of every delisted security ends at its
#: delisting, however it came to be open.
_CLOSE_DELISTED: Final = text(
    """
    UPDATE security_alias a
    SET valid_to = s.delisted_by
    FROM security s
    WHERE s.id = a.security_id
      AND s.delisted_by IS NOT NULL
      AND a.valid_to IS NULL
      AND (a.valid_from IS NULL OR a.valid_from < s.delisted_by)
    RETURNING a.id
    """
)


#: When a delisted security's range for a ticker ends, another security whose
#: range for the same ticker has no known start takes it over from then. A null
#: start only says "as far back as we know"; the delisting is better evidence
#: of when the second security started trading under it. This is a CUSIP
#: change (a reverse split, a reorganisation) or a recycled ticker, and either
#: way the ranges stop overlapping.
#:
#: From the *latest* such ending before the range's own end, so a chain of
#: CUSIP changes (three reverse splits, three CUSIPs) hands the ticker along
#: the chain rather than to every later link at once. A start a hand-over set
#: (one that is some delisted range's end) is revisited, since a later
#: delisting can move it on; a start a person entered is not.
_HAND_OVER: Final = text(
    """
    WITH ended AS (
        SELECT a.security_id, upper(a.ticker) AS ticker, a.valid_to AS ended
        FROM security_alias a
        JOIN security s ON s.id = a.security_id AND s.delisted_by = a.valid_to
    ),
    handover AS (
        SELECT b.id, max(e.ended) AS valid_from
        FROM security_alias b
        JOIN ended e
          ON e.ticker = upper(b.ticker)
         AND e.security_id <> b.security_id
         AND e.ended < coalesce(b.valid_to, 'infinity'::date)
        WHERE b.valid_from IS NULL
           OR EXISTS (
               SELECT 1 FROM ended x
               WHERE x.ticker = upper(b.ticker)
                 AND x.security_id <> b.security_id
                 AND x.ended = b.valid_from
           )
        GROUP BY b.id
    )
    UPDATE security_alias b
    SET valid_from = h.valid_from
    FROM handover h
    WHERE b.id = h.id
      AND b.valid_from IS DISTINCT FROM h.valid_from
    RETURNING b.id
    """
)


@dataclass(frozen=True, slots=True)
class Delistings:
    flagged: int
    """Securities newly found delisted."""
    relisted: int
    """Securities back on the latest list."""
    ranges_closed: int
    handed_over: int
    """Ranges of another security for a delisted one's ticker, now starting
    where the delisted one's ends (:data:`_HAND_OVER`)."""


async def flag_delistings(session: AsyncSession) -> Delistings:
    """Set ``security.delisted_by`` from the imported 13(f) lists, end the
    open ticker range of every delisted security there, and hand its ticker
    over to any other security holding it from an unknown start.

    Cheap enough to run whole after anything that changes tickers or lists:
    one pass over ``security`` and the list's (cusip, period) index.
    """
    changed = (await session.execute(_FLAG)).all()
    relisted = [row for row in changed if row.now is None]
    if relisted:
        await session.execute(
            _REOPEN, {"ids": [r.id for r in relisted], "ended": [r.was for r in relisted]}
        )
    closed = len((await session.execute(_CLOSE_DELISTED)).all())
    handed_over = len((await session.execute(_HAND_OVER)).all())
    return Delistings(len(changed) - len(relisted), len(relisted), closed, handed_over)


# --- former tickers, by hand -------------------------------------------------------


class TickerHistoryConflictError(ValueError):
    """A former ticker that cannot be fitted into the history by trimming."""


@dataclass(frozen=True, slots=True)
class Trimmed:
    ticker: str
    was: tuple[date | None, date | None]
    now: tuple[date | None, date | None]


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    cusip: str
    name: str | None
    current_ticker: str | None
    trimmed: tuple[Trimmed, ...]


async def add_ticker_history(
    session: AsyncSession,
    cusip: str,
    ticker: str,
    *,
    valid_from: date | None,
    valid_to: date,
    note: str | None,
) -> HistoryEntry:
    """Record that ``cusip`` traded as ``ticker`` over ``[valid_from, valid_to)``.

    A range that overlaps one end of the new one is trimmed back to meet it.
    That is the usual case: ``META`` runs from the beginning of time until
    ``FB`` is entered up to 2022-06-09, and then ``META`` starts there. So is a
    range with no known start, whatever the new one's: its start was only "as
    far back as we know". A range the new one would swallow whole, or would cut
    in two, is refused, because which of the two is wrong is a judgement this
    should not make.

    :raises UnknownSecurityError: No security has the CUSIP.
    :raises TickerHistoryConflictError: As above.
    """
    security = (
        await session.execute(
            select(Security.id, Security.name, Security.ticker)
            .where(Security.cusip == cusip)
            .with_for_update()
        )
    ).one_or_none()
    if security is None:
        raise UnknownSecurityError(cusip)

    alias = SecurityAlias
    rows = (
        await session.execute(
            select(alias.id, alias.ticker, alias.valid_from, alias.valid_to).where(
                alias.security_id == security.id
            )
        )
    ).all()
    start, end = _bound(valid_from, low=True), _bound(valid_to, low=False)
    trimmed: list[Trimmed] = []
    for row in rows:
        row_start, row_end = _bound(row.valid_from, low=True), _bound(row.valid_to, low=False)
        if not (row_start < end and start < row_end):
            continue
        span = _span(row.valid_from, row.valid_to)
        if start <= row_start and row_end <= end:
            raise TickerHistoryConflictError(f"{row.ticker} {span} lies wholly inside it")
        # An unknown start is not evidence: what is entered now replaces it.
        unknown_start = row.valid_from is None and end < row_end
        if row_start < start and end < row_end and not unknown_start:
            raise TickerHistoryConflictError(f"it would cut {row.ticker} {span} in two")
        new = (
            (row.valid_from, valid_from)
            if row_start < start and not unknown_start
            else (valid_to, row.valid_to)
        )
        await session.execute(
            update(alias).where(alias.id == row.id).values(valid_from=new[0], valid_to=new[1])
        )
        trimmed.append(Trimmed(row.ticker, (row.valid_from, row.valid_to), new))

    await session.execute(
        insert(alias).values(
            security_id=security.id,
            ticker=ticker,
            valid_from=valid_from,
            valid_to=valid_to,
            source=ResolutionSource.MANUAL.value,
            note=note,
        )
    )
    return HistoryEntry(cusip, security.name, security.ticker, tuple(trimmed))


def _bound(day: date | None, *, low: bool) -> date:
    if day is not None:
        return day
    return date.min if low else date.max


def _span(start: date | None, end: date | None) -> str:
    return f"[{start or '…'}, {end or '…'})"
