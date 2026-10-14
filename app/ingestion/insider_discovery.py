"""Which Form 4s to load: EDGAR's daily index, a day at a time, bounded to a universe.

13F discovery asks EDGAR about each filer we track (:mod:`app.ingestion.discovery`).
Form 4 discovery cannot: the people who file them are not known in advance.
So it reads what EDGAR published each day (:mod:`app.ingestion.edgar.daily_index`),
keeps the Form 4s and 4/As whose issuer is in the universe, and queues them in
``pending_filing``, through the same :func:`~app.ingestion.discovery.enqueue`
as 13Fs: what is loaded is skipped and what is queued stays queued.

The universe
------------
All of EDGAR is about 1,700 Form 4s a business day. Most are about companies
nobody here would look at, so the crawl is bounded from the start
(``FORM4_UNIVERSE``):

``held``
    Issuers that were at least ``FORM4_MIN_WEIGHT_PCT`` of one tracked
    portfolio in the last four quarters. Held positions are securities and
    Form 4s are filed under a CIK, so they are joined through SEC's
    ticker-to-CIK map (``sec_company_ticker``), which each run refreshes first.
    A security without a ticker, or with one SEC no longer lists (an acquired
    company), is out. On 2026's data the map covers 98% of held value, and
    what it misses is mostly ETFs, which have no insiders.
``sp500``
    The CIKs in a CSV, by default the S&P 500 snapshot in
    ``data/universe/sp500.csv``.

A Form 4 is listed once under its issuer and once under each reporting owner,
and a line matches on whichever CIK is in the universe. So a tracked company
that is itself a 10% owner of another (Berkshire in Occidental) brings in that
company's Form 4s too. The loader reads the issuer from the document; a filing
it does not want costs one fetch.

The watermark
-------------
``crawl_watermark`` holds the last day read in full. It moves only to the day
after itself: a day that failed stops the walk, and a day crawled out of order
is crawled without moving it, because the days between were not. So
``--catch-up``, which starts the day after the watermark, never steps over a
day that was not read. A first run sets it wherever it starts.

Which days count as read
------------------------
A day with an index, once its Form 4s are queued. A weekend. A weekday with no
index when a later day has one: a holiday, or one of EDGAR's rare gaps, which
will not be published later. A weekday with no index and no later one is not
published *yet* (today, before about 10pm in New York): the walk stops there
without moving the watermark, and the next run asks again.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.crawl_watermark import CrawlWatermark
from app.db.models.position_snapshot import PositionSnapshot
from app.db.models.sec_company_ticker import SecCompanyTicker
from app.db.models.security import Security
from app.ingestion.discovery import DiscoveredFiling, enqueue
from app.ingestion.edgar.client import EdgarClient, EdgarRateLimited, EdgarServerError
from app.ingestion.edgar.company_tickers import CompanyTicker, fetch_company_tickers
from app.ingestion.edgar.daily_index import (
    DailyIndexMalformedError,
    IndexEntry,
    fetch_daily_index,
    fetch_published_days,
)
from app.ingestion.edgar.submissions import FilingRef

logger = get_logger(__name__)

#: The watermark's key, and the CLI verb that moves it.
CRAWL_JOB: Final = "crawl-form4"

#: Where EDGAR dates a filing, and so where "today" is.
EDGAR_TIMEZONE: Final = ZoneInfo("America/New_York")

#: The S&P 500 universe unless ``FORM4_SP500_PATH`` says otherwise: the
#: constituents CSV of github.com/datasets/s-and-p-500-companies (public
#: domain), as of 2026-10-14. Refresh it by downloading that file again.
DEFAULT_SP500_PATH: Final = Path(__file__).resolve().parents[2] / "data" / "universe" / "sp500.csv"

#: Rows per INSERT into ``sec_company_ticker``: four parameters a row, under
#: asyncpg's 32,767 a statement.
_TICKER_BATCH: Final = 5_000


class UniverseError(Exception):
    """The universe cannot be built, or is empty. Nothing is crawled: a crawl
    against an empty universe would move the watermark past days whose Form 4s
    it never looked for."""


class NoWatermarkError(LookupError):
    """``--catch-up`` with nothing to catch up from."""


def new_york_today() -> date:
    """Today where EDGAR is."""
    return datetime.now(EDGAR_TIMEZONE).date()


# --- the universe ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Universe:
    kind: str
    """``held`` or ``sp500``."""

    ciks: frozenset[str]
    """Issuer CIKs, zero-padded."""

    description: str
    """For a person: what the CIKs are, and how many."""


async def held_universe(
    sessions: async_sessionmaker[AsyncSession],
    edgar: EdgarClient,
    *,
    min_weight_pct: float,
) -> Universe:
    """Issuers tracked investors held, after refreshing SEC's ticker map.

    Checks there is anything held before asking SEC for the map, so that a
    database with no positions fails without a request.

    :raises UniverseError: Nothing is published, or nothing held maps to a CIK.
    """
    async with sessions() as session:
        latest = await session.scalar(select(func.max(PositionSnapshot.period_of_report)))
    if latest is None:
        raise UniverseError(
            "no positions are published, so no issuer is held: load 13Fs first "
            "(discover-filings, backfill), or set FORM4_UNIVERSE=sp500"
        )

    tickers = await fetch_company_tickers(edgar)
    async with sessions.begin() as session:
        await store_company_tickers(session, tickers)
        ciks = await held_issuer_ciks(session, latest=latest, min_weight_pct=min_weight_pct)
    if not ciks:
        raise UniverseError(
            f"no issuer was {min_weight_pct}% of a tracked portfolio in the four "
            f"quarters to {latest} and has a ticker SEC lists"
        )
    floor = "held" if min_weight_pct == 0 else f"at least {min_weight_pct:g}% of a portfolio"
    return Universe(
        kind="held",
        ciks=ciks,
        description=f"{_issuers(len(ciks))} {floor} in the four quarters to {latest}",
    )


async def store_company_tickers(session: AsyncSession, tickers: Sequence[CompanyTicker]) -> None:
    """Upsert SEC's map. A ticker now listed under another CIK moves to it; a
    ticker no longer listed is kept, with its old ``seen_at``. Does not commit."""
    seen_at = datetime.now(UTC)
    for start in range(0, len(tickers), _TICKER_BATCH):
        batch = tickers[start : start + _TICKER_BATCH]
        insert = pg_insert(SecCompanyTicker).values(
            [
                {"ticker": row.ticker, "cik": row.cik, "name": row.name, "seen_at": seen_at}
                for row in batch
            ]
        )
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=[SecCompanyTicker.ticker],
                set_={
                    "cik": insert.excluded.cik,
                    "name": insert.excluded.name,
                    "seen_at": insert.excluded.seen_at,
                },
            )
        )


async def held_issuer_ciks(
    session: AsyncSession, *, latest: date, min_weight_pct: float
) -> frozenset[str]:
    """The CIKs behind every security at least ``min_weight_pct`` of a published
    portfolio in the four quarters ending ``latest``.

    Joined on the ticker with ``security``'s dot for a share class turned into
    SEC's dash: ``BRK.B`` is SEC's ``BRK-B``.
    """
    # Quarter ends never fall on 29 February, so this cannot raise.
    year_before = latest.replace(year=latest.year - 1)
    statement = (
        select(SecCompanyTicker.cik)
        .distinct()
        .join(Security, func.replace(Security.ticker, ".", "-") == SecCompanyTicker.ticker)
        .join(PositionSnapshot, PositionSnapshot.security_id == Security.id)
        .where(PositionSnapshot.period_of_report > year_before)
    )
    if min_weight_pct > 0:
        statement = statement.where(PositionSnapshot.weight_pct >= min_weight_pct)
    return frozenset(await session.scalars(statement))


def sp500_universe(path: Path | None) -> Universe:
    """The CIKs in a CSV with a ``CIK`` column.

    :raises UniverseError: No such file, no ``CIK`` column, or no CIKs.
    """
    path = DEFAULT_SP500_PATH if path is None else path
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as failure:
        raise UniverseError(f"cannot read the S&P 500 list at {path}: {failure}") from failure
    try:
        ciks = frozenset(f"{int(row['CIK']):010d}" for row in rows if row["CIK"].strip())
    except (KeyError, ValueError, AttributeError) as failure:
        raise UniverseError(f"{path} has no readable CIK column: {failure!r}") from failure
    if not ciks:
        raise UniverseError(f"{path} lists no CIKs")
    return Universe(kind="sp500", ciks=ciks, description=f"{_issuers(len(ciks))} from {path.name}")


def _issuers(count: int) -> str:
    return f"{count:,} issuer" if count == 1 else f"{count:,} issuers"


# --- the watermark -----------------------------------------------------------


async def read_watermark(session: AsyncSession, job: str = CRAWL_JOB) -> date | None:
    watermark: date | None = await session.scalar(
        select(CrawlWatermark.last_date).where(CrawlWatermark.job == job)
    )
    return watermark


async def catch_up_days(session: AsyncSession, today: date, job: str = CRAWL_JOB) -> list[date]:
    """Every day after the watermark, to ``today``. Empty when it is today.

    :raises NoWatermarkError: No run has set it.
    """
    watermark = await read_watermark(session, job)
    if watermark is None:
        raise NoWatermarkError(
            "no crawl has set a watermark to catch up from: run crawl-form4 with --from "
            "(or --date) first, to say where crawling starts"
        )
    return days_between(watermark + timedelta(days=1), today)


def days_between(first: date, last: date) -> list[date]:
    """``first`` to ``last``, both included. Empty if ``first`` is later."""
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


async def _set_watermark(session: AsyncSession, day: date, job: str) -> None:
    insert = pg_insert(CrawlWatermark).values(job=job, last_date=day)
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[CrawlWatermark.job],
            set_={"last_date": insert.excluded.last_date, "updated_at": func.now()},
        )
    )


# --- which days have an index --------------------------------------------------


class DayKind(StrEnum):
    """What one day turned out to be."""

    INDEXED = "indexed"
    WEEKEND = "weekend"
    NO_INDEX = "no index"
    """A weekday without an index when a later day has one: a holiday or a gap.
    Read, and empty."""

    UNPUBLISHED = "not published yet"
    """A weekday without an index, and no later day with one. Not read."""

    FAILED = "failed"


class IndexCalendar:
    """Which days have a daily index, from each quarter's directory listing.

    One listing request per quarter a run touches, plus the current quarter's,
    which says how far EDGAR has published.
    """

    def __init__(self, edgar: EdgarClient, today: date) -> None:
        self._edgar = edgar
        self._today = today
        self._listings: dict[tuple[int, int], frozenset[date]] = {}
        self._latest: date | None = None
        self._latest_known = False

    async def kind(self, day: date) -> DayKind:
        if day in await self._published(day.year, _quarter(day)):
            return DayKind.INDEXED
        if day.weekday() >= 5:
            return DayKind.WEEKEND
        latest = await self.latest_published()
        if latest is not None and day < latest:
            return DayKind.NO_INDEX
        return DayKind.UNPUBLISHED

    async def latest_published(self) -> date | None:
        """The newest day with an index: in today's quarter, or the one before
        when today's has none yet."""
        if not self._latest_known:
            year, quarter = self._today.year, _quarter(self._today)
            days = await self._published(year, quarter)
            if not days:
                year, quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
                days = await self._published(year, quarter)
            self._latest = max(days, default=None)
            self._latest_known = True
        return self._latest

    async def _published(self, year: int, quarter: int) -> frozenset[date]:
        key = (year, quarter)
        if key not in self._listings:
            self._listings[key] = await fetch_published_days(self._edgar, year, quarter)
        return self._listings[key]


