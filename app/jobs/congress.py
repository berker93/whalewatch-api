"""The Congress jobs: the House Clerk's indexes and the PTRs they list.

``ingest-house-index`` and ``ingest-house-ptrs`` are these with argument
parsing and a summary; the ``ingest_house_index`` and ``ingest_house_ptrs``
tasks are these in ``asyncio.run``. As with the Form 4 jobs
(app/jobs/form4.py), nothing here prints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import create_engine, create_session_factory
from app.ingestion.congress.filings import (
    HouseIngest,
    YearKind,
    catch_up_years,
    ingest_house_index,
)
from app.ingestion.congress.house_clerk import HouseClerk
from app.ingestion.congress.ptr_ocr import Ocr, Tesseract
from app.ingestion.congress.ptrs import (
    DEFAULT_CONCURRENCY,
    Coverage,
    Outcome,
    PtrIngest,
    ingest_house_ptrs,
    pending_filings,
    table_coverage,
)
from app.ingestion.remote.fetcher import open_fetcher
from app.jobs.tracking import TrackedRun, tracked_task
from app.storage.raw import open_raw_store

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HouseIndexRun:
    """What one run did. ``ingest`` is ``None`` when there was nothing to read."""

    years: list[int]
    ingest: HouseIngest | None

    @property
    def failed(self) -> bool:
        return self.ingest is not None and self.ingest.failed


def _context(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    years: list[int] | None = arguments["years"]
    return {
        "years": None if not years else f"{years[0]}..{years[-1]}",
        "catch_up": years is None,
        "move_watermark": years is None or arguments["move_watermark"],
    }


@tracked_task("ingest-house-index", context=_context)
async def ingest_house_index_job(
    run: TrackedRun,
    settings: Settings,
    *,
    years: list[int] | None,
    today: date,
    move_watermark: bool = True,
) -> HouseIndexRun:
    """Read the years' indexes into ``congress_filing``, through the US fetcher.

    :param years: The years to read, or ``None`` for a catch-up from the
        watermark: what the daily task does. A catch-up always moves the
        watermark; given years move it only with ``move_watermark``, which a
        sweep to today's year sets and a single year does not.
    :param today: Today in New York, for which year is the current one.
    :raises NoWatermarkError: A catch-up with no watermark to start from.
    :raises FetcherUnavailable: No fetcher, or one that cannot be used.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        if years is None:
            async with sessions() as session:
                years = await catch_up_years(session, today)
            move_watermark = True
        if not years:
            return HouseIndexRun(years=[], ingest=None)
        async with open_fetcher(settings) as fetcher, open_raw_store(settings) as store:
            clerk = HouseClerk(fetcher, store, user_agent=settings.sec_user_agent)
            ingest = await ingest_house_index(
                sessions, clerk, years, today=today, move_watermark=move_watermark
            )
    finally:
        await engine.dispose()

    read = [year for year in ingest.years if year.kind is YearKind.READ]
    run.items_seen = sum(len(year.parsed.ptrs) for year in read if year.parsed is not None)
    run.items_written = sum(year.stored.inserted + year.stored.updated for year in read)
    for year in ingest.years:
        if year.kind is YearKind.FAILED:
            run.errors.append(f"{year.year}: {year.error}")
        if year.parsed is not None:
            run.errors.extend(
                f"{year.year} row {bad.position} ({bad.doc_id}): {bad.reason}"
                for bad in year.parsed.malformed
            )
    run.metrics |= {
        "years_read": len(read),
        "inserted": sum(year.stored.inserted for year in read),
        "updated": sum(year.stored.updated for year in read),
        "archived": sum(1 for year in read if year.index is not None and year.index.new),
        "watermark_before": _iso(ingest.watermark_before),
        "watermark_after": _iso(ingest.watermark_after),
    }
    return HouseIndexRun(years=years, ingest=ingest)


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


# --- PTRs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HousePtrRun:
    """What one run did, and the table's coverage after it."""

    ingest: PtrIngest
    table: Coverage


def _number(percent: Decimal | None) -> float | None:
    """A percentage to a tenth, as ``metrics`` (jsonb) stores it."""
    return None if percent is None else float(percent)


def open_ocr(settings: Settings) -> Ocr | None:
    """Tesseract, when ``PTR_OCR`` asks for it and it is installed."""
    if settings.ptr_ocr == "none":
        return None
    engine = Tesseract.find(settings.tesseract_command)
    if engine is None:
        logger.warning("house_ptr.no_ocr", command=settings.tesseract_command)
    return engine


def _ptr_context(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "years": arguments.get("years"),
        "doc_ids": arguments.get("doc_ids"),
        "limit": arguments.get("limit"),
        "reparse": arguments.get("reparse", False),
    }


@tracked_task("ingest-house-ptrs", context=_ptr_context)
async def ingest_house_ptrs_job(
    run: TrackedRun,
    settings: Settings,
    *,
    years: list[int] | None = None,
    doc_ids: list[str] | None = None,
    limit: int | None = None,
    reparse: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> HousePtrRun:
    """Read the PDFs of House filings not yet read into ``congress_transaction``.

    :param years: Only filings in these index years.
    :param doc_ids: Only these filings.
    :param limit: At most this many, newest filing first.
    :param reparse: Every filing chosen, read or not — but never one a person
        has reviewed a row of.
    :raises FetcherUnavailable: No fetcher, or one that cannot be used, and a
        PDF not archived yet.
    """
    ocr = open_ocr(settings)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            filings = await pending_filings(
                session,
                ocr=ocr is not None,
                years=years,
                doc_ids=doc_ids,
                reparse=reparse,
                limit=limit,
            )
        ingest = PtrIngest(ocr=ocr is not None)
        if filings:  # nothing to read needs no fetcher
            async with open_fetcher(settings) as fetcher, open_raw_store(settings) as store:
                clerk = HouseClerk(fetcher, store, user_agent=settings.sec_user_agent)
                ingest = await ingest_house_ptrs(
                    sessions, clerk, filings, ocr=ocr, concurrency=concurrency
                )
        async with sessions() as session:
            table = await table_coverage(session)
    finally:
        await engine.dispose()

    coverage = ingest.coverage
    run.items_seen = coverage.seen
    run.items_written = coverage.read + coverage.review_only + coverage.placeholders
    run.errors.extend(
        f"{read.filing.doc_id}: {read.error}"
        for read in ingest.reads
        if read.outcome is Outcome.FAILED
    )
    run.metrics |= {
        "ocr": ingest.ocr,
        "filings_seen": coverage.seen,
        "filings_read": coverage.read,
        "review_only": coverage.review_only,
        "by_text": coverage.by_text,
        "by_ocr": coverage.by_ocr,
        "placeholders": coverage.placeholders,
        "missing": coverage.missing,
        "failed": coverage.failed,
        "rows_parsed": coverage.rows_parsed,
        "rows_needs_review": coverage.rows_needs_review,
        "coverage_pct": _number(coverage.percent),
        "fetched": sum(1 for read in ingest.reads if read.fetched),
        "table_coverage_pct": _number(table.percent),
    }
    return HousePtrRun(ingest=ingest, table=table)
