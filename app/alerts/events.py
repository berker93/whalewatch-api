"""What happened in a set of ``(filer, period)`` pairs: the events alerts are about.

Read from the derived tables as they are when the task runs, never from what
the rebuild that named the pairs saw: the task runs later, and a second rebuild
may have run in between. Two kinds of event come out of a pair:

``filer_filed``
    The pair is published: the filer's 13F for the quarter is in the
    portfolio. Once per pair, ever, by its dedupe key, so a restatement of a
    quarter already told about is nothing new.
``filer_position_change`` and ``stock_ownership_change``
    Each position the filer opened, added to, trimmed or exited in the pair:
    the same change, told to those following the filer and those following
    the stock. A ``hold`` is not news, and nor is a filer's first period, in
    which every position is ``new`` because it is the first we have.

What is never an event
----------------------
**A suspect filing.** A pair counts only while it is published and not marked
suspect, so a period withheld for a suspect filing, and one published with
``--include-suspect``, are neither. Nor is a change whose previous period is
on the far side of a withheld one: when Q3 is withheld, Q4's changes are
against Q2, and the difference is the suspect filing's.

**A split.** The changes are split-adjusted (``position_change``), so a holder
who held through a 10-for-1 is a ``hold``, and one who bought on top of it an
``add`` of what they bought, in today's shares. That needs the split to have
been detected, which takes two holders' filings
(:data:`~app.derived.security_split.MIN_AGREEING`). Until then, the first
holder to file the quarter is a 900% ``add``. So an ``add`` or ``trim`` that
is not adjusted and looks like a split to
:func:`~app.derived.security_split.looks_like_split` is not an event. When the
split is detected, ``recompute`` rebuilds every change across it and names
those pairs too, and the adjusted change is told then, if it is news at all.

**Unheard.** Only the filers and stocks some rule that is on is about. A
season is a few hundred thousand changes, and a few hundred are followed.

**Old news.** Only pairs whose latest filing was filed in the last
:data:`FRESH_FOR`. ``recompute --all`` rebuilds five years of pairs, and a
filing from 2022 is not an alert in 2026, whoever has not been told about it.

The dollars
-----------
A change is the dollars it traded, as the flows count them
(:func:`app.derived.views.traded_usd`): the shares bought or sold at the
period-end price the filer reported, or for an exit, the price it last
reported. It is what a rule's ``min_value`` is compared with, and what the
activity view shows beside it. The implied price, the median of every
holder's, is said too, but only when it is ``high`` or ``medium``
confidence: a ``low`` one may be one filer's mistake, and shown bare it
would read as a fact. Its confidence is in the payload either way.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.periods import quarter_label
from app.db.models import (
    PAYLOAD_KEYS,
    AlertKind,
    AlertRule,
    Filer,
    Filing,
    ImpliedPrice,
    PositionChange,
    PositionSnapshot,
    Security,
)
from app.db.models.filing import ParseStatus
from app.db.models.implied_price import PriceConfidence
from app.db.models.position_change import ChangeAction
from app.db.queries.tickers import ticker_at
from app.derived.scope import Pair, Scope
from app.derived.security_split import looks_like_split

#: How recently a pair's latest filing must have been filed to be news.
#: Longer than a 13F season, so that a change rebuilt when a split is detected
#: at the deadline, from a filing made early in the season, is still told.
FRESH_FOR: Final = timedelta(days=30)

#: The implied prices worth saying. A ``low`` one is left out of the words.
SAID_CONFIDENCES: Final = frozenset({PriceConfidence.HIGH, PriceConfidence.MEDIUM})

#: The day a filing was filed on is EDGAR's day.
EDGAR_TIME: Final = ZoneInfo("America/New_York")

_VERBS: Final = {
    ChangeAction.NEW: "opened a position in",
    ChangeAction.ADD: "added to",
    ChangeAction.TRIM: "trimmed",
    ChangeAction.EXIT: "exited",
}

#: How the filing's summary counts each action: "opened 3, added to 5".
_COUNTED: Final = {
    ChangeAction.NEW: "opened",
    ChangeAction.ADD: "added to",
    ChangeAction.TRIM: "trimmed",
    ChangeAction.EXIT: "exited",
}


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, as every rule that asks for it is told it."""

    kind: AlertKind
    filer_id: int
    period: date
    security_id: int | None
    """The stock, for the kinds about one; ``None`` for a filing."""
    filed_at: datetime
    """When the filing that made it news was filed: a rule made after it was
    not asking yet."""
    dollars: Decimal | None
    """What a rule's ``min_value`` is compared with, unsigned: the dollars a
    change traded. ``None`` for a filing, which has no threshold."""
    change: tuple[int, date, int] | None
    """``(filer, period, security)`` for a change: one change is an event of
    each of two kinds, and a user following both ends is told it once."""
    dedupe_key: str
    title: str
    body: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Published:
    filer_id: int
    period: date
    slug: str
    filer_name: str
    filed_at: datetime
    positions: int
    value_usd: Decimal