def _quarter(day: date) -> int:
    return (day.month - 1) // 3 + 1


# --- the crawl ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawledDay:
    """One day of a crawl."""

    day: date
    kind: DayKind

    listed: int = 0
    """Form 4s and 4/As in the day's index, all of EDGAR."""

    matched: tuple[DiscoveredFiling, ...] = ()
    """The ones about an issuer in the universe."""

    new: int = 0
    """:attr:`matched` less what is loaded: what is now in ``pending_filing``."""

    error: str | None = None
    """Why a :attr:`DayKind.FAILED` day failed."""


@dataclass(slots=True)
class Crawl:
    """What a run read, and where it left the watermark."""

    watermark_before: date | None
    watermark_after: date | None = None
    days: list[CrawledDay] = field(default_factory=list)

    stopped: CrawledDay | None = None
    """The day the walk stopped at, unpublished or failed. Days after it in the
    range were not looked at."""

    @property
    def failed(self) -> bool:
        return self.stopped is not None and self.stopped.kind is DayKind.FAILED


async def crawl_form4(
    sessions: async_sessionmaker[AsyncSession],
    edgar: EdgarClient,
    universe: frozenset[str],
    days: Iterable[date],
    *,
    today: date,
    job: str = CRAWL_JOB,
) -> Crawl:
    """Read each day's index in order, queue the universe's Form 4s, and move
    the watermark through each day read that follows it.

    Each day commits on its own, its queue rows and its watermark together, so
    a run that stops has kept every day before the stop. Stops at the first
    day that fails or is not published yet. EDGAR blocking us is a failure
    like any other here: the days before it are kept, and the next run starts
    at the day it stopped on.

    :param universe: Issuer CIKs. Must not be empty: see :class:`UniverseError`.
    :param today: Today in New York, which decides how far EDGAR has published.
    """
    if not universe:
        raise UniverseError("an empty universe would read days without looking for anything")

    async with sessions() as session:
        watermark = await read_watermark(session, job)
    crawl = Crawl(watermark_before=watermark, watermark_after=watermark)
    calendar = IndexCalendar(edgar, today)

    for day in days:
        try:
            crawled = await _crawl_day(edgar, calendar, universe, day)
        except (
            EdgarRateLimited,
            EdgarServerError,
            DailyIndexMalformedError,
            # A 404 or S3's 403 for a listed index, a connection lost after retries.
            httpx.HTTPError,
        ) as failure:
            crawled = CrawledDay(
                day=day, kind=DayKind.FAILED, error=f"{type(failure).__name__}: {failure}"
            )
            logger.warning("form4.day_failed", day=day, error=crawled.error)
        if crawled.kind in (DayKind.FAILED, DayKind.UNPUBLISHED):
            crawl.stopped = crawled
            crawl.days.append(crawled)
            break

        async with sessions.begin() as session:
            _, new = await enqueue(session, crawled.matched)
            if crawl.watermark_after is None or day == crawl.watermark_after + timedelta(days=1):
                await _set_watermark(session, day, job)
                crawl.watermark_after = day
            elif day > crawl.watermark_after:
                logger.info(
                    "form4.watermark_held",
                    day=day,
                    watermark=crawl.watermark_after,
                )
        crawled = CrawledDay(
            day=day, kind=crawled.kind, listed=crawled.listed, matched=crawled.matched, new=len(new)
        )
        crawl.days.append(crawled)
        logger.info(
            "form4.day_crawled",
            day=day,
            kind=crawled.kind.value,
            listed=crawled.listed,
            matched=len(crawled.matched),
            new=crawled.new,
        )
    return crawl


