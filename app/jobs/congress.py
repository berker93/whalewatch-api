"""The Congress jobs: the House Clerk's indexes and the PTRs they list, Senate
eFD's, and the member registry the filers are matched to.

``ingest-house-index``, ``ingest-house-ptrs``, ``ingest-senate-ptrs``,
``seed-congress-members`` and ``match-congress-members`` are these with
argument parsing and a summary; the tasks of the same names are these in
``asyncio.run``. Both index jobs end by linking what they stored to filed
names and matching the new names to members
(:func:`~app.ingestion.congress.members.link_and_match`). As with the Form 4 jobs
(app/jobs/form4.py), nothing here prints.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
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
from app.ingestion.congress.legislators import (
    FILES,
    Copy,
    Dataset,
    archive,
    fetch_dataset,
    read_dataset,
)
from app.ingestion.congress.members import MemberPass, MembersStored, link_and_match, store_members
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
from app.ingestion.congress.senate_efd import SenateEfd
from app.ingestion.congress.senate_ptr import SenateLayoutError
from app.ingestion.congress.senate_ptrs import (
    QUIET_DAYS,
    SenateIngest,
    catch_up_from,
    ingest_senate_ptrs,
    newest_filing,
)
from app.ingestion.remote.fetcher import open_fetcher
from app.jobs.tracking import TrackedRun, tracked_task
from app.ops.alerts import alert_ops
from app.storage.raw import RawStore, open_raw_store

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HouseIndexRun:
    """What one run did. ``ingest`` is ``None`` when there was nothing to read."""

    years: list[int]
    ingest: HouseIngest | None
    members: MemberPass | None = None
    """The filings stored, linked to names and the new names matched."""

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
        async with sessions.begin() as session:
            members = await link_and_match(session)
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
    } | member_metrics(members)
    return HouseIndexRun(years=years, ingest=ingest, members=members)


def member_metrics(members: MemberPass) -> dict[str, Any]:
    """What linking and matching did, for a run's ``metrics``."""
    matched = members.matched
    return {
        "filings_linked": members.linked.filings,
        "names_new": len(members.linked.new_names),
        "names_matched": sum(matched.tally.matched.values()),
        "names_review": matched.tally.review,
        "names_unmatched": matched.tally.unmatched,
    }


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


# --- Senate ----------------------------------------------------------------------

#: The fewest seconds between two requests to eFD.
EFD_INTERVAL_SECONDS = 1.0

#: The ops alerts the Senate job raises (app/ops/alerts.py), each once a day at most.
EFD_LAYOUT_ALERT = "senate-efd-layout"
EFD_QUIET_ALERT = "senate-efd-quiet"


@dataclass(frozen=True, slots=True)
class SenatePtrRun:
    """What one run did, and the Senate's coverage after it."""

    window: tuple[date, date] | None
    ingest: SenateIngest
    table: Coverage
    members: MemberPass
    quiet_days: int | None
    """Days since the newest Senate filing, when a catch-up found it past
    :data:`~app.ingestion.congress.senate_ptrs.QUIET_DAYS`."""


