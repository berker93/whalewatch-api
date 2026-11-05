"""The House Clerk's yearly indexes into ``congress_filing``, a year at a time.

For each year: fetch the ZIP and archive it (:mod:`~app.ingestion.congress.house_clerk`),
read its PTRs (:mod:`~app.ingestion.congress.house_index`), and upsert them on
``(chamber, doc_id)``, each year in a transaction of its own. A row is written
only when it is new or the index now says something different about it, so
reading a year again changes nothing — no row, no ``updated_at``, and no
archive object, since an unchanged index is already archived under its checksum.

The watermark
-------------
A year's ZIP cannot be fetched in part, so "only what is new" is a matter of
which years. ``crawl_watermark`` row ``ingest-house-index`` holds the newest
filing date among the PTRs read; ``--catch-up`` — the daily run — reads the
watermark's year, the :data:`LOOKBACK_YEARS` before it, and every year since,
to today's. On a run in 2026 that is 2024, 2025 and 2026: three downloads of
about 100 KB, and the rest of the archive untouched.

Why look back: a year's index keeps gaining filings long after the year ends.
Across the 2020 to 2026 indexes as fetched in November 2026, 42 PTRs were
filed a year after the year they are indexed under, 2 two years after, and 1
three years after (2020's, filed in 2023). Two years back catches all but that
last kind; a ``--from-year`` sweep now and then catches the rest.

The watermark moves only on a ``--catch-up`` or ``--from-year`` run in which no
year failed, and only forward. A ``--year`` run reads its year without moving
it, as a ``crawl-form4 --date`` reads its day: the years around it were not
read. A filing date after today (a typo at the Clerk's end) is stored, and
does not move it.

What a year can be
------------------
Read; not published yet (a 404 for the current year or a later one, which the
first days of January are); or failed — any other answer, a ZIP or XML that
cannot be read, or the Clerk not answering. A failed year is reported and the
other years are still read: unlike days in the Form 4 crawl, years are
independent, and the watermark is held back instead. The fetcher itself being
unavailable or refusing is not a year's failure, and stops the run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Boolean, func, literal_column, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import ReturningInsert

from app.core.logging import get_logger
from app.db.models.congress_filing import CongressFiling
from app.db.models.crawl_watermark import CrawlWatermark
from app.ingestion.congress.house_clerk import (
    FIRST_YEAR,
    ArchivedIndex,
    HouseClerk,
    HouseClerkError,
    IndexNotFound,
)
from app.ingestion.congress.house_index import (
    HouseIndex,
    HouseIndexError,
    IndexEntry,
    parse_house_index,
)
from app.ingestion.insider_discovery import NoWatermarkError, read_watermark
from app.ingestion.remote.fetcher import UpstreamUnreachable

logger = get_logger(__name__)

#: The watermark's key, and the CLI verb that moves it.
HOUSE_INDEX_JOB: Final = "ingest-house-index"

HOUSE: Final = "house"

#: Years before the watermark's that a catch-up reads again. See the module docstring.
LOOKBACK_YEARS: Final = 2

#: Rows per INSERT: at most fifteen parameters a row, well under asyncpg's 32,767.
_BATCH: Final = 2_000

#: What a re-read may change. Not ``source_key``, which says where a row was
#: first seen, nor the chamber and doc id, which are the key.
_CHANGING: Final = (
    "report_type",
    "last_name",
    "first_name",
    "prefix",
    "suffix",
    "office",
    "filing_date",
    "year",
)


class YearKind(StrEnum):
    READ = "read"
    UNPUBLISHED = "not published yet"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Stored:
    """What an upsert did to the PTRs it was given."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class YearRead:
    """One year of a run."""

    year: int
    kind: YearKind
    index: ArchivedIndex | None = None
    parsed: HouseIndex | None = None
    stored: Stored = field(default_factory=Stored)
    newest: date | None = None
    """The newest PTR filing date in the index, up to today."""
    error: str | None = None


@dataclass(slots=True)
class HouseIngest:
    """What a run read, and where it left the watermark."""

    watermark_before: date | None
    watermark_after: date | None
    years: list[YearRead] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(read.kind is YearKind.FAILED for read in self.years)


# --- which years ---------------------------------------------------------------


def years_between(first: int, last: int) -> list[int]:
    """``first`` to ``last``, both included. Empty if ``first`` is later."""
    return list(range(first, last + 1))


async def catch_up_years(session: AsyncSession, today: date) -> list[int]:
    """The watermark's year, :data:`LOOKBACK_YEARS` before it, and on to today's.

    :raises NoWatermarkError: No run has set it.
    """
    watermark = await read_watermark(session, HOUSE_INDEX_JOB)
    if watermark is None:
        raise NoWatermarkError(
            "no run has set a watermark to catch up from: run ingest-house-index with "
            "--from-year first, to say where reading starts"
        )
    return years_between(max(FIRST_YEAR, watermark.year - LOOKBACK_YEARS), today.year)


# --- the run -------------------------------------------------------------------


