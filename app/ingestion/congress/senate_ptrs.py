"""Senate eFD's PTRs into ``congress_filing`` and ``congress_transaction``, in one session.

A run does two things through one eFD session
(:class:`~app.ingestion.congress.senate_efd.SenateEfd`: terms, cookie, CSRF):

1. **Search** the window of filing dates, a page of 100 at a time, each page
   archived before it is read (:func:`~app.ingestion.congress.senate_ptr.parse_search_page`),
   and upsert every report listed on ``(senate, doc_id)``, as the House index
   is: a row written only when new or different, in one transaction.
2. **Read** every Senate filing not read yet by this
   :data:`~app.ingestion.congress.senate_ptr.SENATE_PARSER_VERSION` — those the
   search just found, and any an earlier run left — each in a transaction of
   its own, as the House PDFs are (:func:`~app.ingestion.congress.ptrs.store_ptr`):

   - **Filed online** (``/search/view/ptr/…``): the page from the archive if it
     is there, else fetched and archived; its table read into rows,
     ``parsed``, or ``needs_review`` for a value eFD had not used before.
   - **Paper** (``/search/view/paper/…``): scanned page images, which nothing
     here reads. Never fetched: the filing keeps eFD's link as its
     ``document_url`` and gets one placeholder row, as an unreadable House scan
     does, so the feed can say "not machine-readable — view original".

The window, and the watermark
-----------------------------
``crawl_watermark`` row ``ingest-senate-ptrs`` holds the newest filing date
a complete search found. A catch-up — the scheduled run — searches from
:data:`LOOKBACK_DAYS` before it to today: a paper PTR is listed under the day
the Senate received it, which can be days before it is scanned and posted.
The watermark moves only forward, only on a catch-up or a window that reaches
today, and only when the search read every report it counted: one that came
up short (eFD's list shifting under the pages) leaves it, and the next run's
window reads them again.

When it stops
-------------
- :class:`~app.ingestion.congress.senate_ptr.SenateLayoutError`: a page is not
  eFD's shape. The whole run stops, since one page that moved means they all
  have. A report page from the archive is fetched once more first, in case
  what was archived was a bad answer rather than a new layout.
- :class:`~app.ingestion.congress.senate_efd.SenateEfdSessionError`: eFD will
  not keep a session. Nothing else can be read either.
- The fetcher unavailable or refusing, as for the House.

A report eFD answers with an error status, or not at all, is one filing
failed: reported, the run is partial, and the next run tries it again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_transaction import CongressTransaction
from app.db.models.crawl_watermark import CrawlWatermark
from app.ingestion.congress.filings import Stored, upsert_filings
from app.ingestion.congress.ptr import Extraction, PtrDocument, RowStatus
from app.ingestion.congress.ptrs import Coverage, FilingRead, Outcome, Pending, PtrIngest, store_ptr
from app.ingestion.congress.senate_efd import (
    MAX_SEARCH_PAGE,
    Archived,
    SenateEfd,
    SenateEfdError,
    SenateEfdSessionError,
)
from app.ingestion.congress.senate_ptr import (
    PAPER,
    SENATE_PARSER_VERSION,
    SearchRow,
    SenateLayoutError,
    parse_report,
    parse_search_page,
)
from app.ingestion.insider_discovery import NoWatermarkError, read_watermark
from app.ingestion.remote.fetcher import UpstreamUnreachable
from app.storage.raw import RawStoreError

logger = get_logger(__name__)

#: The watermark's key, the job's name, and the CLI verb.
SENATE_PTRS_JOB: Final = "ingest-senate-ptrs"

SENATE: Final = "senate"

#: The first day eFD lists PTRs from: the STOCK Act's first filings.
FIRST_DAY: Final = date(2012, 1, 1)

#: Days before the watermark a catch-up searches again. See the module docstring.
LOOKBACK_DAYS: Final = 14

#: Days without a new Senate PTR after which a catch-up says eFD has gone quiet.
#: Senators filed something at least every 27 days from 2021 to 2026, so a
#: longer silence is likelier a search that matches nothing than a quiet month.
QUIET_DAYS: Final = 35

#: Why a paper filing's one row is a placeholder.
PAPER_REASON: Final = "a paper filing: eFD serves it as scanned page images, which are not read"


@dataclass(frozen=True, slots=True)
class Searched:
    """What a search read."""

    first: date
    last: date
    counted: int
    """``recordsFiltered``: what eFD said the window holds."""
    rows: list[SearchRow] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    """The archive keys of the pages, in order."""
    sources: dict[str, str] = field(default_factory=dict)
    """Each row's doc id → the key of the page that first listed it."""
    stored: Stored = field(default_factory=Stored)

    @property
    def short(self) -> int:
        """Reports counted and not listed on any page."""
        return max(0, self.counted - len(self.rows))


@dataclass(slots=True)
class SenateIngest:
    """What a run did, and where it left the watermark."""

    watermark_before: date | None
    watermark_after: date | None
    searched: Searched | None = None
    reads: list[FilingRead] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(read.outcome is Outcome.FAILED for read in self.reads)

    @property
    def coverage(self) -> Coverage:
        return PtrIngest(ocr=False, reads=self.reads).coverage