async def read_events(
    session: AsyncSession, pairs: frozenset[Pair] | None, *, now: datetime
) -> list[Event]:
    """Every event in ``pairs``, or in every pair filed for since :data:`FRESH_FOR`
    before ``now`` when ``None``, about a filer or a stock a rule that is on
    follows. The filings first, then the changes."""
    if pairs is not None and not pairs:
        return []
    filers, securities = await _followed(session)
    if not filers and not securities:
        return []
    published = await _published(session, pairs, since=now - FRESH_FOR)
    if not published:
        return []
    rows = (await session.execute(_changes(published, filers, securities))).all()
    changes = [row for row in rows if not _split_unadjusted(row)]

    # Every change of a followed filer is read, so its filing counts them all.
    counted: dict[Pair, Counter[str]] = {key: Counter() for key in published}
    for row in changes:
        counted[(row.filer_id, row.period_of_report)][row.action] += 1
    events = [
        _filed(pair, counted[key])
        for key, pair in sorted(published.items())
        if pair.filer_id in filers
    ]
    for row in changes:
        events.extend(_changed(published[(row.filer_id, row.period_of_report)], row))
    return events


async def _followed(session: AsyncSession) -> tuple[frozenset[int], frozenset[int]]:
    """The filers, and the stocks, some rule that is on is about."""
    rows = await session.execute(
        select(AlertRule.filer_id, AlertRule.security_id)
        .where(AlertRule.is_active.is_(true()))
        .distinct()
    )
    filers: set[int] = set()
    securities: set[int] = set()
    for filer_id, security_id in rows.tuples():
        if filer_id is not None:
            filers.add(filer_id)
        if security_id is not None:
            securities.add(security_id)
    return frozenset(filers), frozenset(securities)


async def _published(
    session: AsyncSession, pairs: frozenset[Pair] | None, *, since: datetime
) -> dict[Pair, _Published]:
    """The pairs that are published, not suspect, and filed for since ``since``.

    Read from the snapshot, whose rows each name the latest filing behind them,
    so the latest of those is when the period last changed. A pair with no
    rows is not published, and a pair with a suspect filing that counts has
    none either, unless ``--include-suspect`` marked them.
    """
    snapshot = PositionSnapshot
    in_pairs: list[ColumnElement[bool]]
    if pairs is None:
        recent = (
            select(Filing.filer_id, Filing.period_of_report)
            .where(Filing.filed_at >= since, Filing.filer_id.is_not(None))
            .distinct()
        )
        in_pairs = [tuple_(snapshot.filer_id, snapshot.period_of_report).in_(recent)]
    else:
        in_pairs = Scope.of(pairs).covers(snapshot.filer_id, snapshot.period_of_report)
    filed = (
        select(
            snapshot.filer_id,
            snapshot.period_of_report,
            func.max(Filing.filed_at).label("filed_at"),
            func.count().label("positions"),
            func.sum(snapshot.value_usd).label("value_usd"),
        )
        .join(Filing, Filing.id == snapshot.source_filing_id)
        .where(*in_pairs)
        .group_by(snapshot.filer_id, snapshot.period_of_report)
        .having(func.max(Filing.filed_at) >= since, ~func.bool_or(snapshot.suspect))
        .subquery("filed")
    )
    rows = await session.execute(
        select(filed, Filer.slug, func.coalesce(Filer.display_name, Filer.name)).join(
            Filer, Filer.id == filed.c.filer_id
        )
    )
    return {
        (filer_id, period): _Published(filer_id, period, slug, name, filed_at, positions, value_usd)
        for filer_id, period, filed_at, positions, value_usd, slug, name in rows.tuples()
    }


