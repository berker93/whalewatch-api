"""The 13F jobs: tracked filers' filings into the queue, and the queue into the database.

``discover-filings`` and ``backfill`` are these, wrapped in argument parsing
and a summary; the ``discover_13f_filings`` and ``ingest_pending_filings``
tasks are these, wrapped in ``asyncio.run``. Neither does anything the other
does not, which is the point: a backfill that misbehaves at 3am is re-run by
hand as the same code. The steps one filing goes through — fetch, archive,
parse, load and publish — are here too, because ``ingest-filing`` shares them.

Neither job prints. What a person reads is built by the CLI from what the job
returns, and from :class:`Progress`, which a backfill run by hand is given so
it can say how far along it is. A task gives none, and so also installs no
Ctrl-C handler: a worker's signals are Celery's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.filing import ParseStatus
from app.db.session import create_engine, create_session_factory
from app.derived.recompute import Recomputed, hold_recompute_lock, recompute
from app.derived.scope import Pair, Scope, filing_pairs
from app.ingestion.archive import archive_13f_documents, read_13f_documents
from app.ingestion.backfill import (
    BackfillPlan,
    BackfillRun,
    FilingResult,
    Outcome,
    PlannedFiling,
    plan_backfill,
    record_attempt,
    run_backfill,
    stop_on_interrupt,
)
from app.ingestion.discovery import (
    FilerDiscovery,
    default_since,
    discover_filings,
    mark_ingested,
    tracked_filer_ids,
)
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.documents import FilingDocuments, fetch_13f_documents
from app.ingestion.edgar.submissions import Submission, find_submission
from app.ingestion.loaders import LoadResult, load_filing
from app.ingestion.normalisation import NormalisedFiling, normalise_filing
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
    parse_information_table,
    parse_primary_doc,
)
from app.jobs.cache import invalidate_cache
from app.jobs.errors import JobError
from app.jobs.tracking import TrackedRun, tracked_task, without_progress
from app.storage.raw import RawStore, open_raw_store

logger = get_logger(__name__)

#: The form types this pipeline knows how to parse. Checked against EDGAR's own
#: word for what the submission is, before anything is fetched from the filing
#: directory, because the 13F parser applied to a Form 4 does not fail — it
#: finds no ``<infoTable>`` elements and yields an empty portfolio.
THIRTEEN_F_FORMS: Final = ("13F-HR", "13F-NT")

#: The most filings in flight at once. Bounded by the database rather than by
#: EDGAR: every worker writes through the run's one engine, whose pool is five
#: connections plus ten overflow, and a worker past that waits out
#: ``pool_timeout`` and fails its filing. EDGAR stopped rewarding concurrency
#: well before this, since every worker draws on the one rate limiter.
MAX_CONCURRENCY: Final = 15

DEFAULT_CONCURRENCY: Final = 5


# --- discovery ---------------------------------------------------------------


def discovery_cutoff(since: date | None, *, full_history: bool, today: date) -> date | None:
    """The earliest filing date to look at: ``since``, five years back, or ``None`` for all."""
    if full_history:
        return None
    return since if since is not None else default_since(today)


@tracked_task("discover-filings")
async def discover_13f_filings(
    run: TrackedRun, settings: Settings, *, filer: str | None, since: date | None
) -> list[FilerDiscovery]:
    """Queue every 13F a tracked filer has filed and we have not loaded, filer by filer.

    One engine for the run rather than a ``session_scope`` per filer: each of
    those builds and disposes an engine, and this opens two sessions per filer
    across a hundred filers.

    Counted filer by filer, so a run that EDGAR blocks partway records what the
    filers before the block found and queued, which are committed. A CIK that
    could not be read is a line of the run's errors, and the run goes on.

    :param filer: One filer's slug, or ``None`` for every tracked filer.
    :param since: The earliest filing date, or ``None`` for the full history:
        :func:`discovery_cutoff`.
    :raises UnknownFilerError: ``filer`` names no filer.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    results: list[FilerDiscovery] = []
    try:
        async with sessions() as session:
            filer_ids = await tracked_filer_ids(session, slug=filer)
        async with EdgarClient(settings) as edgar:
            for filer_id in filer_ids:
                result = await discover_filings(sessions, edgar, filer_id, since=since)
                results.append(result)
                run.items_seen += len(result.found)
                run.items_written += len(result.new)
                run.errors.extend(
                    f"{result.filer.slug} CIK {failure.cik}: {failure.error}"
                    for failure in result.failures
                )
    finally:
        await engine.dispose()
    return results