# --- which window ----------------------------------------------------------------


async def catch_up_from(session: AsyncSession) -> date:
    """:data:`LOOKBACK_DAYS` before the watermark.

    :raises NoWatermarkError: No run has set it.
    """
    watermark = await read_watermark(session, SENATE_PTRS_JOB)
    if watermark is None:
        raise NoWatermarkError(
            "no run has set a watermark to catch up from: run ingest-senate-ptrs with "
            "--from first, to say where reading starts"
        )
    return max(FIRST_DAY, watermark - timedelta(days=LOOKBACK_DAYS))


async def newest_filing(session: AsyncSession) -> date | None:
    """The newest Senate filing date in the table, up to today's."""
    newest: date | None = await session.scalar(
        select(func.max(CongressFiling.filing_date)).where(CongressFiling.chamber == SENATE)
    )
    return newest


# --- the run -----------------------------------------------------------------------


async def ingest_senate_ptrs(
    sessions: async_sessionmaker[AsyncSession],
    efd: SenateEfd,
    *,
    window: tuple[date, date] | None,
    today: date,
    move_watermark: bool,
    doc_ids: Iterable[str] | None = None,
    reparse: bool = False,
    limit: int | None = None,
) -> SenateIngest:
    """Search ``window`` (unless ``None``), then read every Senate filing pending.

    :param doc_ids: Read only these filings.
    :param reparse: Read every filing chosen again, read or not — but never
        one a person has reviewed a row of. From the archive, fetching only
        what is not there.
    :param limit: Read at most this many, newest first.
    :raises SenateLayoutError: A page not eFD's shape; see the module docstring.
    :raises SenateEfdSessionError: eFD would not keep a session.
    :raises FetcherUnavailable: The fetcher cannot be used.
    :raises FetchRefused: The fetcher declined a request.
    """
    async with sessions() as session:
        before = await read_watermark(session, SENATE_PTRS_JOB)
    ingest = SenateIngest(watermark_before=before, watermark_after=before)

    if window is not None:
        searched = await search(efd, *window)
        async with sessions.begin() as session:
            stored = await store_senate_filings(session, efd, searched)
        ingest.searched = searched = replace(searched, stored=stored)
        logger.info(
            "senate_efd.searched",
            first=searched.first.isoformat(),
            last=searched.last.isoformat(),
            counted=searched.counted,
            listed=len(searched.rows),
            inserted=stored.inserted,
            updated=stored.updated,
        )
        newest = max(
            (row.filing_date for row in searched.rows if row.filing_date <= today), default=None
        )
        moves = newest is not None and (before is None or newest > before)
        if move_watermark and not searched.short and newest is not None and moves:
            async with sessions.begin() as session:
                await _set_watermark(session, newest)
            ingest.watermark_after = newest

    async with sessions() as session:
        filings = await pending_filings(session, doc_ids=doc_ids, reparse=reparse, limit=limit)
    for pending in filings:
        read = await _read_filing(sessions, efd, pending)
        ingest.reads.append(read)
        logger.info(
            "senate_ptr.read",
            doc_id=pending.doc_id,
            outcome=read.outcome.value,
            parsed=read.parsed,
            needs_review=read.needs_review,
            error=read.error,
        )
    return ingest


async def search(efd: SenateEfd, first: date, last: date) -> Searched:
    """Every report filed from ``first`` to ``last``, page by page, each archived.

    :raises SenateLayoutError: A page not eFD's shape, or pages that never end.
    """
    rows: dict[str, SearchRow] = {}
    pages: list[str] = []
    sources: dict[str, str] = {}
    counted: int | None = None
    start = 0
    while True:
        archived = await efd.search_reports(
            submitted_from=first, submitted_to=last, start=start, length=MAX_SEARCH_PAGE
        )
        page = parse_search_page(archived.body, key=archived.key)
        pages.append(archived.key)
        if counted is None:
            counted = page.total
        for row in page.rows:
            if row.doc_id not in rows:
                rows[row.doc_id] = row
                sources[row.doc_id] = archived.key
        start += len(page.rows)
        if not page.rows or start >= page.total:
            break
        # A list that keeps paging past its count is not eFD's DataTables any more.
        if len(pages) > counted // MAX_SEARCH_PAGE + 2:
            raise SenateLayoutError(
                f"the search kept paging: {len(pages)} pages for {counted} reports",
                key=archived.key,
            )
    listed = sorted(rows.values(), key=lambda row: (row.filing_date, row.doc_id))
    return Searched(first, last, counted or 0, listed, pages, sources)


async def store_senate_filings(session: AsyncSession, efd: SenateEfd, searched: Searched) -> Stored:
    """Upsert the search's rows on ``(senate, doc_id)``. Does not commit."""
    if not searched.rows:
        return Stored()
    rows = [_filing_row(efd, row, searched.sources[row.doc_id]) for row in searched.rows]
    return await upsert_filings(session, rows, _CHANGING)


