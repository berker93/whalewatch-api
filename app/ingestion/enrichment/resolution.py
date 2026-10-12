"""CUSIP resolution: giving each ``security`` a ticker, a FIGI and a name.

Resolution is the gate on the whole UI. Every aggregate ranks securities, and a
security with no ticker is still counted but cannot be found by one. Below
roughly 95% resolved by value, the rankings are quietly incomplete and nothing
says so. ``cusip-coverage`` (:mod:`app.db.queries.cusip_coverage`) is what says
so.

::

    async with OpenFIGIClient(settings, DatabaseResponseLog(sessions)) as figi:
        report = await resolve_securities(sessions, figi, limit=200)

By value, highest first
-----------------------
The candidates are taken in descending order of their aggregate holdings value:
``SUM(holding.value_usd)``, every filing's every line. Value is concentrated.
On the five-year dev backfill, 90 of 20,766 securities are half the value and
1,681 are 95% of it. So ``--limit 200`` is most of the way, and resolving in any
other order would take a long time to get anywhere useful.

The order of sources
--------------------
1. **OpenFIGI.** A ticker only from an equity listing on a US venue
   (:func:`choose_listing`). The FIGI is the US composite's. The name is
   OpenFIGI's.
2. **The SEC's 13(f) list**, for what OpenFIGI could not give a ticker. A name
   only: the list has no tickers. The security stays unresolved, under a name
   the SEC wrote rather than the one a filer abbreviated.
3. **A trigram match** on that name against securities that do have a ticker.
   Never applied: each candidate is written to ``manual_review`` for a person
   (:class:`~app.db.models.manual_review.ManualReview`).

Whichever won is ``security.resolution_source``. If none did, the security keeps
the name its first filing gave it (``security.filed_name``), and the API shows
that name and the CUSIP.

What gets asked again
---------------------
Without ``force``: every security with no ticker that a person has not
resolved. OpenFIGI answers come from ``figi_response_log`` where it has them, so
a re-run spends no quota on what it already asked. Misses older than
:data:`MISS_RETRY_AFTER` are the exception and are asked again, because a newly
listed name can resolve next month.

With ``force``: every security a person has not resolved, resolved or not, and
OpenFIGI is asked again about all of them. A manual resolution is never
touched. It is not a candidate, and the ``UPDATE`` refuses it too, in case
``resolve-cusip`` ran while this was working.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

from sqlalchemy import ColumnElement, case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.figi_response_log import FIGIOutcome
from app.db.models.manual_review import ManualReview, ReviewStatus
from app.db.models.security import ResolutionSource, Security
from app.db.models.security_alias import SecurityAlias
from app.db.queries.cusip_coverage import exposure_by_security
from app.ingestion.enrichment.figi import FIGIMatch, FIGIResult
from app.ingestion.enrichment.issuer_names import ListedName, issuer_name, latest_listed_names

logger = get_logger(__name__)

#: OpenFIGI's exchange code for the US composite: one row per instrument that
#: stands for every US venue it trades on. The listing to take when there is one.
US_COMPOSITE: Final = "US"

#: Exchange codes of US venues: every code whose rows OpenFIGI rolls up into a
#: ``US`` composite. Derived from the answers for all 20,766 dev securities on
#: 2026-10-12, not from a list of Bloomberg codes. Not every code that starts
#: with a U is American (``UZ``), and some American ones do not start with a U.
#: Used only when no composite came back.
US_VENUES: Final = frozenset(
    {
        *("OC", "OD", "PQ"),
        *("UA", "UB", "UC", "UD", "UF", "UJ", "UM", "UN", "UP", "UQ", "UR", "UT", "UV", "UW"),
        "UX",
        *("VF", "VG", "VJ", "VK", "VL", "VP", "VT", "VY"),
    }
)

#: OpenFIGI's ``marketSector`` for stocks, ETFs, ADRs, funds and warrants. The
#: others (``Corp``, ``Pfd``, ``Govt``, ...) carry bond-style "tickers" such as
#: ``ALB 7.25 03/01/27``, which are not something to look a stock up by.
EQUITY_SECTOR: Final = "Equity"

#: How old an OpenFIGI miss gets before it is asked about again.
MISS_RETRY_AFTER: Final = timedelta(days=30)

#: Candidates per transaction, and per round of OpenFIGI requests. A run that
#: is interrupted keeps every chunk it finished.
CHUNK_SIZE: Final = 1000

#: ``pg_trgm`` similarity a name must reach to be suggested, and how many
#: suggestions one security gets at most. See :func:`queue_fuzzy_matches`.
#: Calibrated on the dev backfill on 2026-10-12: below 0.8, nearly every
#: suggestion was two different issuers sharing words ("... ACQUISITION
#: CORP", "... THERAPEUTICS INC"). At 0.8, 1,769 of the 8,500 unresolved
#: securities get one. Noise remains at the top too. "CONCORD ACQUISITION
#: CORP" is 0.96 similar to "... CORP II", and an exact match can be another
#: share class, which is why none is ever applied.
FUZZY_THRESHOLD: Final = 0.8
FUZZY_LIMIT: Final = 3


# --- choosing a listing -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listing:
    """The one OpenFIGI listing a security takes its ticker and FIGI from."""

    ticker: str
    """As stored: :func:`normalise_ticker` of OpenFIGI's spelling."""

    figi: str
    """The US composite's FIGI. Every venue's row shares it."""

    @property
    def alias(self) -> str | None:
        """The share class after a hyphen, ``BRK-B``, for a ticker that has one.

        The other spelling a client is likely to put in a path. OpenFIGI's own
        slash cannot get there: ``/v1/stocks/BRK%2FB`` is not routed.
        """
        return self.ticker.replace(".", "-") if "." in self.ticker else None