def _senate_context(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    first, last = arguments.get("first"), arguments.get("last")
    return {
        "catch_up": first is None and arguments.get("search", True),
        "first": first,
        "last": last,
        "search": arguments.get("search", True),
        "doc_ids": arguments.get("doc_ids"),
        "limit": arguments.get("limit"),
        "reparse": arguments.get("reparse", False),
    }


@tracked_task("ingest-senate-ptrs", context=_senate_context)
async def ingest_senate_ptrs_job(
    run: TrackedRun,
    settings: Settings,
    *,
    today: date,
    first: date | None = None,
    last: date | None = None,
    search: bool = True,
    doc_ids: list[str] | None = None,
    limit: int | None = None,
    reparse: bool = False,
) -> SenatePtrRun:
    """Search eFD for the window's PTRs, then read every Senate filing not read yet.

    :param first: The first filing date to search, or ``None`` for a catch-up
        from the watermark: what the scheduled task does.
    :param last: The last, or ``None`` for today. A window that reaches today
        moves the watermark; one that ends before it does not.
    :param search: ``False`` to only read filings already listed: a re-parse.
    :param today: Today in New York.
    :raises SenateLayoutError: eFD changed the shape of a page. Ops are told.
    :raises NoWatermarkError: A catch-up with no watermark to start from.
    :raises FetcherUnavailable: No fetcher, or one that cannot be used.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    window: tuple[date, date] | None = None
    try:
        if search:
            if first is None:
                async with sessions() as session:
                    first = await catch_up_from(session)
            window = (first, min(last or today, today))
        catch_up = search and (last is None or last >= today)
        async with open_fetcher(settings) as fetcher, open_raw_store(settings) as store:
            efd = SenateEfd(
                fetcher, store, user_agent=settings.sec_user_agent, interval=EFD_INTERVAL_SECONDS
            )
            try:
                ingest = await ingest_senate_ptrs(
                    sessions,
                    efd,
                    window=window,
                    today=today,
                    move_watermark=catch_up,
                    doc_ids=doc_ids,
                    reparse=reparse,
                    limit=limit,
                )
            except SenateLayoutError as failure:
                await alert_ops(
                    settings,
                    EFD_LAYOUT_ALERT,
                    f"Senate eFD changed its layout, and ingest-senate-ptrs stopped rather "
                    f"than read no rows: {failure}. Fix app/ingestion/congress/senate_ptr.py "
                    f"against the archived page, then run ingest-senate-ptrs --catch-up.",
                )
                raise
        async with sessions.begin() as session:
            members = await link_and_match(session)
        async with sessions() as session:
            table = await table_coverage(session, "senate")
            newest = await newest_filing(session)
    finally:
        await engine.dispose()

    quiet_days = None
    if catch_up and newest is not None and (today - newest).days > QUIET_DAYS:
        quiet_days = (today - newest).days
        message = (
            f"no Senate PTR filed in {quiet_days} days (newest {newest}): eFD may be "
            f"answering the search with nothing"
        )
        run.errors.append(message)
        await alert_ops(settings, EFD_QUIET_ALERT, f"ingest-senate-ptrs: {message}.")

    searched = ingest.searched
    coverage = ingest.coverage
    if searched is not None and searched.short:
        run.errors.append(
            f"the search counted {searched.counted} reports and listed {len(searched.rows)}: "
            "the watermark stays, and the next run reads the window again"
        )
    run.errors.extend(
        f"{read.filing.doc_id}: {read.error}"
        for read in ingest.reads
        if read.outcome is Outcome.FAILED
    )
    run.items_seen = coverage.seen
    run.items_written = coverage.read + coverage.review_only + coverage.placeholders
    run.metrics |= {
        "window": None if window is None else f"{window[0]}..{window[1]}",
        "search_pages": 0 if searched is None else len(searched.pages),
        "listed": 0 if searched is None else len(searched.rows),
        "inserted": 0 if searched is None else searched.stored.inserted,
        "updated": 0 if searched is None else searched.stored.updated,
        "filings_seen": coverage.seen,
        "filings_read": coverage.read,
        "review_only": coverage.review_only,
        "placeholders": coverage.placeholders,
        "failed": coverage.failed,
        "rows_parsed": coverage.rows_parsed,
        "rows_needs_review": coverage.rows_needs_review,
        "fetched": sum(1 for read in ingest.reads if read.fetched),
        "coverage_pct": _number(coverage.percent),
        "table_coverage_pct": _number(table.percent),
        "watermark_before": _iso(ingest.watermark_before),
        "watermark_after": _iso(ingest.watermark_after),
        "quiet_days": quiet_days,
    } | member_metrics(members)
    return SenatePtrRun(
        window=window, ingest=ingest, table=table, members=members, quiet_days=quiet_days
    )


# --- members ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedRun:
    """What a seed read and stored, and the pass over every name after it."""

    dataset: Dataset
    stored: MembersStored
    members: MemberPass


def _seed_context(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    directory = arguments.get("directory")
    return {"directory": None if directory is None else str(directory)}


@tracked_task("seed-congress-members", context=_seed_context)
async def seed_congress_members_job(
    run: TrackedRun, settings: Settings, *, directory: Path | None = None
) -> SeedRun:
    """Seed ``congress_member`` from the congress-legislators dataset, then
    match every filed name a person has not.

    :param directory: Read ``legislators-current.json`` and
        ``legislators-historical.json`` from here instead of downloading them.
        Archived like a download.
    :raises LegislatorsError: A file could not be fetched or read. Nothing is
        written.
    """
    async with open_raw_store(settings) as store:
        if directory is None:
            copies = await fetch_dataset(
                store,
                base_url=settings.congress_legislators_url,
                user_agent=settings.sec_user_agent,
            )
        else:
            copies = await _copies_from(store, directory)
    dataset = read_dataset(copies)

    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with sessions.begin() as session:
            stored = await store_members(session, dataset)
            members = await link_and_match(session, every_name=True)
    finally:
        await engine.dispose()

    run.items_seen = dataset.listed
    run.items_written = stored.inserted + stored.updated
    run.metrics |= {
        "kept": len(dataset.legislators),
        "inserted": stored.inserted,
        "updated": stored.updated,
        "terms": stored.terms,
        "raw_keys": [copy.key for copy in copies],
        "names_changed": members.matched.changed,
    } | member_metrics(members)
    return SeedRun(dataset=dataset, stored=stored, members=members)


async def _copies_from(store: RawStore, directory: Path) -> tuple[Copy, ...]:
    copies: list[Copy] = []
    for name in FILES:
        path = directory / f"{name}.json"
        body = await asyncio.to_thread(path.read_bytes)
        copies.append(await archive(store, name, body, url=str(path)))
    return tuple(copies)


@tracked_task("match-congress-members")
async def match_congress_members_job(run: TrackedRun, settings: Settings) -> MemberPass:
    """Link every unlinked filing, and match every filed name a person has not,
    against the registry as it is: after a matcher change, say."""
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with sessions.begin() as session:
            members = await link_and_match(session, every_name=True)
    finally:
        await engine.dispose()
    run.items_seen = members.matched.names
    run.items_written = members.matched.changed
    run.metrics |= {"names_changed": members.matched.changed} | member_metrics(members)
    return members