#: What a search read again may change. Not ``source_key``, which says where a
#: row was first seen.
_CHANGING: Final = (
    "report_type",
    "last_name",
    "first_name",
    "office",
    "filing_date",
    "year",
    "report_date",
    "amendment",
    "document_url",
)


def _filing_row(efd: SenateEfd, row: SearchRow, source_key: str) -> dict[str, Any]:
    return {
        "chamber": SENATE,
        "doc_id": row.doc_id,
        "report_type": "ptr",
        "last_name": row.last_name,
        "first_name": row.first_name,
        "prefix": None,
        "suffix": None,
        "office": row.office,
        "filing_date": row.filing_date,
        "year": row.filing_date.year,
        "report_date": row.report_date,
        "amendment": row.amendment,
        "source_key": source_key,
        "document_url": efd.report_url(row.link),
    }


# --- which filings ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SenatePending:
    """A Senate filing to read: :class:`~app.ingestion.congress.ptrs.Pending`
    and the link eFD serves it at."""

    pending: Pending
    url: str

    @property
    def doc_id(self) -> str:
        return self.pending.doc_id

    @property
    def paper(self) -> bool:
        return f"/view/{PAPER}/" in self.url


async def pending_filings(
    session: AsyncSession,
    *,
    doc_ids: Iterable[str] | None = None,
    reparse: bool = False,
    limit: int | None = None,
) -> list[SenatePending]:
    """Senate filings to read, newest filing first: never read, or read by an
    older parser; with ``reparse``, every one. Never one a person has reviewed
    a row of."""
    filing = CongressFiling
    reviewed = exists().where(
        CongressTransaction.filing_id == filing.id, CongressTransaction.reviewed_at.is_not(None)
    )
    query = (
        select(filing.id, filing.doc_id, filing.year, filing.filing_date, filing.document_url)
        .where(filing.chamber == SENATE, filing.document_url.is_not(None), ~reviewed)
        .order_by(filing.filing_date.desc(), filing.id.desc())
    )
    if not reparse:
        query = query.where(
            or_(filing.parser_version.is_(None), filing.parser_version < SENATE_PARSER_VERSION)
        )
    if doc_ids is not None:
        query = query.where(filing.doc_id.in_([doc_id.lower() for doc_id in doc_ids]))
    if limit is not None:
        query = query.limit(limit)
    return [
        SenatePending(Pending(id_, doc_id, year, filed), url)
        for id_, doc_id, year, filed, url in (await session.execute(query)).all()
    ]


# --- reading one ---------------------------------------------------------------------


async def _read_filing(
    sessions: async_sessionmaker[AsyncSession], efd: SenateEfd, chosen: SenatePending
) -> FilingRead:
    pending = chosen.pending
    if chosen.paper:
        document = PtrDocument(Extraction.NONE, pages=0, unreadable=PAPER_REASON)
        async with sessions.begin() as session:
            outcome = await store_ptr(
                session,
                pending.filing_id,
                document,
                url=chosen.url,
                key=None,
                parser_version=SENATE_PARSER_VERSION,
            )
        return FilingRead(pending, outcome, error=PAPER_REASON)

    try:
        archived, document = await _report(efd, chosen)
    except SenateEfdSessionError:
        raise
    except (SenateEfdError, UpstreamUnreachable, RawStoreError) as failure:
        return FilingRead(pending, Outcome.FAILED, error=str(failure))

    async with sessions.begin() as session:
        outcome = await store_ptr(
            session,
            pending.filing_id,
            document,
            url=chosen.url,
            key=archived.key,
            parser_version=SENATE_PARSER_VERSION,
        )
    statuses = [row.status for row in document.rows]
    kept = outcome is Outcome.KEPT
    return FilingRead(
        pending,
        outcome,
        parsed=0 if kept else statuses.count(RowStatus.PARSED),
        needs_review=0 if kept else statuses.count(RowStatus.NEEDS_REVIEW),
        fetched=archived.fetched,
        error=document.unreadable,
    )


async def _report(efd: SenateEfd, chosen: SenatePending) -> tuple[Archived, PtrDocument]:
    """The report's page, read: from the archive, else fetched. A page from
    the archive that will not read is fetched again, and archived over."""
    filed = chosen.pending.filing_date
    archived = await efd.report(chosen.url, archived=True)
    try:
        return archived, parse_report(archived.body, filing_date=filed, key=archived.key).document
    except SenateLayoutError as failure:
        if archived.fetched:
            raise
        logger.warning("senate_ptr.archive_unreadable", key=archived.key, error=str(failure))
    archived = await efd.report(chosen.url, overwrite=True)
    return archived, parse_report(archived.body, filing_date=filed, key=archived.key).document


async def _set_watermark(session: AsyncSession, day: date) -> None:
    insert = pg_insert(CrawlWatermark).values(job=SENATE_PTRS_JOB, last_date=day)
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[CrawlWatermark.job],
            set_={"last_date": insert.excluded.last_date, "updated_at": func.now()},
        )
    )