def normalise_ticker(ticker: str) -> str:
    """Upper case, with a share class after a dot: ``BRK/B`` becomes ``BRK.B``.

    OpenFIGI writes the class with a slash, which cannot go in a path:
    ``/v1/stocks/BRK/B`` is a different route. The dot is the most common
    spelling elsewhere, and the hyphen is kept as an alias (:attr:`Listing.alias`).
    """
    return ticker.strip().upper().replace("/", ".")


def _is_nav_symbol(ticker: str) -> bool:
    """Nasdaq's NAV symbol for a fund: five letters starting and ending with X.

    A closed-end fund's US composite comes back twice, once under its trading
    ticker (``ECAT``) and once under the symbol its net asset value is
    published under (``XESGX``). That accounts for 326 of the 331 dev CUSIPs
    with more than one US ticker.
    """
    return len(ticker) == 5 and ticker.startswith("X") and ticker.endswith("X")


def choose_listing(result: FIGIResult | None) -> Listing | None:
    """The US equity listing a CUSIP resolves to, or ``None`` if there is not
    exactly one ticker to choose.

    OpenFIGI answers an unfiltered CUSIP with a row per venue worldwide, often
    hundreds. The ``US`` composite is taken if there is one. Failing that, a
    US venue's row is taken, provided every such row agrees on the ticker. A
    CUSIP with listings only abroad, or with no equity listing at all, gets
    ``None``. So do the rare CUSIPs with two US tickers that are not one
    trading ticker and one NAV symbol. Any of those falls through to the 13(f)
    list and the review queue.
    """
    if result is None or result.outcome is not FIGIOutcome.FOUND:
        return None
    equities = [
        match
        for match in result.matches
        if match.market_sector == EQUITY_SECTOR and match.ticker and match.ticker.strip()
    ]
    composite = [match for match in equities if match.exchange_code == US_COMPOSITE]
    candidates = composite or [match for match in equities if match.exchange_code in US_VENUES]
    tickers = {_ticker(match) for match in candidates}
    if len(tickers) > 1:
        tickers = {ticker for ticker in tickers if not _is_nav_symbol(ticker)} or tickers
    if len(tickers) != 1:
        return None
    [ticker] = tickers
    chosen = next(match for match in candidates if _ticker(match) == ticker)
    figi = chosen.figi if composite else (chosen.composite_figi or chosen.figi)
    return Listing(normalise_ticker(ticker), figi)


def _ticker(match: FIGIMatch) -> str:
    assert match.ticker is not None  # filtered in choose_listing
    return match.ticker.strip()


# --- candidates ----------------------------------------------------------------


class CusipMapper(Protocol):
    """What resolution needs from :class:`~app.ingestion.enrichment.figi.OpenFIGIClient`."""

    async def map_cusips(
        self, cusips: Sequence[str], *, refresh: bool = False
    ) -> dict[str, FIGIResult]: ...


@dataclass(frozen=True, slots=True)
class Candidate:
    security_id: int
    cusip: str
    filed_name: str | None
    exposure: Decimal
    """``SUM(holding.value_usd)``: every line any filing reported it on."""