# --- one filing: fetch, parse, load and publish ------------------------------


@dataclass(frozen=True, slots=True)
class Parsed:
    """Both documents parsed, and the verdict of the guards on them."""

    cover: PrimaryDoc
    table: InformationTable
    normalised: NormalisedFiling


@dataclass(frozen=True, slots=True)
class Written:
    """What loading one filing did."""

    result: LoadResult

    scope: Scope
    """The pairs it was published into: where it is filed now, and where it was
    filed before if a re-ingest moved it. Empty while it has no filer."""

    published: Recomputed | None
    """The rebuild of :attr:`scope`. ``None`` when that is empty."""


async def fetch_13f(
    edgar: EdgarClient, accession: str, *, cik: str
) -> tuple[Submission, FilingDocuments]:
    """EDGAR's account of the submission, then its documents — unless it is not a 13F."""
    submission = await find_submission(edgar, cik=cik, accession_no=accession)
    require_thirteen_f(submission)
    return submission, await fetch_13f_documents(edgar, cik=cik, accession_no=accession)


def require_thirteen_f(submission: Submission) -> None:
    """Refuse anything this pipeline cannot parse, before fetching its documents.

    A guard rather than a filter because of how the alternative fails. Handing a
    Form 4 to the 13F parser raises nothing: there are no ``<infoTable>``
    elements in it, so the information table comes back empty, the guards have
    no declared totals to check it against, and the filing loads clean with zero
    holdings — which is a valid ``13F-NT``. The wrong form loads as a correct
    filing of the right one.
    """
    form = submission.form_type.upper()
    if not form.startswith(THIRTEEN_F_FORMS):
        raise JobError(
            f"{submission.accession_no} is a {submission.form_type or 'unknown'} filing; "
            f"ingest-filing reads {' and '.join(THIRTEEN_F_FORMS)} (and their /A amendments)"
        )


def parse_13f(documents: FilingDocuments, *, filed_at: datetime) -> Parsed:
    """Parse and normalise. Pure: the same bytes and ``filed_at`` give the same rows."""
    cover = parse_primary_doc(documents.primary_doc)
    table = (
        parse_information_table(documents.info_table)
        if documents.info_table is not None
        else InformationTable(rows=(), warnings=())
    )
    normalised = normalise_filing(filed_at=filed_at, cover=cover, table=table)
    return Parsed(cover=cover, table=table, normalised=normalised)


async def write_13f(
    session: AsyncSession,
    *,
    accession: str,
    filed_at: datetime,
    parsed: Parsed,
    raw_prefix: str | None,
    source_url: str,
) -> Written:
    """Load the filing, mark its queue row ``done`` and publish it, in the caller's transaction.

    Publishing is :func:`~app.derived.recompute.recompute` over the pair the
    filing is filed under, and the pair it was filed under before if a
    re-ingest moved it, with the period after each. In the load's transaction,
    so a reader never sees holdings that the snapshot has not caught up with,
    and a load whose rebuild fails is not loaded.

    ``raw_key`` is the filing's archive *prefix*, not one document's key: a
    13F is several documents, and the prefix is what lists all of them. It is
    only ever ``None`` on a dry run, which never gets here.
    """
    # Before the first write, not just before the rebuild: see the deadlock in
    # app.derived.recompute.
    await hold_recompute_lock(session)
    filed_before = await filing_pairs(session, accession)
    result = await load_filing(
        session,
        accession_no=accession,
        filed_at=filed_at,
        primary_doc=parsed.cover,
        normalised=parsed.normalised,
        raw_key=raw_prefix,
        source_url=source_url,
    )
    await mark_ingested(session, accession)
    scope = Scope.of(filed_before | await filing_pairs(session, accession))
    published = await recompute(session, scope) if scope.pairs else None
    return Written(result=result, scope=scope, published=published)