def _changes(
    published: dict[Pair, _Published], filers: frozenset[int], securities: frozenset[int]
) -> Select[Any]:
    """Every change worth telling in the published pairs, by ``filers`` or in
    ``securities``, with its stock and implied price: no holds, no first
    periods, nothing suspect, nothing across a period withheld for a suspect
    filing."""
    change = PositionChange
    withheld_between = exists(
        select(Filing.id).where(
            Filing.filer_id == change.filer_id,
            Filing.period_of_report > change.prev_period_of_report,
            Filing.period_of_report < change.period_of_report,
            Filing.parse_status == ParseStatus.SUSPECT.value,
        )
    )
    return (
        select(
            change.filer_id,
            change.period_of_report,
            change.security_id,
            change.action,
            change.shares,
            change.value_usd,
            change.prev_shares,
            change.prev_value_usd,
            change.shares_delta,
            change.shares_delta_pct,
            change.is_split,
            change.split_ratio,
            Security.cusip,
            Security.name.label("issuer"),
            ticker_at(change.period_of_report).label("ticker"),
            ImpliedPrice.price.label("implied_price"),
            ImpliedPrice.confidence.label("price_confidence"),
        )
        .join(Security, Security.id == change.security_id)
        .outerjoin(
            ImpliedPrice,
            and_(
                ImpliedPrice.security_id == change.security_id,
                ImpliedPrice.period_of_report == change.period_of_report,
            ),
        )
        .where(
            *Scope.of(published).covers(change.filer_id, change.period_of_report),
            or_(change.filer_id.in_(sorted(filers)), change.security_id.in_(sorted(securities))),
            change.action != ChangeAction.HOLD.value,
            change.suspect.is_not(true()),
            change.prev_period_of_report.is_not(None),
            ~withheld_between,
        )
        .order_by(change.filer_id, change.period_of_report, change.security_id)
    )


def _split_unadjusted(row: Any) -> bool:
    """An ``add`` or ``trim`` not taken across a split that looks like one."""
    if row.is_split or row.action not in (ChangeAction.ADD, ChangeAction.TRIM):
        return False
    price_ratio = None
    if row.shares > 0 and row.prev_shares > 0 and row.value_usd > 0 and row.prev_value_usd > 0:
        price_ratio = (row.value_usd / row.shares) / (row.prev_value_usd / row.prev_shares)
    return looks_like_split(row.prev_shares, row.shares, price_ratio)


def _traded(row: Any) -> Decimal:
    """:func:`app.derived.views.traded_usd`, signed: positive bought, negative sold."""
    shares: Decimal = abs(row.shares_delta)
    if row.shares > 0:
        traded: Decimal = shares * row.value_usd / row.shares
    elif row.prev_shares:
        traded = shares * row.prev_value_usd / row.prev_shares
    else:
        traded = Decimal(0)
    return traded.copy_sign(row.shares_delta) if traded else traded


def _filed(pair: _Published, counted: Counter[str]) -> Event:
    period = quarter_label(pair.period)
    positions = f"{_number(pair.positions)} {_plural(pair.positions, 'position')}"
    body = f"{positions} worth {dollars(pair.value_usd)}."
    done = [f"{_COUNTED[action]} {counted[action]}" for action in _COUNTED if counted[action]]
    if done:
        body += f" {_sentence(done)}."
    return Event(
        kind=AlertKind.FILER_FILED,
        filer_id=pair.filer_id,
        period=pair.period,
        security_id=None,
        filed_at=pair.filed_at,
        dollars=None,
        change=None,
        dedupe_key=f"{AlertKind.FILER_FILED}:{pair.slug}:{period}",
        title=f"{pair.filer_name} filed its 13F for {period}",
        body=body,
        payload=_payload(
            filer_slug=pair.slug,
            period=period,
            as_of=_filed_on(pair.filed_at),
            positions=pair.positions,
            value_usd=f"{pair.value_usd:.2f}",
        ),
    )


