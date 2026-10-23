"""The Form 4 jobs: EDGAR's daily index into the queue, and the queue into the database.

``crawl-form4`` and ``ingest-form4`` are these with argument parsing and a
summary; the ``crawl_form4_daily`` and ``ingest_form4`` tasks are these in
``asyncio.run``. As with the 13F jobs (app/jobs/thirteen_f.py), neither
prints, and a run by hand passes a :class:`Form4Progress` to hear how it goes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Any, Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.partitions import CreatedPartition, ensure_partitions, next_year
from app.db.session import create_engine, create_session_factory, session_scope
from app.derived.insider_cluster import ClusterRebuild, detect_clusters
from app.ingestion.archive import archive_form4_documents, read_form4_documents
from app.ingestion.backfill import (
    BackfillPlan,
    BackfillRun,
    FilingResult,
    Outcome,
    PlannedFiling,
    record_attempt,
    run_backfill,
)
from app.ingestion.discovery import mark_ingested
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.form4_documents import fetch_form4_documents
from app.ingestion.insider_discovery import (
    Crawl,
    DayKind,
    Universe,
    catch_up_days,
    crawl_form4,
    held_universe,
    sp500_universe,
)
from app.ingestion.insider_ingest import plan_form4
from app.ingestion.loaders import Form4LoadResult, load_form4
from app.ingestion.parsers.form4 import Form4Document, parse_form4
from app.jobs.cache import invalidate_cache
from app.jobs.errors import JobError
from app.jobs.thirteen_f import MAX_CONCURRENCY, Shared, directory_url, interruptible
from app.jobs.tracking import TrackedRun, tracked_task, without_progress
from app.storage.raw import open_raw_store

logger = get_logger(__name__)

#: The kinds of day that count as read, and move the watermark.
READ_DAYS: Final = frozenset({DayKind.INDEXED, DayKind.WEEKEND, DayKind.NO_INDEX})


# --- crawl -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Crawled:
    """What one crawl did. Both ``None`` when the watermark was already today."""

    universe: Universe | None
    crawl: Crawl | None

    @property
    def failed(self) -> bool:
        """A day failed: the days before it are kept, and the next run starts there."""
        return self.crawl is not None and self.crawl.failed


def _crawl_context(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """The days as a range, not a list: ``--from 2020-01-01`` is two thousand of them."""
    days: list[date] | None = arguments["days"]
    return {
        "days": None if days is None else f"{days[0]}..{days[-1]}",
        "catch_up": days is None,
        "universe": arguments["universe"] or settings.form4_universe,
    }


@tracked_task("crawl-form4", context=_crawl_context)
async def crawl_form4_daily(
    run: TrackedRun,
    settings: Settings,
    *,
    days: list[date] | None,
    universe: str | None,
    today: date,
) -> Crawled:
    """Choose the days, build the universe, crawl the daily index into ``pending_filing``.

    The days before the universe, so that a catch-up with no watermark fails
    without a request to SEC.

    :param days: The days to read, or ``None`` for every day since the
        watermark: what the daily task does.
    :param universe: ``held`` or ``sp500``; ``None`` for ``settings.form4_universe``.
    :param today: Today in New York, past which there is no index.
    :raises NoWatermarkError: A catch-up with no watermark to start from.
    :raises UniverseError: The universe cannot be built, or is empty.
    """
    kind = universe or settings.form4_universe
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        if days is None:
            async with sessions() as session:
                days = await catch_up_days(session, today)
        if not days:
            return Crawled(universe=None, crawl=None)
        async with EdgarClient(settings) as edgar:
            if kind == "held":
                chosen = await held_universe(
                    sessions, edgar, min_weight_pct=settings.form4_min_weight_pct
                )
            else:
                chosen = sp500_universe(settings.form4_sp500_path)
            crawl = await crawl_form4(sessions, edgar, chosen.ciks, days, today=today)
    finally:
        await engine.dispose()

    run.items_seen = sum(len(crawled.matched) for crawled in crawl.days)
    run.items_written = sum(crawled.new for crawled in crawl.days)
    if crawl.failed and crawl.stopped is not None:
        run.errors.append(f"{crawl.stopped.day}: {crawl.stopped.error}")
    run.metrics |= {
        "universe_issuers": len(chosen.ciks),
        "days_read": sum(1 for crawled in crawl.days if crawled.kind in READ_DAYS),
        "watermark_before": _iso(crawl.watermark_before),
        "watermark_after": _iso(crawl.watermark_after),
        "stopped": None if crawl.stopped is None else crawl.stopped.kind.value,
    }
    return Crawled(universe=chosen, crawl=crawl)


def _iso(day: date | None) -> str:
    return "" if day is None else day.isoformat()


# --- ingest ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Form4Loaded:
    """What one filing's load did, for its progress line and the summary."""

    document: Form4Document
    result: Form4LoadResult