async def _crawl_day(
    edgar: EdgarClient, calendar: IndexCalendar, universe: frozenset[str], day: date
) -> CrawledDay:
    kind = await calendar.kind(day)
    if kind is not DayKind.INDEXED:
        return CrawledDay(day=day, kind=kind)
    entries = await fetch_daily_index(edgar, day)
    return CrawledDay(
        day=day,
        kind=kind,
        listed=len({entry.accession_no for entry in entries}),
        matched=select_filings(entries, universe),
    )


def select_filings(
    entries: Iterable[IndexEntry], universe: frozenset[str]
) -> tuple[DiscoveredFiling, ...]:
    """The filings with a line under a CIK in ``universe``, once each, by
    accession number. The CIK kept is the matching line's, whose archive
    directory holds the filing."""
    found: dict[str, DiscoveredFiling] = {}
    for entry in entries:
        if entry.cik in universe:
            found.setdefault(
                entry.accession_no,
                DiscoveredFiling(
                    cik=entry.cik,
                    filing=FilingRef(
                        accession_no=entry.accession_no,
                        form_type=entry.form_type,
                        filing_date=entry.date_filed,
                        report_date=None,
                        primary_document=None,
                        is_xbrl=False,
                    ),
                ),
            )
    return tuple(sorted(found.values(), key=lambda row: row.accession_no))