def _changed(pair: _Published, row: Any) -> Iterable[Event]:
    """The change as each of its two kinds: about the filer, and about the stock."""
    action = ChangeAction(row.action)
    period = quarter_label(pair.period)
    stock = row.ticker or row.issuer or row.cusip
    traded = _traded(row)
    said = row.price_confidence in SAID_CONFIDENCES and row.implied_price is not None

    if action is ChangeAction.NEW:
        what = f"{_number(row.shares)} shares"
    elif action is ChangeAction.EXIT:
        what = f"All {_number(row.prev_shares)} shares"
    else:
        what = f"{row.shares_delta:+,.0f} shares"
        if row.shares_delta_pct is not None:
            what += f" ({row.shares_delta_pct:+.1f}%)"
    price = "its last reported price" if action is ChangeAction.EXIT else f"the {period} price"
    body = f"{what}, about {dollars(abs(traded))} at {price}."
    if row.is_split:
        body += f" Counted in shares after {stock}'s {_split(row.split_ratio)} split."
    if said:
        body += (
            f" {stock}'s implied price at the quarter end: ${row.implied_price:,.2f} a share "
            f"({row.price_confidence} confidence)."
        )
    payload = _payload(
        ticker=row.ticker,
        filer_slug=pair.slug,
        period=period,
        delta=f"{traded:.2f}",
        confidence=row.price_confidence,
        as_of=_filed_on(pair.filed_at),
        cusip=row.cusip,
        action=action.value,
        shares_delta=_plain(row.shares_delta),
        shares_delta_pct=None if row.shares_delta_pct is None else f"{row.shares_delta_pct:.2f}",
        split_ratio=None if row.split_ratio is None else _plain(row.split_ratio),
        implied_price=_plain(row.implied_price) if said else None,
    )
    title = f"{pair.filer_name} {_VERBS[action]} {stock}"
    common = {
        "filer_id": pair.filer_id,
        "period": pair.period,
        "security_id": row.security_id,
        "filed_at": pair.filed_at,
        "dollars": abs(traded),
        "change": (pair.filer_id, pair.period, row.security_id),
        "title": title,
        "body": body,
        "payload": payload,
    }
    yield Event(
        kind=AlertKind.FILER_POSITION_CHANGE,
        dedupe_key=f"{AlertKind.FILER_POSITION_CHANGE}:{pair.slug}:{period}:{row.cusip}",
        **common,
    )
    yield Event(
        kind=AlertKind.STOCK_OWNERSHIP_CHANGE,
        dedupe_key=f"{AlertKind.STOCK_OWNERSHIP_CHANGE}:{row.cusip}:{period}:{pair.slug}",
        **common,
    )


def _payload(**values: Any) -> dict[str, Any]:
    """Every key of :data:`~app.db.models.PAYLOAD_KEYS`, null unless given, and the kind's own."""
    return dict.fromkeys(PAYLOAD_KEYS) | values


def _plain(number: Decimal) -> str:
    """``500``, ``10.5``: the column's places without the trailing zeros, never
    an exponent."""
    return f"{number.normalize():f}"


def _filed_on(filed_at: datetime) -> str:
    return filed_at.astimezone(EDGAR_TIME).date().isoformat()


def dollars(amount: Decimal) -> str:
    """``$9,450``, ``$230.4M``, ``$1.2B``: an estimate, said as one."""
    for size, suffix in ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M")):
        if amount >= size:
            return f"${amount / size:,.1f}{suffix}"
    return f"${amount:,.0f}"


def _number(shares: Decimal | int) -> str:
    return f"{shares:,.0f}"


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def _sentence(parts: list[str]) -> str:
    """``Opened 3, added to 5 and exited 1``."""
    text = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return text[0].upper() + text[1:]


def _split(ratio: Decimal) -> str:
    """``10-for-1``, ``1-for-10``: the stored ratio, as the split is said."""
    fraction = Fraction(ratio).limit_denominator(1000)
    return f"{fraction.numerator}-for-{fraction.denominator}"