@dataclass(frozen=True, slots=True)
class Form4Progress:
    """What a run by hand is told as it goes, to print. A task passes none."""

    partitions_created: Callable[[list[CreatedPartition]], None]
    """Before the plan, if the run had to create any."""

    planned: Callable[[BackfillPlan], None]

    finished: Callable[[FilingResult, Form4Loaded | None], None]
    """As each filing finishes, with what it loaded, ``None`` if it failed."""

    stopping: Callable[[], None]


@dataclass(frozen=True, slots=True)
class Form4Ingested:
    """What one run of the Form 4 queue did."""

    plan: BackfillPlan
    ran: BackfillRun
    loaded: dict[str, Form4Loaded]
    """Each filing loaded, by accession number."""

    clusters: ClusterRebuild | None
    """The cluster rebuild after the loads; ``None`` if nothing loaded."""

    @property
    def failed(self) -> bool:
        return self.ran.rate_limited is not None or bool(self.ran.count(Outcome.FAILED))

    @property
    def not_started(self) -> int:
        return self.ran.count(Outcome.NOT_STARTED)


@tracked_task("ingest-form4", context=without_progress)
async def ingest_form4(
    run: TrackedRun,
    settings: Settings,
    *,
    concurrency: int,
    limit: int | None,
    force: bool,
    progress: Form4Progress | None = None,
) -> Form4Ingested:
    """Create any missing partition, plan, load, then rebuild the clusters.

    No snapshot or view reads these tables, so there is nothing to refresh.
    ``insider_cluster`` is derived from them, so a run that loaded anything
    rebuilds it, in a transaction of its own after the loads, and then drops
    the API's cache: the insider feed reads the lines live.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency is 1 to {MAX_CONCURRENCY}, not {concurrency}")
    loaded: dict[str, Form4Loaded] = {}
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    ran = BackfillRun(results=(), rate_limited=None)
    try:
        # A transaction of its own: creating a partition blocks reads of the
        # table until it commits, so it is not held through the run.
        async with sessions.begin() as session:
            created = await ensure_partitions(session, through=next_year())
        if created:
            run.metrics["partitions_created"] = partition_metrics(created)
            if progress is not None:
                progress.partitions_created(created)
        async with sessions.begin() as session:
            plan = await plan_form4(session, force=force, limit=limit)
        if progress is not None:
            progress.planned(plan)
        run.items_seen = len(plan.work)

        if plan.work:
            async with open_raw_store(settings) as store, EdgarClient(settings) as edgar:
                shared = Shared(sessions=sessions, store=store, edgar=edgar)
                with interruptible(None if progress is None else progress.stopping) as stop:
                    ran = await run_backfill(
                        plan.work,
                        process=partial(_ingest_form4_filing, shared, loaded),
                        on_failure=partial(record_attempt, sessions),
                        on_result=_reporter(progress, loaded),
                        concurrency=concurrency,
                        stop=stop,
                    )
    finally:
        await engine.dispose()

    run.items_written = ran.count(Outcome.OK)
    run.errors.extend(
        f"{result.filing.accession_no}: {result.error}"
        for result in ran.results
        if result.outcome is Outcome.FAILED
    )
    not_started = ran.count(Outcome.NOT_STARTED)
    if not_started:
        run.errors.append(f"stopped with {not_started} not started; re-run to resume")
    results = [entry.result for entry in loaded.values()]
    run.metrics |= {
        "lines_written": sum(result.lines_written for result in results),
        "lines_superseded": sum(result.lines_superseded for result in results),
        "security_links": dict(Counter(result.security_link.value for result in results)),
        "amendments": dict(
            Counter(result.amendment.value for result in results if result.amendment)
        ),
    }
    clusters = None
    if loaded:
        clusters = await rebuild_clusters(settings)
        run.metrics["clusters"] = cluster_metrics(clusters)
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    return Form4Ingested(plan=plan, ran=ran, loaded=loaded, clusters=clusters)


def _reporter(
    progress: Form4Progress | None, loaded: dict[str, Form4Loaded]
) -> Callable[[FilingResult], None]:
    def report(result: FilingResult) -> None:
        if progress is not None:
            progress.finished(result, loaded.get(result.filing.accession_no))

    return report


async def _ingest_form4_filing(
    shared: Shared, loaded: dict[str, Form4Loaded], filing: PlannedFiling
) -> FilingResult:
    """One Form 4: fetched and archived, or read back from the archive when
    it is loaded already (``force``), then parsed and loaded, replacing what
    an earlier load wrote in the second case."""
    accession = filing.accession_no
    if filing.loaded is None:
        documents = await fetch_form4_documents(
            shared.edgar, cik=filing.cik, accession_no=accession
        )
        raw_prefix = await archive_form4_documents(
            shared.store, documents, cik=filing.cik, accession_no=accession
        )
    else:
        if filing.loaded.raw_key is None:
            raise JobError(f"{accession} was loaded without its documents archived")
        raw_prefix = filing.loaded.raw_key
        documents = await read_form4_documents(
            shared.store, raw_prefix, directory_url=directory_url(filing)
        )

    document = parse_form4(documents.document)
    async with shared.sessions.begin() as session:
        result = await load_form4(
            session,
            accession_no=accession,
            filed_at=documents.filed_at,
            document=document,
            raw_key=raw_prefix,
            source_url=documents.document_url,
            replace=filing.loaded is not None,
        )
        await mark_ingested(session, accession)
    logger.info(
        "form4.ingested",
        filing_id=result.filing_id,
        issuer_cik=document.issuer.cik,
        lines=result.lines,
        lines_written=result.lines_written,
        security_link=result.security_link.value,
        superseded_by=result.superseded_by,
    )
    loaded[accession] = Form4Loaded(document=document, result=result)
    return FilingResult(filing=filing, outcome=Outcome.OK, period=None, rows=result.lines)


# --- what ingest-form4, detect-clusters and create-partitions share ----------


async def rebuild_clusters(settings: Settings) -> ClusterRebuild:
    """``insider_cluster`` rebuilt whole from every line, in one transaction."""
    async with session_scope(settings) as session:
        clusters = await detect_clusters(session)
    logger.info(
        "insider_cluster.detected",
        episodes=clusters.episodes,
        added=len(clusters.added),
        removed=len(clusters.removed),
        lines=dict(clusters.lines),
    )
    return clusters


def cluster_metrics(clusters: ClusterRebuild) -> dict[str, object]:
    return {
        "episodes": clusters.episodes,
        "added": len(clusters.added),
        "removed": len(clusters.removed),
        "lines": dict(clusters.lines),
    }


def partition_metrics(created: list[CreatedPartition]) -> dict[str, int]:
    return {partition.name: partition.rows_moved for partition in created}