def log_ingested(parsed: Parsed, written: Written) -> None:
    """After the commit, so the line never reports a load that rolled back."""
    published = written.published
    logger.info(
        "filing.ingested",
        cik=parsed.cover.cik,
        period=parsed.cover.period_of_report.isoformat(),
        filing_id=written.result.filing_id,
        rows=written.result.holdings_loaded,
        status=parsed.normalised.parse_status.value,
        positions_published=None if published is None else published.snapshot.positions,
        periods_withheld=None if published is None else published.snapshot.withheld,
    )


def positions(table: InformationTable, result: LoadResult) -> int:
    """Positions loaded, or waiting to be.

    Deferred holdings are counted rather than reported as the zero the loader
    returns, because "0 positions loaded" is what a 13F-NT looks like and this
    is the opposite: the positions exist, they are waiting on a filer. The
    arithmetic is the loader's own — rows minus the ones it folded — so the
    two branches give the same number for the same filing either way.
    """
    if result.holdings_deferred:
        return len(table.rows) - result.rows_collapsed
    return result.holdings_loaded


# --- the queue ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shared:
    """What every worker in a run shares."""

    sessions: async_sessionmaker[AsyncSession]
    store: RawStore
    edgar: EdgarClient


@dataclass(frozen=True, slots=True)
class Progress:
    """What a run by hand is told as it goes, to print. A task passes none."""

    planned: Callable[[BackfillPlan], None]
    """Once, with the plan, before any filing starts."""

    finished: Callable[[FilingResult], None]
    """As each filing finishes, in the order they finish."""

    stopping: Callable[[], None]
    """On the first Ctrl-C, which lets the filings in flight finish."""


def interruptible(stopping: Callable[[], None] | None) -> AbstractContextManager[asyncio.Event]:
    """The event the first Ctrl-C sets, when there is someone at a terminal to press it.

    Without ``stopping``, an event nothing sets, and no handler: in a Celery
    worker SIGINT belongs to the worker, whose warm shutdown already lets a
    running task finish.
    """
    if stopping is None:
        return nullcontext(asyncio.Event())
    return stop_on_interrupt(notify=stopping)


@dataclass(frozen=True, slots=True)
class Backfilled:
    """What one run of the queue did."""

    plan: BackfillPlan
    ran: BackfillRun

    published: frozenset[Pair]
    """Every pair a filing this run loaded was published into."""

    @property
    def failed(self) -> bool:
        """A filing failed, or EDGAR blocked the run."""
        return self.ran.rate_limited is not None or bool(self.ran.count(Outcome.FAILED))

    @property
    def not_started(self) -> int:
        return self.ran.count(Outcome.NOT_STARTED)


