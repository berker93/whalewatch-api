"""House PTR PDFs into ``congress_transaction``, a filing at a time.

For each House filing in ``congress_filing`` not yet read by this
:data:`~app.ingestion.congress.ptr.PARSER_VERSION`: get its PDF — from the
archive if it is there, else from the Clerk through the US fetcher, archived
before it is read (:meth:`~app.ingestion.congress.house_clerk.HouseClerk.ptr`)
— read it (:func:`~app.ingestion.congress.ptr_pdf.read_ptr`), and replace the
filing's rows, each filing in a transaction of its own.

What a filing can come to
-------------------------
- ``text`` / ``ocr``: rows read, ``parsed`` or ``needs_review`` each.
- ``placeholder``: no row could be read, and one placeholder row stands in for
  it, with why in ``congress_filing.parse_error``. A scan read when OCR was not
  installed is one, with ``extraction`` ``none``: the next run that has OCR
  reads it again, without being asked to.
- ``missing``: the index lists it and the Clerk has no PDF for it yet. Nothing
  is written but the reason, and the next run tries again.
- ``failed``: the Clerk answered something else, or not at all. Reported, the
  run is partial, and the next run tries again.
- ``kept``: a person has reviewed one of its rows since the run chose it, and
  its rows are left as they are. A reviewed filing is not chosen at all.

The fetcher being unavailable or refusing stops the run, as it does the index's.

Coverage
--------
Each run reports its filings seen, and of those how many have a line
``parsed`` (:attr:`Coverage.read`: what a feed of trades can show), how many
have lines only in review (:attr:`Coverage.review_only`: nothing to show until
a person settles them), how many got a placeholder, and the lines parsed and
left for review. ``coverage`` is filings read over filings seen: about 87%
with OCR off, since about one PTR in eight is a scan, and in the 2026 index
read live with OCR on, 410 of 453. :func:`table_coverage` says the same of the
whole table.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Integer, delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_transaction import CongressTransaction
from app.ingestion.congress.house_clerk import (
    ArchivedPtr,
    HouseClerk,
    HouseClerkError,
    PtrNotFound,
    ptr_url,
)
from app.ingestion.congress.ptr import (
    PARSER_VERSION,
    Extraction,
    PtrDocument,
    PtrPdfError,
    PtrRow,
    RowStatus,
)
from app.ingestion.congress.ptr_ocr import Ocr, OcrError
from app.ingestion.congress.ptr_pdf import read_ptr
from app.ingestion.remote.fetcher import FetcherUnavailable, FetchRefused, UpstreamUnreachable
from app.storage.raw import RawStoreError

logger = get_logger(__name__)

#: The job's name, and the CLI verb.
HOUSE_PTRS_JOB: Final = "ingest-house-ptrs"

HOUSE: Final = "house"

#: Filings read at once: each is a fetch through the fetcher and, for a scan,
#: Tesseract in a thread.
DEFAULT_CONCURRENCY: Final = 4


class Outcome(StrEnum):
    TEXT = "text"
    OCR = "ocr"
    PLACEHOLDER = "placeholder"
    MISSING = "missing"
    FAILED = "failed"
    KEPT = "kept"


@dataclass(frozen=True, slots=True)
class Pending:
    """A filing to read."""

    filing_id: int
    doc_id: str
    year: int
    filing_date: date


@dataclass(frozen=True, slots=True)
class FilingRead:
    """What one filing came to."""

    filing: Pending
    outcome: Outcome
    parsed: int = 0
    needs_review: int = 0
    fetched: bool = False
    """Whether its PDF was fetched by this run, rather than read from the archive."""
    error: str | None = None
    """Why it failed or is missing, or why it has a placeholder."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """Filings seen, and what came of them: a run's, or the table's."""

    seen: int = 0
    read: int = 0
    """With at least one line ``parsed``: in a feed of trades."""
    review_only: int = 0
    """With lines read, none of them parsed yet: all in the review queue."""
    by_text: int = 0
    """Read from the text layer, :attr:`read` or :attr:`review_only`."""
    by_ocr: int = 0
    """Read by OCR, :attr:`read` or :attr:`review_only`."""
    placeholders: int = 0
    missing: int = 0
    failed: int = 0
    rows_parsed: int = 0
    rows_needs_review: int = 0

    @property
    def percent(self) -> Decimal | None:
        """Filings read over filings seen, to a tenth; ``None`` with none seen."""
        if not self.seen:
            return None
        return (Decimal(100 * self.read) / self.seen).quantize(Decimal("0.1"), ROUND_HALF_UP)


@dataclass(slots=True)
class PtrIngest:
    """What a run read."""

    ocr: bool
    """Whether OCR was available to it."""
    reads: list[FilingRead] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(read.outcome is Outcome.FAILED for read in self.reads)

    @property
    def coverage(self) -> Coverage:
        def count(*outcomes: Outcome) -> int:
            return sum(1 for read in self.reads if read.outcome in outcomes)

        lines = [read for read in self.reads if read.outcome in (Outcome.TEXT, Outcome.OCR)]
        return Coverage(
            seen=len(self.reads),
            read=sum(1 for read in lines if read.parsed),
            review_only=sum(1 for read in lines if not read.parsed),
            by_text=count(Outcome.TEXT),
            by_ocr=count(Outcome.OCR),
            placeholders=count(Outcome.PLACEHOLDER),
            missing=count(Outcome.MISSING),
            failed=count(Outcome.FAILED),
            rows_parsed=sum(read.parsed for read in self.reads),
            rows_needs_review=sum(read.needs_review for read in self.reads),
        )


# --- which filings -----------------------------------------------------------------


async def pending_filings(
    session: AsyncSession,
    *,
    ocr: bool,
    years: Iterable[int] | None = None,
    doc_ids: Iterable[str] | None = None,
    reparse: bool = False,
    limit: int | None = None,
) -> list[Pending]:
    """House filings to read, newest filing first.

    Those never read, read by an older parser, or read as a scan without OCR
    when ``ocr`` is now available; with ``reparse``, every one. Never one a
    person has reviewed a row of.
    """
    filing = CongressFiling
    reviewed = exists().where(
        CongressTransaction.filing_id == filing.id, CongressTransaction.reviewed_at.is_not(None)
    )
    query = (
        select(filing.id, filing.doc_id, filing.year, filing.filing_date)
        .where(filing.chamber == HOUSE, ~reviewed)
        .order_by(filing.filing_date.desc(), filing.id.desc())
    )
    if not reparse:
        stale = [filing.parser_version.is_(None), filing.parser_version < PARSER_VERSION]
        if ocr:
            stale.append(filing.extraction == Extraction.NONE.value)
        query = query.where(or_(*stale))
    if years is not None:
        query = query.where(filing.year.in_(list(years)))
    if doc_ids is not None:
        query = query.where(filing.doc_id.in_(list(doc_ids)))
    if limit is not None:
        query = query.limit(limit)
    return [Pending(*row) for row in (await session.execute(query)).all()]


# --- the run -------------------------------------------------------------------------


async def ingest_house_ptrs(
    sessions: async_sessionmaker[AsyncSession],
    clerk: HouseClerk,
    filings: Sequence[Pending],
    *,
    ocr: Ocr | None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> PtrIngest:
    """Read each filing's PDF into its rows, ``concurrency`` at a time.

    :raises FetcherUnavailable: The fetcher cannot be used. Filings already
        read are kept.
    :raises FetchRefused: The fetcher declined a request.
    """
    ingest = PtrIngest(ocr=ocr is not None)
    queue: asyncio.Queue[Pending] = asyncio.Queue()
    for pending in filings:
        queue.put_nowait(pending)
    stop: list[BaseException] = []

    async def worker() -> None:
        while not stop:
            try:
                pending = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                read = await _read_filing(sessions, clerk, pending, ocr=ocr)
            except (FetcherUnavailable, FetchRefused) as failure:
                stop.append(failure)
                return
            ingest.reads.append(read)
            logger.info(
                "house_ptr.read",
                doc_id=pending.doc_id,
                outcome=read.outcome.value,
                parsed=read.parsed,
                needs_review=read.needs_review,
                error=read.error,
            )

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    # In the order chosen, whatever order the workers finished in.
    order = {pending.filing_id: at for at, pending in enumerate(filings)}
    ingest.reads.sort(key=lambda read: order[read.filing.filing_id])
    if stop:
        raise stop[0]
    return ingest


async def _read_filing(
    sessions: async_sessionmaker[AsyncSession],
    clerk: HouseClerk,
    pending: Pending,
    *,
    ocr: Ocr | None,
) -> FilingRead:
    try:
        archived = await clerk.ptr(pending.year, pending.doc_id)
    except PtrNotFound as failure:
        async with sessions.begin() as session:
            await _note_missing(session, pending, str(failure))
        return FilingRead(pending, Outcome.MISSING, error=str(failure))
    except (HouseClerkError, UpstreamUnreachable, RawStoreError) as failure:
        return FilingRead(pending, Outcome.FAILED, error=str(failure))

    opened = True
    try:
        document = await asyncio.to_thread(
            read_ptr, archived.pdf, ocr=ocr, latest=pending.filing_date
        )
    except PtrPdfError as failure:
        # Archived, and linkable: the original is still worth a placeholder.
        opened = False
        document = PtrDocument(Extraction.NONE, pages=0, unreadable=str(failure))
    except OcrError as failure:
        document = PtrDocument(Extraction.OCR, pages=0, unreadable=f"OCR failed: {failure}")
    if document.filing_id is not None and document.filing_id != pending.doc_id:
        document = replace(
            document,
            rows=(),
            unreadable=f"the PDF is Filing ID #{document.filing_id}, not {pending.doc_id}",
        )

    async with sessions.begin() as session:
        outcome = await store_ptr(session, pending, archived, document, opened=opened)
    statuses = [row.status for row in document.rows]
    return FilingRead(
        pending,
        outcome,
        parsed=statuses.count(RowStatus.PARSED) if outcome is not Outcome.KEPT else 0,
        needs_review=statuses.count(RowStatus.NEEDS_REVIEW) if outcome is not Outcome.KEPT else 0,
        fetched=archived.fetched,
        error=document.unreadable,
    )


# --- writing -------------------------------------------------------------------------


async def store_ptr(
    session: AsyncSession,
    pending: Pending,
    archived: ArchivedPtr,
    document: PtrDocument,
    *,
    opened: bool = True,
) -> Outcome:
    """Replace the filing's rows with ``document``'s, or with a placeholder.

    Leaves the filing alone if a person reviewed a row of it after it was
    chosen. Does not commit.
    """
    # The filing locked, so a review cannot land between the check and the write.
    await session.execute(
        select(CongressFiling.id).where(CongressFiling.id == pending.filing_id).with_for_update()
    )
    reviewed = await session.scalar(
        select(
            exists().where(
                CongressTransaction.filing_id == pending.filing_id,
                CongressTransaction.reviewed_at.is_not(None),
            )
        )
    )
    if reviewed:
        return Outcome.KEPT

    await session.execute(
        delete(CongressTransaction).where(CongressTransaction.filing_id == pending.filing_id)
    )
    if document.rows:
        values = [_row(pending.filing_id, row, document.extraction) for row in document.rows]
        outcome = Outcome.OCR if document.extraction is Extraction.OCR else Outcome.TEXT
    else:
        values = [_placeholder(pending.filing_id, document)]
        outcome = Outcome.PLACEHOLDER
    await session.execute(insert(CongressTransaction), values)
    await session.execute(
        update(CongressFiling)
        .where(CongressFiling.id == pending.filing_id)
        .values(
            document_url=archived.url,
            document_key=archived.key,
            pages=document.pages or None,
            extraction=document.extraction.value if opened else None,
            parser_version=PARSER_VERSION,
            parsed_at=func.now(),
            parse_error=None if document.rows else document.unreadable,
        )
    )
    return outcome


async def _note_missing(session: AsyncSession, pending: Pending, reason: str) -> None:
    await session.execute(
        update(CongressFiling)
        .where(CongressFiling.id == pending.filing_id)
        .values(document_url=ptr_url(pending.year, pending.doc_id), parse_error=reason)
    )


def _row(filing_id: int, row: PtrRow, extraction: Extraction) -> dict[str, Any]:
    return {
        "filing_id": filing_id,
        "line": row.line,
        "page": row.page,
        "parse_status": row.status.value,
        "extraction": extraction.value,
        "owner": row.owner,
        "asset_name": row.asset_name,
        "ticker": row.ticker,
        "asset_type": row.asset_type,
        "transaction_type": row.transaction_type.value if row.transaction_type else None,
        "transaction_date": row.transaction_date,
        "notification_date": row.notification_date,
        "amount_low": row.amount_low,
        "amount_high": row.amount_high,
        "amount_exact": row.amount_exact,
        "cap_gains_over_200": row.cap_gains_over_200,
        "filing_status": row.filing_status,
        "amended_id": row.amended_id,
        "subholding_of": row.subholding_of,
        "description": row.description,
        "comment": row.comment,
        "location": row.location,
        "confidence": row.confidence,
        "problems": list(row.problems),
        "raw_text": row.raw_text,
    }


def _placeholder(filing_id: int, document: PtrDocument) -> dict[str, Any]:
    reason = document.unreadable or "no row could be read"
    return {
        "filing_id": filing_id,
        "line": 1,
        "page": None,
        "parse_status": RowStatus.PLACEHOLDER.value,
        "extraction": document.extraction.value,
        "problems": [reason],
        "raw_text": "",
    }


# --- the table's coverage ------------------------------------------------------------


async def table_coverage(session: AsyncSession, chamber: str = HOUSE) -> Coverage:
    """:class:`Coverage` of every filing of ``chamber`` read so far: those with a
    PDF missing count as seen and missing; those not tried yet, not at all."""
    filing, row = CongressFiling, CongressTransaction
    rows = (
        select(
            row.filing_id,
            func.count().filter(row.parse_status == RowStatus.PARSED.value).label("parsed"),
            func.count()
            .filter(row.parse_status == RowStatus.NEEDS_REVIEW.value)
            .label("needs_review"),
            func.count().filter(row.parse_status == RowStatus.PLACEHOLDER.value).label("holder"),
        )
        .group_by(row.filing_id)
        .subquery()
    )
    tried = or_(filing.parser_version.is_not(None), filing.parse_error.is_not(None))
    lines = rows.c.holder == 0
    counts = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(lines, rows.c.parsed > 0),
                func.count().filter(lines, rows.c.parsed == 0),
                func.count().filter(filing.extraction == Extraction.TEXT.value, lines),
                func.count().filter(filing.extraction == Extraction.OCR.value, lines),
                func.count().filter(rows.c.holder > 0),
                func.count().filter(filing.parser_version.is_(None)),
                func.coalesce(func.sum(rows.c.parsed), 0).cast(Integer),
                func.coalesce(func.sum(rows.c.needs_review), 0).cast(Integer),
            )
            .select_from(filing)
            .outerjoin(rows, rows.c.filing_id == filing.id)
            .where(filing.chamber == chamber, tried)
        )
    ).one()
    seen, read, review_only, by_text, by_ocr, placeholders, missing, parsed, review = counts
    return Coverage(
        seen=seen,
        read=read,
        review_only=review_only,
        by_text=by_text,
        by_ocr=by_ocr,
        placeholders=placeholders,
        missing=missing,
        rows_parsed=parsed,
        rows_needs_review=review,
    )