async def candidates(
    session: AsyncSession, *, limit: int | None = None, force: bool = False
) -> list[Candidate]:
    """Securities to resolve, highest aggregate holdings value first.

    Without ``force``, those without a ticker. With it, all of them. Never one
    a person resolved. A security no holding refers to any more comes last,
    at zero. Ties go by CUSIP, so ``--limit`` is repeatable.
    """
    exposure = exposure_by_security().subquery()
    value = func.coalesce(exposure.c.exposure, 0)
    statement = (
        select(Security.id, Security.cusip, Security.filed_name, value.label("exposure"))
        .outerjoin(exposure, exposure.c.security_id == Security.id)
        .where(Security.resolution_source.is_distinct_from(ResolutionSource.MANUAL.value))
        .order_by(value.desc(), Security.cusip)
        .limit(limit)
    )
    if not force:
        statement = statement.where(Security.ticker.is_(None))
    rows = (await session.execute(statement)).all()
    return [Candidate(row.id, row.cusip, row.filed_name, Decimal(row.exposure)) for row in rows]


# --- resolving -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one security was resolved to by this run."""

    candidate: Candidate
    source: ResolutionSource | None
    ticker: str | None
    figi: str | None
    name: str | None
    alias: str | None
    """Another spelling of the ticker to look it up by: :attr:`Listing.alias`."""


def resolve(
    candidate: Candidate, figi: FIGIResult | None, listed: Mapping[str, ListedName]
) -> Resolution:
    """Apply the order of sources to one candidate. Pure: no database.

    :param listed: The latest 13(f) list entry per CUSIP, as
        :func:`~app.ingestion.enrichment.issuer_names.latest_listed_names`
        returns them.
    """
    listing = choose_listing(figi)
    entry = listed.get(candidate.cusip)
    # OpenFIGI's name only alongside its ticker. A CUSIP OpenFIGI knows only
    # from foreign listings is named by the list, per the order of sources.
    named = issuer_name(figi if listing else None, entry)
    name = named.name if named is not None else candidate.filed_name
    if listing is not None:
        return Resolution(
            candidate, ResolutionSource.OPENFIGI, listing.ticker, listing.figi, name, listing.alias
        )
    source = ResolutionSource.SEC_13F_LIST if named is not None else None
    return Resolution(candidate, source, None, None, name, None)


@dataclass
class ResolutionReport:
    """What ``resolve-cusips`` did, for its summary and its run's metrics."""

    candidates: int = 0
    exposure: Decimal = Decimal(0)
    """Aggregate holdings value of every candidate."""

    by_source: dict[ResolutionSource | None, int] = field(default_factory=dict)
    value_by_source: dict[ResolutionSource | None, Decimal] = field(default_factory=dict)
    changed: int = 0
    """Rows whose ticker, FIGI, name or source this run changed."""

    misses_retried: int = 0
    """OpenFIGI misses older than :data:`MISS_RETRY_AFTER`, asked about again."""

    aliases: int = 0
    reviews_queued: int = 0
    reviews_queued_for: int = 0
    """How many securities the new suggestions are for."""

    reviews_superseded: int = 0

    def count(self, source: ResolutionSource | None) -> int:
        return self.by_source.get(source, 0)

    def value(self, source: ResolutionSource | None) -> Decimal:
        return self.value_by_source.get(source, Decimal(0))

    def add(self, resolution: Resolution) -> None:
        source = resolution.source
        self.by_source[source] = self.count(source) + 1
        self.value_by_source[source] = self.value(source) + resolution.candidate.exposure


async def resolve_securities(
    sessions: async_sessionmaker[AsyncSession],
    figi: CusipMapper,
    *,
    limit: int | None = None,
    force: bool = False,
    chunk_size: int = CHUNK_SIZE,
) -> ResolutionReport:
    """Resolve up to ``limit`` securities, highest value first. See the module
    docstring for which ones, and in what order of sources.

    One transaction per chunk of candidates, so an interrupted run keeps what
    it finished, and one more at the end for the review queue. The queue goes
    last so that a fuzzy match can find a security this same run resolved.
    """
    async with sessions() as session:
        todo = await candidates(session, limit=limit, force=force)
    report = ResolutionReport(
        candidates=len(todo), exposure=sum((c.exposure for c in todo), Decimal(0))
    )

    unresolved: list[int] = []
    for chunk in _chunks(todo, chunk_size):
        answers = await _ask_openfigi(figi, [c.cusip for c in chunk], force=force, report=report)
        async with sessions.begin() as session:
            listed = await latest_listed_names(
                session, [c.cusip for c in chunk if choose_listing(answers.get(c.cusip)) is None]
            )
            resolutions = [resolve(c, answers.get(c.cusip), listed) for c in chunk]
            await _write(session, resolutions, report)
        for resolution in resolutions:
            report.add(resolution)
            if resolution.ticker is None:
                unresolved.append(resolution.candidate.security_id)
        logger.info(
            "resolution.chunk",
            candidates=len(chunk),
            openfigi=sum(r.source is ResolutionSource.OPENFIGI for r in resolutions),
            sec_13f_list=sum(r.source is ResolutionSource.SEC_13F_LIST for r in resolutions),
        )

    if unresolved:
        async with sessions.begin() as session:
            queued = await queue_fuzzy_matches(session, unresolved)
        report.reviews_queued = len(queued)
        report.reviews_queued_for = len({security_id for security_id, _ in queued})
    return report