async def ingest_house_index(
    sessions: async_sessionmaker[AsyncSession],
    clerk: HouseClerk,
    years: Iterable[int],
    *,
    today: date,
    move_watermark: bool,
) -> HouseIngest:
    """Read each year's index into ``congress_filing``; move the watermark if
    ``move_watermark`` and no year failed.

    :raises FetcherUnavailable: The fetcher cannot be used. Years already read
        are kept.
    :raises FetchRefused: The fetcher declined the request.
    """
    async with sessions() as session:
        before = await read_watermark(session, HOUSE_INDEX_JOB)
    ingest = HouseIngest(watermark_before=before, watermark_after=before)
    for year in years:
        read = await _read_year(sessions, clerk, year, today=today)
        ingest.years.append(read)
        logger.info(
            "house_index.year",
            year=year,
            kind=read.kind.value,
            inserted=read.stored.inserted,
            updated=read.stored.updated,
            error=read.error,
        )

    if move_watermark and not ingest.failed:
        newest = max(
            (read.newest for read in ingest.years if read.newest is not None), default=None
        )
        if newest is not None and (before is None or newest > before):
            async with sessions.begin() as session:
                await _set_watermark(session, newest)
            ingest.watermark_after = newest
    return ingest


async def _read_year(
    sessions: async_sessionmaker[AsyncSession], clerk: HouseClerk, year: int, *, today: date
) -> YearRead:
    try:
        index = await clerk.fetch_index(year)
    except IndexNotFound as failure:
        if year >= today.year:
            return YearRead(year, YearKind.UNPUBLISHED)
        return YearRead(year, YearKind.FAILED, error=str(failure))
    except (HouseClerkError, HouseIndexError, UpstreamUnreachable) as failure:
        return YearRead(year, YearKind.FAILED, error=str(failure))

    try:
        parsed = parse_house_index(index.xml)
    except HouseIndexError as failure:
        # Archived already: a parser fix re-reads it.
        return YearRead(year, YearKind.FAILED, index=index, error=str(failure))

    async with sessions.begin() as session:
        stored = await store_house_filings(session, parsed.ptrs, source_key=index.key)
    newest = max((ptr.filing_date for ptr in parsed.ptrs if ptr.filing_date <= today), default=None)
    return YearRead(year, YearKind.READ, index=index, parsed=parsed, stored=stored, newest=newest)


# --- writing ---------------------------------------------------------------------


async def store_house_filings(
    session: AsyncSession, ptrs: Sequence[IndexEntry], *, source_key: str
) -> Stored:
    """Upsert ``ptrs`` on ``(house, doc_id)``, touching only rows that differ.

    ``ptrs`` must not repeat a doc id (the parser collapses repeats): Postgres
    refuses an upsert that would write one row twice. Does not commit.
    """
    return await upsert_filings(session, [_row(ptr, source_key) for ptr in ptrs], _CHANGING)


async def upsert_filings(
    session: AsyncSession, rows: Sequence[dict[str, Any]], changing: Sequence[str]
) -> Stored:
    """Upsert ``congress_filing`` ``rows`` on ``(chamber, doc_id)``, updating
    ``changing`` only where one of them differs, and ``updated_at`` with them.

    Either chamber's: ``rows`` must not repeat a key. Does not commit.
    """
    inserted = updated = 0
    table = CongressFiling.__table__.c
    for start in range(0, len(rows), _BATCH):
        insert = pg_insert(CongressFiling).values(list(rows[start : start + _BATCH]))
        statement: ReturningInsert[tuple[bool]] = insert.on_conflict_do_update(
            index_elements=[CongressFiling.chamber, CongressFiling.doc_id],
            set_={name: insert.excluded[name] for name in changing} | {"updated_at": func.now()},
            where=tuple_(*(table[name] for name in changing)).is_distinct_from(
                tuple_(*(insert.excluded[name] for name in changing))
            ),
        ).returning(literal_column("xmax = 0", Boolean))
        # A row the WHERE skipped returns nothing; an insert has no xmax yet.
        for was_insert in (await session.scalars(statement)).all():
            if was_insert:
                inserted += 1
            else:
                updated += 1
    return Stored(inserted=inserted, updated=updated, unchanged=len(rows) - inserted - updated)


def _row(ptr: IndexEntry, source_key: str) -> dict[str, Any]:
    return {
        "chamber": HOUSE,
        "doc_id": ptr.doc_id,
        "report_type": "ptr",
        "last_name": ptr.last_name,
        "first_name": ptr.first_name,
        "prefix": ptr.prefix,
        "suffix": ptr.suffix,
        "office": ptr.office,
        "filing_date": ptr.filing_date,
        "year": ptr.year,
        "source_key": source_key,
    }


async def _set_watermark(session: AsyncSession, day: date) -> None:
    insert = pg_insert(CrawlWatermark).values(job=HOUSE_INDEX_JOB, last_date=day)
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[CrawlWatermark.job],
            set_={"last_date": insert.excluded.last_date, "updated_at": func.now()},
        )
    )