@tracked_task("backfill_13f", context=without_progress)
async def ingest_pending_filings(
    run: TrackedRun,
    settings: Settings,
    *,
    filer: str | None,
    since: date | None,
    concurrency: int,
    limit: int | None,
    force: bool,
    progress: Progress | None = None,
) -> Backfilled:
    """Plan from the database, then ingest the plan, ``concurrency`` filings at a time.

    One engine, one raw store and one EDGAR client for the whole run, shared by
    every worker. The client is opened even for a run that will only reprocess,
    because opening it sends nothing; the plan is what keeps such a run off
    the network, by giving the workers nothing to fetch.

    Each filing is published as it loads (:func:`write_13f`). The materialised
    views are not refreshed here: that is ``refresh-views``, a run of its own,
    which the CLI runs after this and a task chain runs after
    ``recompute_positions``.

    The run is ``partial`` when a filing failed, EDGAR blocked the run, or
    Ctrl-C stopped it short. Each failed filing is a line of
    ``ingestion_run.error``, as on its queue row.

    :param filer: One filer's slug, or ``None`` for every filer.
    :param since: Only filings filed on or after this date.
    :param limit: At most this many filings worked on; skipped ones do not count.
    :param force: Also reprocess filings already loaded, from the raw store.
    :raises UnknownFilerError: ``filer`` names no filer.
    """
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency is 1 to {MAX_CONCURRENCY}, not {concurrency}")
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    ran = BackfillRun(results=(), rate_limited=None)
    published: set[Pair] = set()
    try:
        async with sessions.begin() as session:
            plan = await plan_backfill(session, slug=filer, since=since, force=force, limit=limit)
        if progress is not None:
            progress.planned(plan)
        run.items_seen = len(plan.work)

        if plan.work:
            async with open_raw_store(settings) as store, EdgarClient(settings) as edgar:
                shared = Shared(sessions=sessions, store=store, edgar=edgar)
                with interruptible(None if progress is None else progress.stopping) as stop:
                    ran = await run_backfill(
                        plan.work,
                        process=partial(_ingest_filing, shared, published),
                        on_failure=partial(record_attempt, sessions),
                        on_result=progress.finished if progress is not None else _ignore,
                        concurrency=concurrency,
                        stop=stop,
                    )
    finally:
        await engine.dispose()

    run.items_written = ran.count(Outcome.OK) + ran.count(Outcome.SUSPECT)
    run.errors.extend(
        f"{result.filing.accession_no}: {result.error}"
        for result in ran.results
        if result.outcome is Outcome.FAILED
    )
    not_started = ran.count(Outcome.NOT_STARTED)
    if not_started:
        run.errors.append(f"stopped with {not_started} not started; re-run to resume")
    if run.items_written:
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    return Backfilled(plan=plan, ran=ran, published=frozenset(published))


def _ignore(_: FilingResult) -> None:
    pass


async def _ingest_filing(
    shared: Shared, published: set[Pair], filing: PlannedFiling
) -> FilingResult:
    """One filing, archived before it is parsed and parsed before it is loaded.

    A filing the plan has as loaded is in the work only because of ``force``,
    and is reprocessed: its archived documents, parsed again with the
    ``filed_at`` its row already holds. That branch makes no EDGAR request. The
    other is ``ingest-filing``'s, except that it never overwrites an archived
    document — a backfill does not replace what it has with what EDGAR serves
    today, and ``ingest-filing --force`` is there for the filing that should.
    """
    accession = filing.accession_no
    if filing.loaded is None:
        submission, documents = await fetch_13f(shared.edgar, accession, cik=filing.cik)
        raw_prefix = await archive_13f_documents(
            shared.store, documents, cik=filing.cik, accession_no=accession
        )
        filed_at = submission.filed_at
    else:
        if filing.loaded.raw_key is None:
            raise JobError(
                f"{accession} was loaded before its documents were archived, so there is "
                f"nothing to reprocess: ingest-filing {accession} --force fetches them again"
            )
        raw_prefix = filing.loaded.raw_key
        documents = await read_13f_documents(
            shared.store, raw_prefix, directory_url=directory_url(filing)
        )
        filed_at = filing.loaded.filed_at

    parsed = parse_13f(documents, filed_at=filed_at)
    async with shared.sessions.begin() as session:
        written = await write_13f(
            session,
            accession=accession,
            filed_at=filed_at,
            parsed=parsed,
            raw_prefix=raw_prefix,
            source_url=documents.primary_doc_url,
        )
    log_ingested(parsed, written)
    published.update(written.scope.pairs or ())

    return FilingResult(
        filing=filing,
        outcome=(
            Outcome.SUSPECT if parsed.normalised.parse_status is ParseStatus.SUSPECT else Outcome.OK
        ),
        period=parsed.cover.period_of_report,
        rows=positions(parsed.table, written.result),
        deferred=written.result.holdings_deferred,
    )


def directory_url(filing: PlannedFiling) -> str:
    """The EDGAR directory an archived filing was fetched from.

    Taken from the URL recorded when it was, rather than rebuilt from today's
    archive convention, which is the reason ``filing.source_url`` is kept.
    """
    if filing.loaded is not None and filing.loaded.source_url is not None:
        return filing.loaded.source_url.rsplit("/", 1)[0]
    return EdgarClient.filing_index_url(filing.cik, filing.accession_no).rsplit("/", 1)[0]