async def _ask_openfigi(
    figi: CusipMapper, cusips: list[str], *, force: bool, report: ResolutionReport
) -> dict[str, FIGIResult]:
    """OpenFIGI's answers, from the log where it has them; old misses asked again."""
    answers = await figi.map_cusips(cusips, refresh=force)
    if force or not answers:
        return answers
    cutoff = datetime.now(UTC) - MISS_RETRY_AFTER
    stale = [
        cusip
        for cusip, answer in answers.items()
        if answer.outcome is FIGIOutcome.NOT_FOUND and answer.requested_at < cutoff
    ]
    if stale:
        answers |= await figi.map_cusips(stale, refresh=True)
        report.misses_retried += len(stale)
    return answers


#: One statement per chunk, the chunk passed as parallel arrays. Only rows that
#: change are updated, and never a person's resolution, even one made since
#: this run began.
_UPDATE_RESOLVED: Final = text(
    """
    UPDATE security s
    SET ticker = v.ticker,
        figi = v.figi,
        name = v.name,
        resolution_source = v.source,
        resolved_at = CASE WHEN v.source IS NULL THEN NULL ELSE now() END,
        resolution_note = NULL
    FROM unnest(
        CAST(:ids AS bigint[]),
        CAST(:tickers AS text[]),
        CAST(:figis AS text[]),
        CAST(:names AS text[]),
        CAST(:sources AS text[])
    ) AS v(id, ticker, figi, name, source)
    WHERE s.id = v.id
      AND s.resolution_source IS DISTINCT FROM 'manual'
      AND (s.ticker, s.figi::text, s.name, s.resolution_source)
          IS DISTINCT FROM (v.ticker, v.figi, v.name, v.source)
    RETURNING s.id
    """
)


async def _write(
    session: AsyncSession, resolutions: Sequence[Resolution], report: ResolutionReport
) -> None:
    """Write a chunk's resolutions, its aliases, and close the review rows of
    every security it gave a ticker.

    A row is updated only if something in it changes, so ``resolved_at`` says
    when its resolution last changed rather than when it was last looked at.
    """
    if not resolutions:
        return
    changed = await session.execute(
        _UPDATE_RESOLVED,
        {
            "ids": [r.candidate.security_id for r in resolutions],
            "tickers": [r.ticker for r in resolutions],
            "figis": [r.figi for r in resolutions],
            "names": [r.name for r in resolutions],
            "sources": [r.source.value if r.source is not None else None for r in resolutions],
        },
    )
    report.changed += len(changed.all())

    aliases = [
        {"security_id": r.candidate.security_id, "alias": r.alias, "source": r.source.value}
        for r in resolutions
        if r.alias is not None and r.source is not None
    ]
    if aliases:
        inserted = await session.execute(
            pg_insert(SecurityAlias)
            .values(aliases)
            .on_conflict_do_nothing()
            .returning(SecurityAlias.id)
        )
        report.aliases += len(inserted.all())

    tickered = [r.candidate.security_id for r in resolutions if r.ticker is not None]
    report.reviews_superseded += len(
        await close_reviews(session, tickered, ReviewStatus.SUPERSEDED)
    )


async def close_reviews(
    session: AsyncSession,
    security_ids: Sequence[int],
    status: ReviewStatus,
    *,
    accepted_ticker: str | None = None,
) -> list[str]:
    """Close the pending review rows of ``security_ids``; the status each got.

    ``status``, except that with ``accepted_ticker`` the rows that suggested
    it are ``accepted``.
    """
    if not security_ids:
        return []
    review = ManualReview
    new_status: ColumnElement[str] | str = status.value
    if accepted_ticker is not None:
        new_status = case(
            (
                func.upper(review.suggested_ticker) == accepted_ticker.upper(),
                ReviewStatus.ACCEPTED.value,
            ),
            else_=status.value,
        )
    result = await session.execute(
        update(review)
        .where(review.security_id.in_(security_ids), review.status == ReviewStatus.PENDING.value)
        .values(status=new_status, closed_at=func.now())
        .returning(review.status)
    )
    return list(result.scalars().all())


# --- the review queue ----------------------------------------------------------


async def queue_fuzzy_matches(
    session: AsyncSession, security_ids: Sequence[int]
) -> list[tuple[int, int]]:
    """Suggest tickers for unresolved securities by name; ``(security, suggested)`` pairs written.

    For each security still without a ticker, the :data:`FUZZY_LIMIT` securities
    with a ticker whose names are most similar, by ``pg_trgm``'s
    ``similarity``, at :data:`FUZZY_THRESHOLD` or above. Both sides' current
    and filed names are compared, and the best of the four counts. Spellings
    differ more between sources than between issuers: Exxon's own old CUSIP is
    "EXXON MOBIL CORP" as filed and "EXXONMOBIL HOLDINGS CORP" to OpenFIGI,
    0.50 similar. The ``%`` operators are what let the trigram indexes on both
    name columns find candidates. Written ``pending``. A pair already pending is
    skipped, so a second run adds nothing it suggested already.
    """
    if not security_ids:
        return []
    await session.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold', :threshold, true)"),
        {"threshold": str(FUZZY_THRESHOLD)},
    )
    rows = await session.execute(
        text(
            """
            INSERT INTO manual_review (
                security_id, suggested_security_id, suggested_ticker,
                matched_name, suggested_name, similarity
            )
            SELECT u.id, r.id, r.ticker, coalesce(u.name, u.filed_name), r.name, r.similarity
            FROM security u
            CROSS JOIN LATERAL (
                SELECT r.id, r.ticker, coalesce(r.name, r.filed_name) AS name,
                       greatest(
                           similarity(r.name, u.name),
                           similarity(r.filed_name, u.name),
                           similarity(r.name, u.filed_name),
                           similarity(r.filed_name, u.filed_name)
                       ) AS similarity
                FROM security r
                WHERE (
                        r.name % u.name
                        OR r.filed_name % u.name
                        OR r.name % u.filed_name
                        OR r.filed_name % u.filed_name
                      )
                  AND r.ticker IS NOT NULL
                  AND r.id <> u.id
                ORDER BY 4 DESC, r.id
                LIMIT :fuzzy_limit
            ) r
            WHERE u.id = ANY(:ids)
              AND u.ticker IS NULL
            ON CONFLICT (security_id, suggested_security_id) WHERE status = 'pending'
            DO NOTHING
            RETURNING security_id, suggested_security_id
            """
        ),
        {"ids": list(security_ids), "fuzzy_limit": FUZZY_LIMIT},
    )
    return [(row.security_id, row.suggested_security_id) for row in rows]


# --- by hand --------------------------------------------------------------------


class UnknownSecurityError(LookupError):
    """``resolve-cusip`` was given a CUSIP no filing has reported."""


@dataclass(frozen=True, slots=True)
class ManualResolution:
    cusip: str
    name: str | None
    ticker: str
    previous_ticker: str | None
    previous_source: str | None
    reviews_accepted: int
    reviews_superseded: int


async def resolve_manually(
    session: AsyncSession, cusip: str, ticker: str, *, note: str | None
) -> ManualResolution:
    """Set a security's ticker by hand, for good.

    ``resolution_source`` becomes ``manual``, which ``resolve-cusips`` never
    overwrites, ``--force`` included. The FIGI is kept if the ticker is the one
    it already had (pinning a resolution) and cleared otherwise, since it
    belonged to the listing being replaced. The name is left alone. Pending
    review rows are closed: ``accepted`` where they suggested this ticker.
    """
    ticker = normalise_ticker(ticker)
    row = (
        await session.execute(
            select(Security.id, Security.name, Security.ticker, Security.resolution_source)
            .where(Security.cusip == cusip)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise UnknownSecurityError(cusip)
    keep_figi = row.ticker == ticker
    await session.execute(
        update(Security)
        .where(Security.id == row.id)
        .values(
            ticker=ticker,
            figi=Security.figi if keep_figi else None,
            resolution_source=ResolutionSource.MANUAL.value,
            resolution_note=note,
            resolved_at=func.now(),
        )
    )
    closed = await close_reviews(session, [row.id], ReviewStatus.SUPERSEDED, accepted_ticker=ticker)
    accepted = sum(status == ReviewStatus.ACCEPTED.value for status in closed)
    return ManualResolution(
        cusip=cusip,
        name=row.name,
        ticker=ticker,
        previous_ticker=row.ticker,
        previous_source=row.resolution_source,
        reviews_accepted=accepted,
        reviews_superseded=len(closed) - accepted,
    )


def _chunks(items: Sequence[Candidate], size: int) -> Iterator[Sequence[Candidate]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
