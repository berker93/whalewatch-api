"""Typer CLI: discover-filings, ingest-filing, seed-investors, verify-investors, audit-overlaps,
audit-amendments, check-data, recompute, detect-splits, backfill, runs, refresh-views,
reconcile, import-13f-list, resolve-cusips, cusip-coverage, resolve-cusip, ticker-alias,
fetch-quotes, crawl-form4, ingest-form4, create-partitions, detect-clusters, create-user.

The operational interface. Celery's beat schedule is how this pipeline runs when
nobody is watching; this is how it runs when somebody is, and the two must not
be different code. Every verb here is the same function a task calls, wrapped in
argument parsing and a summary a person can read — so that a quarter that came
out wrong is re-run by hand, from a shell in the container, without a broker in
the loop and without anyone having to write a throwaway script at the point in
the incident where throwaway scripts are least trustworthy.

::

    uv run python -m app.cli discover-filings --filer berkshire-hathaway
    uv run python -m app.cli backfill --concurrency 5
    uv run python -m app.cli backfill --filer berkshire-hathaway --force
    uv run python -m app.cli ingest-filing 0001067983-24-000011 --cik 1067983
    uv run python -m app.cli ingest-filing 0001067983-24-000011 --dry-run
    uv run python -m app.cli seed-investors
    uv run python -m app.cli verify-investors --csv verify-investors.csv
    uv run python -m app.cli audit-overlaps --filer pershing-square
    uv run python -m app.cli audit-amendments --filer berkshire-hathaway
    uv run python -m app.cli check-data
    uv run python -m app.cli recompute --filer berkshire-hathaway
    uv run python -m app.cli recompute --period 2026Q1
    uv run python -m app.cli recompute --all
    uv run python -m app.cli detect-splits --all --dry-run
    uv run python -m app.cli detect-splits --period 2024Q2
    uv run python -m app.cli refresh-views
    uv run python -m app.cli refresh-views --view mv_quarter_flows --no-concurrent
    uv run python -m app.cli reconcile
    uv run python -m app.cli runs --job backfill_13f
    uv run python -m app.cli import-13f-list --quarter 2026Q1
    uv run python -m app.cli resolve-cusips --limit 200
    uv run python -m app.cli cusip-coverage
    uv run python -m app.cli resolve-cusip 037833100 --ticker AAPL --note "OpenFIGI miss"
    uv run python -m app.cli ticker-alias 30303M102 --ticker FB --to 2022-06-09
    uv run python -m app.cli fetch-quotes --ticker NVDA
    uv run python -m app.cli crawl-form4 --from 2026-07-01
    uv run python -m app.cli crawl-form4 --catch-up
    uv run python -m app.cli ingest-form4
    uv run python -m app.cli create-partitions --through 2028
    uv run python -m app.cli detect-clusters
    uv run python -m app.cli create-user --email ada@example.com --name "Ada Lovelace"

Every run is recorded
---------------------
Every verb that touches the database runs its body inside
:func:`~app.jobs.tracking.track_run`, which writes an ``ingestion_run`` row as
it starts and its outcome as it ends, and binds the row's id as ``run_id`` on
every log line in between. ``runs`` lists them. The exceptions are ``runs``
itself, which reads the record, ``verify-investors``, which has no database
to write it to, and ``create-user``, whose row would hold a person's email. A
test fails if a new verb is neither tracked nor one of those.
A verb that publishes (``recompute``, ``detect-splits``, and ``ingest-filing``
or ``backfill`` when they load something) then runs ``refresh-views`` as a
second run, which names the first in its ``after_run_id``.

Every verb that changes what the API serves — those, ``refresh-views``,
``seed-investors``, ``resolve-cusips``, ``resolve-cusip``, ``ticker-alias``,
``fetch-quotes`` when it writes a close, ``ingest-form4`` when it loads
something, and ``detect-clusters`` — then drops every response the API has
cached (:func:`~app.jobs.cache.invalidate_cache`), and records how many in its
run's ``metrics``.

The verbs that are also Celery tasks
------------------------------------
``discover-filings``, ``backfill``, ``crawl-form4``, ``ingest-form4``,
``resolve-cusips``, ``fetch-quotes``, ``recompute`` and ``refresh-views`` are
each a call to a ``@tracked_task`` job (app/jobs/), the same call its task
makes (app/jobs/tasks.py), with the arguments parsed on the way in and the
summary printed on the way out. The job holds its advisory lock for the run,
so a verb started while the same job is running — the worker's, or another
shell's — prints ``skipped: ...`` on stderr, records nothing and exits 0.

Exit codes
----------
Zero when the filing ends up loaded, and zero when it was already loaded and
this run was asked to leave it alone — "already done" is a success, or a
backfill script resuming over a thousand filings would fail on every one it had
finished. Non-zero for everything else: a filing that could not be found,
fetched, parsed or written. That is the contract the shell loop around this
command depends on, and it is why the failure paths below all funnel through
:class:`CommandError` rather than tracebacks.

``check-data`` and ``reconcile`` are the exceptions, because finding something
is their job: 1 means it ran and found something to look at, and 2 means it
could not run as asked. ``cusip-coverage`` is a gate: 1 means coverage by value
is below its threshold.

Logs to stderr, summary to stdout
---------------------------------
:func:`~app.core.logging.configure_logging` is pointed at stderr here, unlike in
the API where it goes to stdout. A backfill loop that captures this command's
output wants the summary and not the eleven ``edgar.request`` lines that
produced it, and the split is what lets ``ingest-filing ... > report.txt`` work
while the operational log still reaches the terminal.

Sessions
--------
Everything here goes through :func:`~app.db.session.session_scope`, never
``app.api.deps.get_session``. The request dependency draws from the pool the
FastAPI lifespan owns, and there is no lifespan in a CLI process — reaching for
it gets a session bound to an engine nobody created, or worse, one bound to an
event loop that ``asyncio.run`` is about to close.
"""

from __future__ import annotations

import asyncio
import csv
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, NoReturn, TextIO

import httpx
import structlog
import typer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import rules as account_rules
from app.accounts.passwords import hash_password
from app.accounts.rules import DEFAULT_TIME_ZONE
from app.core.accession import normalise_accession
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.periods import quarter_label
from app.core.tickers import normalise_ticker
from app.db.models.account import AppUser
from app.db.models.filer import OverlapPolicy
from app.db.models.filing import LOADED_STATUSES, Filing
from app.db.models.pending_filing import PendingStatus
from app.db.models.sec_13f_security import ListStatus
from app.db.models.security import ResolutionSource, Security
from app.db.partitions import CreatedPartition, ensure_partitions, next_year
from app.db.queries.amendments import PeriodFiling, PeriodResolution, audit_amendments
from app.db.queries.checks import DataCheckReport, check_data
from app.db.queries.cusip_coverage import DEFAULT_THRESHOLD_PCT, CusipCoverage, cusip_coverage
from app.db.queries.overlaps import OverlapFinding, audit_overlaps
from app.db.queries.runs import RunSummary, job_names, recent_runs
from app.db.session import session_scope
from app.derived.implied_price import PriceRebuild
from app.derived.insider_cluster import (
    DEFAULT_MIN_INSIDERS,
    DEFAULT_WINDOW,
    ClusterRebuild,
)
from app.derived.position_change import ChangeRebuild
from app.derived.recompute import (
    Recomputed,
    Redetected,
    detect_splits,
)
from app.derived.reconcile import INVARIANTS, SAMPLE, Reconciliation, reconcile
from app.derived.scope import Pair, Scope
from app.derived.security_split import SplitRebuild, Verdict, ratio_label
from app.derived.views import MATERIALISED_VIEWS, Refreshed
from app.ingestion.archive import (
    archive_13f_documents,
)
from app.ingestion.backfill import (
    BackfillPlan,
    BackfillRun,
    FilingResult,
    Outcome,
)
from app.ingestion.discovery import (
    FilerDiscovery,
    QueuedFiling,
    UnknownFilerError,
    mark_ingested,
    queued_filing,
    record_failure,
    tracked_filer_ids,
)
from app.ingestion.edgar.client import EdgarClient, EdgarRateLimited, EdgarServerError
from app.ingestion.edgar.company_tickers import CompanyTickersMalformedError
from app.ingestion.edgar.documents import (
    FilingDocuments,
    FilingDocumentsError,
)
from app.ingestion.edgar.submissions import (
    Submission,
    SubmissionMalformedError,
    SubmissionNotFoundError,
)
from app.ingestion.edgar.thirteen_f_list import (
    ThirteenFListUnavailableError,
    filename_quarter,
    find_list_url,
)
from app.ingestion.enrichment.figi import (
    OpenFIGIRateLimited,
    OpenFIGIResponseError,
    OpenFIGIServerError,
)
from app.ingestion.enrichment.quotes import (
    NullQuoteProvider,
    QuoteFetchError,
    QuoteProviderUnavailableError,
    QuoteReport,
)
from app.ingestion.enrichment.resolution import (
    ManualResolution,
    ResolutionReport,
    resolve_manually,
)
from app.ingestion.enrichment.tickers import (
    Delistings,
    HistoryEntry,
    TickerHistoryConflictError,
    UnknownSecurityError,
    add_ticker_history,
    flag_delistings,
)
from app.ingestion.insider_discovery import (
    Crawl,
    DayKind,
    NoWatermarkError,
    Universe,
    UniverseError,
    days_between,
    new_york_today,
)
from app.ingestion.investors import (
    DEFAULT_INVESTORS_PATH,
    InvestorEntry,
    InvestorListError,
    SeedConflictError,
    SeedResult,
    load_investors,
    seed_investors,
)
from app.ingestion.loaders import (
    AdditionsCheck,
    AmendmentLink,
    LoadResult,
    SecurityLink,
    check_additions,
    load_13f_list,
)
from app.ingestion.normalisation import NormalisedFiling, NoteKind
from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
)
from app.ingestion.parsers.thirteen_f_list import (
    ThirteenFList,
    ThirteenFListParseError,
    parse_13f_list,
)
from app.ingestion.verify_investors import CikCheck, stale_cutoff, verify_investors
from app.jobs.cache import invalidate_cache
from app.jobs.derived import recompute_positions, refresh_matviews
from app.jobs.enrichment import fetch_prices, float_pct, resolve_cusips
from app.jobs.errors import JobError
from app.jobs.form4 import (
    READ_DAYS,
    Form4Loaded,
    Form4Progress,
    cluster_metrics,
    crawl_form4_daily,
    ingest_form4,
    partition_metrics,
    rebuild_clusters,
)
from app.jobs.thirteen_f import (
    MAX_CONCURRENCY,
    Parsed,
    Progress,
    discover_13f_filings,
    discovery_cutoff,
    fetch_13f,
    ingest_pending_filings,
    log_ingested,
    parse_13f,
    positions,
    write_13f,
)
from app.jobs.tracking import JobLocked, track_run
from app.storage.raw import (
    RawStore,
    RawStoreError,
    open_raw_store,
    thirteen_f_list_key,
    thirteen_f_list_prefix,
)

logger = get_logger(__name__)

#: Width of the label column in the summary. Wide enough for the longest label
#: below, which keeps the values in one column that the eye can run down.
_LABEL_WIDTH: Final = 12

#: How many warnings to print before summarising the rest. A filing whose units
#: are wrong has one note per position, and three thousand lines of them is not
#: a summary. ``filing.parse_notes`` has all of them.
_MAX_ECHOED_WARNINGS: Final = 10

#: Wide enough for every guard's name, so the details line up in one column.
_NOTE_KIND_WIDTH: Final = max(len(kind) for kind in NoteKind)


class CommandError(JobError):
    """An operational failure with a message worth printing and no traceback.

    Everything the operator can act on — a CIK we could not work out, a filing
    EDGAR does not have, a document that is not what it claims — is raised as
    one of these and rendered as a single line on stderr. Anything else is a bug
    in this codebase and keeps its traceback, because a stack is what makes that
    kind of failure fixable and a tidy message is what makes it invisible.
    """


app = typer.Typer(
    name="whalewatch",
    help="WhaleWatch ingestion and maintenance commands.",
    no_args_is_help=True,
    add_completion=False,
    # Typer's decorated tracebacks hide the frames inside our own code, which is
    # the opposite of what is wanted from an unexpected exception in a job.
    pretty_exceptions_enable=False,
)


@app.callback()
def main() -> None:
    """WhaleWatch's operational CLI.

    Exists to make this a command *group* rather than a single command. Typer
    promotes a lone command to the top level, which would make the verb below
    disappear from the invocation and change every documented example the day
    ``backfill`` is added.
    """


@app.command("ingest-filing")
def ingest_filing(
    accession_no: Annotated[
        str,
        typer.Argument(
            metavar="ACCESSION_NO",
            help="EDGAR accession number, dashed or not: 0001067983-24-000011.",
        ),
    ],
    cik: Annotated[
        str | None,
        typer.Option(
            "--cik",
            help=(
                "CIK whose archive the filing lives under. Optional only for a "
                "filing already in the database, whose CIK we then already know."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Re-fetch and re-load a filing that is already loaded, replacing "
                "its archived documents with what EDGAR serves now."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch, parse and report. Write nothing."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh-views/--no-refresh-views",
            help="Refresh the materialised views after a load that publishes, as "
            "refresh-views does. --no-refresh-views is for a script that loads many "
            "filings and runs refresh-views once at the end.",
        ),
    ] = True,
) -> None:
    """Fetch, parse and load one 13F filing.

    Idempotent: running it twice leaves the database exactly as running it once
    did. Without ``--force`` a filing that is already loaded is left alone and
    reported as such.
    """
    try:
        asyncio.run(
            _ingest_filing(accession_no, cik=cik, force=force, dry_run=dry_run, refresh=refresh)
        )
    except (
        # The job's own, from the steps ingest-filing shares with backfill, and
        # this command's.
        JobError,
        FilingDocumentsError,
        FilingParseError,
        SubmissionMalformedError,
        SubmissionNotFoundError,
        # EdgarRateLimited arrives here only after the client has already spent
        # its retries waiting the block out, so there is nothing left to do but
        # say so and let the caller decide when to come back.
        EdgarRateLimited,
        EdgarServerError,
        httpx.HTTPError,
        # The archive being unreachable or refusing a write. Fatal rather than
        # skipped: a filing loaded without its bytes archived is the one kind a
        # parser fix cannot reach without going back to EDGAR.
        RawStoreError,
    ) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _ingest_filing(
    accession_no: str, *, cik: str | None, force: bool, dry_run: bool, refresh: bool
) -> None:
    """The command's body, as one coroutine, so the sync wrapper stays a bridge.

    The ordering here is not arbitrary. The database is consulted first and
    briefly — for the CIK we may already know and for whether there is anything
    to do — then released, because the EDGAR half of this can sit for ten
    minutes waiting out a rate-limit block and a Postgres connection held idle
    in a transaction for that long is one that blocks a migration and shows up
    in someone else's incident. The write opens its own scope at the end.

    A filing in ``pending_filing`` has each attempt written back to its row:
    ``done`` in the loader's own transaction, or a failure counted with its
    message. A dry run writes neither. The run itself is recorded either way.

    A load that publishes refreshes the materialised views after it commits,
    as a ``refresh-views`` run of its own. A skipped filing, a dry run and a
    filing loaded without a filer publish nothing, so they refresh nothing.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(
        settings,
        "ingest-filing",
        accession_no=accession_no,
        cik=cik,
        force=force,
        dry_run=dry_run,
        refresh_views=refresh,
    ) as run:
        run.items_seen = 1
        accession = _normalise_accession(accession_no)
        structlog.contextvars.bind_contextvars(accession_no=accession)

        known, queued = await _known_filing(settings, accession)
        resolved_cik = _resolve_cik(cik, known=known, queued=queued, accession_no=accession)

        if known is not None and known.parse_status in LOADED_STATUSES and not force:
            if queued is not None and queued.status != PendingStatus.DONE and not dry_run:
                async with session_scope(settings) as session:
                    await mark_ingested(session, accession)
            _echo_skip(accession, known)
            return

        try:
            report = await _fetch_and_load(
                settings, accession, cik=resolved_cik, force=force, dry_run=dry_run
            )
        except Exception as failure:
            if queued is not None and not dry_run:
                await _record_failure(settings, accession, failure)
            raise
        if report.result is not None:
            run.items_written = 1
        if report.published is not None:
            run.metrics["cache_invalidated"] = await invalidate_cache(settings)
        _echo_report(report)

    if refresh and report.published is not None:
        await _refresh_after(settings, run.id)


async def _fetch_and_load(
    settings: Settings, accession: str, *, cik: str, force: bool, dry_run: bool
) -> _Report:
    """Everything after the decision that there is work to do."""
    async with EdgarClient(settings) as edgar:
        submission, documents = await fetch_13f(edgar, accession, cik=cik)

    # Archived before parsed, always: a parser that raises below has nothing
    # left to lose, and the fix is a re-parse of these bytes instead of a
    # re-crawl. Skipped on a dry run, which promises to write nothing.
    raw_prefix = (
        None
        if dry_run
        else await _archive(settings, documents, cik=cik, accession=accession, force=force)
    )

    # Parsing happens after the client is closed: it is pure CPU over bytes we
    # already hold, and holding a connection pool open across it keeps a socket
    # to sec.gov alive for no reason.
    parsed = parse_13f(documents, filed_at=submission.filed_at)

    result, published = (
        (None, None)
        if dry_run
        else await _load(
            settings,
            accession=accession,
            filed_at=submission.filed_at,
            parsed=parsed,
            raw_prefix=raw_prefix,
            source_url=documents.primary_doc_url,
        )
    )

    return _Report(
        accession_no=accession,
        submission=submission,
        cover=parsed.cover,
        table=parsed.table,
        normalised=parsed.normalised,
        documents=documents,
        raw_prefix=raw_prefix,
        result=result,
        published=published,
        dry_run=dry_run,
    )


async def _record_failure(settings: Settings, accession: str, failure: Exception) -> None:
    """Count the attempt on the queue row, without hiding why it failed.

    Best effort, and deliberately so: the likeliest reason this write fails is
    the one the ingest failed for — the database is gone — and an exception
    from here would replace the real error on stderr with a second-hand one.
    """
    try:
        async with session_scope(settings) as session:
            await record_failure(session, accession, f"{type(failure).__name__}: {failure}")
    except Exception as unrecorded:
        logger.warning("pending_filing.record_failed", error=str(unrecorded))


async def _archive(
    settings: Settings,
    documents: FilingDocuments,
    *,
    cik: str,
    accession: str,
    force: bool,
) -> str:
    """Write the fetched documents to the raw store; return their prefix.

    ``--force`` is what overwrites. Without it a filing archived by an earlier
    run keeps the bytes it was first archived with, even though this run has
    just fetched them again — the first copy is the one worth keeping.
    """
    async with open_raw_store(settings) as store:
        return await archive_13f_documents(
            store, documents, cik=cik, accession_no=accession, overwrite=force
        )


async def _load(
    settings: Settings,
    *,
    accession: str,
    filed_at: datetime,
    parsed: Parsed,
    raw_prefix: str | None,
    source_url: str,
) -> tuple[LoadResult, Recomputed | None]:
    """Write the parsed filing, in one transaction that ``session_scope`` commits."""
    async with session_scope(settings) as session:
        written = await write_13f(
            session,
            accession=accession,
            filed_at=filed_at,
            parsed=parsed,
            raw_prefix=raw_prefix,
            source_url=source_url,
        )
    log_ingested(parsed, written)
    return written.result, written.published


# --- what we already know ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class _KnownFiling:
    """The two columns of an existing ``filing`` row this command cares about."""

    cik: str
    parse_status: str


async def _known_filing(
    settings: Settings, accession_no: str
) -> tuple[_KnownFiling | None, QueuedFiling | None]:
    """Look the accession number up in our own database: ``filing``, then the queue.

    The answers are load-bearing. The CIK is what makes ``--cik`` optional — a
    filing already loaded or queued by ``discover-filings`` has one, and
    re-running it should not require the operator to go and find it again. The
    status is what makes the command idempotent without re-fetching: "already
    loaded" is decided here, before a single EDGAR request, which is the
    difference between resuming a backfill and re-running it.
    """
    async with session_scope(settings) as session:
        return await _loaded_row(session, accession_no), await queued_filing(session, accession_no)


async def _loaded_row(session: AsyncSession, accession_no: str) -> _KnownFiling | None:
    row = (
        await session.execute(
            select(Filing.cik, Filing.parse_status).where(Filing.accession_no == accession_no)
        )
    ).first()
    return None if row is None else _KnownFiling(cik=row.cik, parse_status=row.parse_status)


def _resolve_cik(
    given: str | None,
    *,
    known: _KnownFiling | None,
    queued: QueuedFiling | None,
    accession_no: str,
) -> str:
    """The CIK whose archive directory holds this filing.

    Required, and not derivable from the accession number, which is the thing
    everyone assumes it is. An accession number's leading ten digits identify
    whoever *transmitted* the submission — for most institutional filers that is
    a filing agent, and ``/Archives/edgar/data/<agent-cik>/<accession>/`` is not
    a directory that exists. The archive path is keyed on the subject filer.
    """
    if given is not None:
        return _padded_cik(given)
    if known is not None:
        return known.cik
    if queued is not None:
        return queued.cik
    raise CommandError(
        f"{accession_no} is not in the database, so its CIK is unknown: pass --cik. "
        "It cannot be read off the accession number — the leading digits belong to "
        "whoever transmitted the filing, usually a filing agent, and EDGAR's archive "
        "path is keyed on the filer instead."
    )


# --- input normalisation -----------------------------------------------------


def _normalise_accession(value: str) -> str:
    """The dashed form, or a :class:`CommandError` naming the shape expected.

    The rule itself lives in :mod:`app.core.accession`, shared with the API,
    because "what an accession number looks like" is one fact and the two
    spellings must not be allowed to diverge between the endpoint that reads a
    filing and the command that writes it. All this adds is the CLI's failure
    mode: a message on stderr and exit 1, rather than a traceback.
    """
    try:
        return normalise_accession(value)
    except ValueError as malformed:
        raise CommandError(str(malformed)) from malformed


def _padded_cik(value: str) -> str:
    """``1067983`` -> ``0001067983``, in the one spelling the database stores.

    Padded rather than passed through, even though the archive URLs want it
    unpadded and :class:`~app.ingestion.edgar.client.EdgarClient` re-derives
    both forms anyway: this value is also compared against ``filing.cik``, which
    is ``CHAR(10)``, and an unpadded comparison finds nothing.
    """
    digits = value.strip()
    if not digits.isdigit() or len(digits) > 10:
        raise CommandError(f"{value!r} is not a CIK: expected up to ten digits")
    return digits.zfill(10)


# --- output ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Report:
    """Everything one run produced, gathered so that rendering is a pure function.

    Assembled rather than printed as it goes, so that a dry run and a real run
    are the same summary with one line different — which is the only way
    ``--dry-run`` is worth anything. A dry run whose output does not match what
    the real run prints is a rehearsal of a different command.
    """

    accession_no: str
    submission: Submission
    cover: PrimaryDoc
    table: InformationTable
    normalised: NormalisedFiling
    documents: FilingDocuments
    raw_prefix: str | None
    result: LoadResult | None
    published: Recomputed | None
    dry_run: bool


def _echo_report(report: _Report) -> None:
    """Print the summary a person reads to decide whether to trust the load."""
    cover = report.cover
    normalised = report.normalised

    typer.echo(
        f"{report.accession_no}  {cover.form_type}"
        + ("  — dry run, nothing written" if report.dry_run else "")
    )
    _line("filer", f"{_filer_name(report)}  (CIK {cover.cik})")
    _line("period", f"{cover.period_of_report.isoformat()}  ({_quarter(cover.period_of_report)})")
    _line(
        "filed",
        f"{_instant(report.submission.filed_at)}  (values x{normalised.value_multiplier})",
    )
    _line("documents", _document_names(report.documents))
    if report.raw_prefix is not None:
        _line("archived", report.raw_prefix)
    _line("rows", _rows_line(report))
    _line("value", f"${_total_value(normalised):,.2f}")
    _line("status", normalised.parse_status.value)

    for note in _disagreements(report):
        _line("mismatch", note)

    _echo_warnings(report)

    if report.result is not None:
        _line("written", _written_line(report.result))
    if report.published is not None:
        _echo_published(report.published)


def _echo_published(published: Recomputed) -> None:
    """The filing's period as published, or withheld, and the changes rebuilt with it."""
    snapshot = published.snapshot
    periods = _quarters(published.scope)
    if snapshot.withheld:
        _line(
            "withheld",
            f"{periods}: a suspect filing counts toward it — check-data lists it; "
            "recompute --include-suspect publishes it",
        )
    else:
        _line("published", f"{_count(snapshot.positions, 'position')} in {periods}")
    changed = published.scope | Scope.of(published.following)
    _line("changes", f"{_actions(published.changes)} in {_quarters(changed)}")
    _line("prices", f"{_grades(published.prices)} in {periods}")
    if published.splits.moved:
        _line("splits", _splits(published.splits))


def _line(label: str, value: str) -> None:
    typer.echo(f"  {label:<{_LABEL_WIDTH}}{value}")


def _echo_skip(accession_no: str, known: _KnownFiling) -> None:
    """What a run that decided there was nothing to do says, and why it says it.

    On stdout and at exit 0, because this is a success: a backfill loop over a
    thousand accession numbers re-runs the ones it already finished, and a
    non-zero exit or a stderr line would make every resumed run look like a
    partial failure.
    """
    typer.echo(f"{accession_no}  already loaded (status {known.parse_status}) — nothing to do")
    _line("filer", f"CIK {known.cik}")
    _line("hint", "--force fetches and loads it again; --dry-run reports without writing")


def _echo_warnings(report: _Report) -> None:
    """The guards' findings and the parser's, capped, most structured first."""
    warnings = _warning_lines(report)
    if not warnings:
        return

    _line("warnings", str(len(warnings)))
    for warning in warnings[:_MAX_ECHOED_WARNINGS]:
        typer.echo(f"  {'':<{_LABEL_WIDTH}}  {warning}")
    if len(warnings) > _MAX_ECHOED_WARNINGS:
        remaining = len(warnings) - _MAX_ECHOED_WARNINGS
        typer.echo(
            f"  {'':<{_LABEL_WIDTH}}  ... and {remaining} more; "
            "the full set is on filing.parse_notes"
        )


def _warning_lines(report: _Report) -> list[str]:
    """Every finding worth a person's attention, as one flat list.

    Two sources, and neither subsumes the other.
    :attr:`~app.ingestion.normalisation.NormalisedFiling.parse_notes` is the
    durable record — the guards' verdicts plus the rows the parser dropped — and
    it is what lands in the database. The parser's *tolerated* warnings do not:
    a malformed ``<figi>`` costs no value and no share count, so it is
    deliberately kept out of the column that exists for missing money. It still
    belongs in front of whoever is watching this run, which is here.
    """
    lines = [
        f"{note.kind.value:<{_NOTE_KIND_WIDTH}} {note.detail}"
        for note in report.normalised.parse_notes
    ]
    lines += [
        f"{'tolerated':<{_NOTE_KIND_WIDTH}} row {warning.row} {warning.field}: {warning.reason}"
        for warning in report.table.warnings
        if not warning.dropped
    ]
    return lines


def _disagreements(report: _Report) -> list[str]:
    """Where EDGAR's account of the submission and the document's differ.

    Reported, never fatal. The two are independent statements about one filing —
    EDGAR's index says what it accepted, the cover page says what the filer
    wrote — and a disagreement is usually benign (a co-filed 13F is indexed
    under a CIK that is not the filing manager's) and occasionally the first
    sign that the wrong directory was fetched. Neither case is worth refusing a
    load over; both are worth a line.

    The values written to the database are the *document's*, because that is
    what the loader is given, which is why this compares against the cover page
    rather than silently preferring the index.
    """
    submission, cover = report.submission, report.cover
    notes = []
    if submission.cik != cover.cik:
        notes.append(f"indexed under CIK {submission.cik}, cover page says {cover.cik}")
    if submission.form_type.upper() != cover.form_type.upper():
        notes.append(f"EDGAR calls this {submission.form_type}, cover page says {cover.form_type}")
    if (
        submission.period_of_report is not None
        and submission.period_of_report != cover.period_of_report
    ):
        notes.append(
            f"EDGAR reports period {submission.period_of_report.isoformat()}, "
            f"cover page says {cover.period_of_report.isoformat()}"
        )
    return notes


def _rows_line(report: _Report) -> str:
    """Parsed rows, loaded positions, and the arithmetic between them.

    Three numbers rather than one because they differ for three unrelated
    reasons, and a summary showing only the last of them cannot be checked. Rows
    are what the document contained; the cover page's declared count is the
    filer's own claim about that; positions are what the loader wrote after
    folding the lines that share a natural key (an ``otherManager`` split), and
    that fold is normal rather than a loss.
    """
    parts = [f"{len(report.table.rows)} rows parsed"]
    if report.cover.table_entry_total is not None:
        parts.append(f"{report.cover.table_entry_total} declared")

    result = report.result
    if result is None:
        return ", ".join(parts)

    count = positions(report.table, result)
    parts.append(f"{count} positions {'deferred' if result.holdings_deferred else 'loaded'}")
    if result.rows_collapsed:
        parts.append(f"{result.rows_collapsed} folded into another line")
    return ", ".join(parts)


def _written_line(result: LoadResult) -> str:
    """What the write actually did, including the case where it half-happened.

    ``holdings_deferred`` gets a sentence of its own because it is otherwise
    invisible: the filing is in the table, the holdings are not, and the summary
    would show "0 positions loaded" — which is exactly what a legitimate
    ``13F-NT`` shows. The two mean opposite things and the operator has to be
    able to tell them apart from this output alone.
    """
    if result.holdings_deferred:
        return (
            f"filing #{result.filing_id}, holdings DEFERRED — the CIK is not a known "
            "filer yet, so re-run this once it is resolved"
        )
    written = f"filing #{result.filing_id}, {result.holdings_loaded} holdings"
    if result.securities_created:
        written += f", {result.securities_created} new securities"
    return written


def _document_names(documents: FilingDocuments) -> str:
    """The two files that were fetched, by name, so a wrong pick is visible.

    The information table's filename is not predictable — this codebase has to
    identify it by its root element — and printing the one that was chosen is
    what lets someone reading a suspicious summary confirm in one glance that
    the portfolio came out of the file they expected.
    """
    primary = documents.primary_doc_url.rsplit("/", 1)[-1]
    if documents.info_table_url is None:
        return f"{primary}  (no information table — a notice reports no holdings)"
    return f"{primary} + {documents.info_table_url.rsplit('/', 1)[-1]}"


def _filer_name(report: _Report) -> str:
    """The cover page's name for the filer, falling back to EDGAR's.

    The cover page first because it is the filer's own statement of who they
    are for this period, and it is the value the loader writes.
    """
    return report.cover.filer_name or report.submission.entity_name or "(unnamed)"


def _total_value(normalised: NormalisedFiling) -> Decimal:
    """The portfolio's total, in whole dollars, summed the way the loader sums it.

    Over the normalised rows rather than the cover page's ``tableValueTotal``,
    so that the number printed here is the number that went into the database
    rather than the number the filer says should have. When those two disagree
    the value-total guard has already fired and said so in the warnings above.
    """
    return sum((holding.value_usd for holding in normalised.holdings), start=Decimal(0))


def _quarter(period: date) -> str:
    """``2024-03-31`` -> ``2024Q1``, matching the generated ``filing.quarter``."""
    return f"{period.year}Q{(period.month - 1) // 3 + 1}"


def _instant(moment: datetime) -> str:
    """A timezone-aware timestamp, printed with its offset kept.

    The offset is not decoration on this particular field: ``filed_at`` is what
    decides whether a filing's values are thousands or dollars, and the cutover
    is at midnight Eastern. A summary that dropped the zone would be unable to
    show why a filing near it got the multiplier it did.
    """
    return moment.isoformat(sep=" ", timespec="seconds")


# --- discover-filings --------------------------------------------------------


@app.command("discover-filings")
def discover_filings_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Discover for one filer instead of all."),
    ] = None,
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            formats=["%Y-%m-%d"],
            help="Earliest filing date to look at.",
            show_default="five years ago",
        ),
    ] = None,
    full_history: Annotated[
        bool,
        typer.Option("--all", help="Look at every filing EDGAR lists, however old."),
    ] = False,
) -> None:
    """Queue every 13F-HR and 13F-HR/A a tracked filer has filed and we have not loaded.

    Lists each of the filer's CIKs in EDGAR's submissions index and subtracts
    the filings already loaded; what is left goes into pending_filing, from
    which ingest-filing takes its CIK. Idempotent. A filing that failed to load
    is found again on the next run, with no special handling.

    Exits 1 if any CIK could not be read. The others are still queued.
    """
    if since is not None and full_history:
        typer.secho("error: --since and --all contradict each other", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    cutoff = discovery_cutoff(
        since.date() if since else None, full_history=full_history, today=date.today()
    )
    try:
        failed = asyncio.run(_discover_filings(filer, since=cutoff))
    except (UnknownFilerError, EdgarRateLimited) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure
    if failed:
        raise typer.Exit(code=1)


async def _discover_filings(slug: str | None, *, since: date | None) -> bool:
    """The job, then its summary. Returns whether any CIK failed."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    try:
        ran = await discover_13f_filings(settings, filer=slug, since=since)
    except JobLocked as locked:
        _echo_locked(locked)
        return False
    _echo_discovery(ran.value, since=since)
    return any(result.failures for result in ran.value)


def _echo_discovery(results: list[FilerDiscovery], *, since: date | None) -> None:
    window = "full history" if since is None else f"filed since {since.isoformat()}"
    typer.echo(f"discover-filings  13F-HR and 13F-HR/A, {window}")
    slug_width = max((len(result.filer.slug) for result in results), default=4)
    typer.echo(f"  {'slug':<{slug_width}}  {'found':>6}  {'ingested':>8}  {'new':>6}")
    for result in results:
        typer.echo(
            f"  {result.filer.slug:<{slug_width}}  {len(result.found):>6}  "
            f"{len(result.already_ingested):>8}  {len(result.new):>6}"
            + ("  FAILED" if result.failures else "")
        )

    found = sum(len(result.found) for result in results)
    ingested = sum(len(result.already_ingested) for result in results)
    new = sum(len(result.new) for result in results)
    typer.echo(
        f"  {len(results)} filers: {found} found, {ingested} already ingested, "
        f"{new} new in pending_filing"
    )
    for result in results:
        for failure in result.failures:
            _line("error", f"{result.filer.slug} CIK {failure.cik}: {failure.error}")


# --- crawl-form4 -------------------------------------------------------------


@app.command("crawl-form4")
def crawl_form4_command(
    day: Annotated[
        datetime | None,
        typer.Option("--date", formats=["%Y-%m-%d"], help="Crawl this one day."),
    ] = None,
    first: Annotated[
        datetime | None,
        typer.Option("--from", formats=["%Y-%m-%d"], help="Crawl from this day."),
    ] = None,
    last: Annotated[
        datetime | None,
        typer.Option(
            "--to",
            formats=["%Y-%m-%d"],
            help="Crawl to this day, with --from.",
            show_default="today",
        ),
    ] = None,
    catch_up: Annotated[
        bool,
        typer.Option(
            "--catch-up",
            help="Crawl from the day after the watermark to today. What a run with no "
            "days given does.",
        ),
    ] = False,
    universe: Annotated[
        str | None,
        typer.Option(
            "--universe",
            metavar="held|sp500",
            help="Whose Form 4s to queue.",
            show_default="FORM4_UNIVERSE",
        ),
    ] = None,
) -> None:
    """Queue the Form 4s and 4/As EDGAR's daily index lists for the universe's issuers.

    Reads each day's form index, keeps the lines whose CIK is a universe
    issuer, and queues the filings not already loaded in pending_filing.
    Weekends and holidays have no index and are skipped. The walk stops,
    without failing, at a day EDGAR has not published yet (today, before about
    10pm New York time).

    The watermark is the last day read in full, and moves only to the day
    after itself, so --catch-up never skips a day. A first run with --from or
    --date sets it.

    Exits 1 if a day failed: the days before it are kept, and the next run
    starts there.
    """
    if universe is not None and universe not in ("held", "sp500"):
        _usage("--universe is held or sp500")
    chosen = sum((day is not None, first is not None, catch_up))
    if chosen > 1:
        _usage("--date, --from and --catch-up are three different ways to choose days: use one")
    if last is not None and first is None:
        _usage("--to needs --from")
    today = new_york_today()
    days: list[date] | None
    if day is not None:
        days = [day.date()]
    elif first is not None:
        days = days_between(first.date(), today if last is None else last.date())
        if not days:
            _usage("--from is after --to")
    else:
        days = None  # --catch-up, read from the watermark inside the run
    if days and days[-1] > today:
        _usage(f"{days[-1]} is after today in New York ({today})")

    try:
        failed = asyncio.run(_crawl_form4(days, universe=universe, today=today))
    except (
        UniverseError,
        NoWatermarkError,
        EdgarRateLimited,
        EdgarServerError,
        CompanyTickersMalformedError,
        httpx.HTTPError,
    ) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure
    if failed:
        raise typer.Exit(code=1)


def _usage(message: str) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def _echo_locked(locked: JobLocked) -> None:
    """Another run of the job is in progress, so this one did nothing: exit 0.

    Not a failure: the run holding the lock is doing this one's work, and a
    loop or cron around the command should not page anyone for it.
    """
    typer.secho(f"skipped: {locked}", fg=typer.colors.YELLOW, err=True)


async def _crawl_form4(days: list[date] | None, *, universe: str | None, today: date) -> bool:
    """The job, then its summary. Returns whether a day failed."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    try:
        ran = await crawl_form4_daily(settings, days=days, universe=universe, today=today)
    except JobLocked as locked:
        _echo_locked(locked)
        return False
    crawled = ran.value
    if crawled.universe is None or crawled.crawl is None:
        typer.echo("crawl-form4  nothing to crawl: the watermark is today")
        return False
    _echo_crawl(crawled.universe, crawled.crawl)
    return crawled.failed


def _echo_crawl(universe: Universe, crawl: Crawl) -> None:
    typer.echo(f"crawl-form4  {universe.kind} universe: {universe.description}")
    for crawled in crawl.days:
        if crawled.kind is DayKind.WEEKEND:
            continue
        if crawled.kind is DayKind.INDEXED:
            detail = (
                f"{crawled.listed:>6,} Form 4s  {len(crawled.matched):>5,} matched  "
                f"{crawled.new:>5,} new"
            )
        elif crawled.kind is DayKind.FAILED:
            detail = f"FAILED  {crawled.error}"
        elif crawled.kind is DayKind.UNPUBLISHED:
            detail = "not published yet: stopped here"
        else:
            detail = "no index (holiday)"
        typer.echo(f"  {crawled.day}  {detail}")

    read = [crawled for crawled in crawl.days if crawled.kind in READ_DAYS]
    matched = sum(len(crawled.matched) for crawled in read)
    new = sum(crawled.new for crawled in read)
    typer.echo(
        f"  {_count(len(read), 'day')} read: {_count(matched, 'filing')} matched, "
        f"{new:,} new in pending_filing"
    )
    before = "none" if crawl.watermark_before is None else str(crawl.watermark_before)
    if crawl.watermark_after == crawl.watermark_before:
        typer.echo(f"  watermark {before}, unchanged")
    else:
        typer.echo(f"  watermark {before} -> {crawl.watermark_after}")


# --- backfill ----------------------------------------------------------------

#: 128 + SIGINT: what a shell reports for a process Ctrl-C stopped, so that a
#: wrapper can tell "interrupted, resume me" from "something failed, look".
_INTERRUPTED: Final = 130


@app.command("backfill")
def backfill_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Backfill one filer instead of all."),
    ] = None,
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since", formats=["%Y-%m-%d"], help="Only filings filed on or after this date."
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            metavar="N",
            min=1,
            max=MAX_CONCURRENCY,
            help="Filings in flight at once. All of them share one EDGAR rate limit.",
        ),
    ] = 5,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            metavar="N",
            min=1,
            help="Work on at most N filings. Skipped ones do not count.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Also reprocess filings already loaded, from their archived documents. "
                "Makes no EDGAR request for them."
            ),
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh-views/--no-refresh-views",
            help="Refresh the materialised views once, at the end, if anything loaded, "
            "as refresh-views does.",
        ),
    ] = True,
) -> None:
    """Ingest every queued 13F, several at a time. Re-run it to resume.

    Works through pending_filing, which discover-filings fills. A filing
    already loaded is skipped without an EDGAR request; with --force it is
    re-parsed from the raw store instead, also without one. A filing that fails
    is reported and recorded on its queue row, and the run carries on.

    Each filing is published as it loads. The materialised views are refreshed
    once, after the last filing, never in the middle of the run.

    Ctrl-C lets the filings in flight finish and starts no more; a second
    Ctrl-C abandons them. Exits 1 if any filing failed or EDGAR blocked the
    run, and 130 if it was interrupted.
    """
    try:
        code = asyncio.run(
            _backfill(
                filer,
                since=since.date() if since else None,
                concurrency=concurrency,
                limit=limit,
                force=force,
                refresh=refresh,
            )
        )
    except UnknownFilerError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure
    except KeyboardInterrupt:
        typer.secho(
            "aborted: the filings in flight were rolled back; re-run to resume",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=_INTERRUPTED) from None
    if code:
        raise typer.Exit(code=code)


async def _backfill(
    slug: str | None,
    *,
    since: date | None,
    concurrency: int,
    limit: int | None,
    force: bool,
    refresh: bool,
) -> int:
    """The job, its progress and summary, then the refresh. Returns the exit code.

    The views are refreshed after the run is recorded, as a ``refresh-views``
    run of its own, whenever a filing loaded, however the backfill ended. A
    run that stopped short has published what it loaded, and the views
    should say so.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    started = time.monotonic()
    total = 0

    def planned(plan: BackfillPlan) -> None:
        nonlocal total
        total = len(plan.work)
        _echo_plan(plan, slug=slug, since=since, concurrency=concurrency)

    progress = Progress(
        planned=planned,
        finished=_progress(lambda: total),
        stopping=_echo_stopping,
    )
    try:
        ran = await ingest_pending_filings(
            settings,
            filer=slug,
            since=since,
            concurrency=concurrency,
            limit=limit,
            force=force,
            progress=progress,
        )
    except JobLocked as locked:
        _echo_locked(locked)
        return 0
    backfilled = ran.value

    _echo_backfill_summary(backfilled.plan, backfilled.ran, elapsed=time.monotonic() - started)
    if refresh and ran.run.items_written:
        await _refresh_after(settings, ran.run.id)
    if backfilled.failed:
        return 1
    return _INTERRUPTED if backfilled.not_started else 0


def _echo_plan(
    plan: BackfillPlan, *, slug: str | None, since: date | None, concurrency: int
) -> None:
    scope = (slug or "every filer") + (f", filed since {since.isoformat()}" if since else "")
    total = len(plan.work) + len(plan.skipped) + plan.held_back
    parts = [f"{total} filings"]
    if plan.skipped:
        parts.append(f"{len(plan.skipped)} already loaded")
    if plan.to_reprocess:
        parts.append(f"{plan.to_reprocess} to reprocess from the raw store")
    if plan.to_ingest:
        parts.append(f"{plan.to_ingest} to ingest")
    if plan.held_back:
        parts.append(f"{plan.held_back} held back by --limit")
    typer.echo(
        f"backfill  {scope}: {', '.join(parts)}"
        + (f" · {concurrency} at a time" if plan.work else "")
    )
    if not total:
        _line("hint", "nothing is queued or loaded; discover-filings finds the work")


def _progress(total: Callable[[], int]) -> Callable[[FilingResult], None]:
    """A printer for ``[347/2103] berkshire-hathaway 2022Q3 · 41 rows · ok``.

    Numbered in the order filings finish, which with several in flight is not
    quite the order they started, so the count always reads as how far along
    the run is. ``total`` is asked as each prints, since the plan that knows
    it is made inside the job.
    """
    finished = 0

    def echo(result: FilingResult) -> None:
        nonlocal finished
        finished += 1
        typer.echo(f"[{finished}/{total()}] {_describe(result)}")

    return echo


def _describe(result: FilingResult) -> str:
    """One filing's line. A failure names its accession number, which is what
    ``ingest-filing`` and every log line about it are keyed on."""
    if result.outcome is Outcome.FAILED:
        return f"{_which(result)} · FAILED · {result.filing.accession_no} · {result.error}"
    rows = f"{result.rows} rows deferred" if result.deferred else f"{result.rows} rows"
    return f"{_which(result)} · {rows} · {result.outcome.value}"


def _which(result: FilingResult) -> str:
    """``berkshire-hathaway 2022Q3``: the filer and quarter, which is how a person
    thinks of a filing, where the accession number is how everything else does."""
    quarter = _quarter(result.period) if result.period is not None else "—"
    return f"{result.filing.label} {quarter}"


def _echo_stopping() -> None:
    typer.secho(
        "stopping: finishing the filings in flight and starting no more; "
        "Ctrl-C again to abandon them",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _echo_backfill_summary(plan: BackfillPlan, run: BackfillRun, *, elapsed: float) -> None:
    """The five numbers the run comes down to, then whatever needs a person."""
    not_started = run.count(Outcome.NOT_STARTED)
    counts = [
        f"succeeded {run.count(Outcome.OK)}",
        f"skipped {len(plan.skipped)}",
        f"failed {run.count(Outcome.FAILED)}",
        f"suspect {run.count(Outcome.SUSPECT)}",
    ]
    if not_started:
        counts.append(f"not started {not_started}")
    counts.append(f"elapsed {_duration(elapsed)}")
    typer.echo(f"backfill  {'stopped' if not_started else 'done'}: {' · '.join(counts)}")

    # Repeated from the progress lines, which a run of two thousand has long
    # since scrolled away.
    for result in run.results:
        if result.outcome is Outcome.FAILED:
            _line("failed", f"{result.filing.accession_no}  {_which(result)}  {result.error}")
    deferred = sum(1 for result in run.results if result.deferred)
    if deferred:
        _line(
            "deferred",
            f"{deferred} loaded without holdings: the CIK is not a known filer yet. "
            "Seed it, then re-run with --force",
        )
    if run.rate_limited is not None:
        _line("error", f"EDGAR blocked the run: {run.rate_limited}. Re-run once it lifts")
    elif not_started:
        _line("interrupted", f"{not_started} not started. Re-run to resume")


def _duration(seconds: float) -> str:
    """``12.4s``, ``3m12s``, ``1h04m``: as precise as a run that long deserves."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, whole_seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{whole_seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# --- ingest-form4 ------------------------------------------------------------


@app.command("ingest-form4")
def ingest_form4_command(
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            metavar="N",
            min=1,
            max=MAX_CONCURRENCY,
            help="Filings in flight at once. All of them share one EDGAR rate limit.",
        ),
    ] = 5,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            metavar="N",
            min=1,
            help="Work on at most N filings. Skipped ones do not count.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Also reload Form 4s already loaded, from their archived documents, "
                "replacing their owners and lines. Makes no EDGAR request for them."
            ),
        ),
    ] = False,
) -> None:
    """Load every queued Form 4 and 4/A: insiders, owners and transaction lines.

    Works through the Form 4s crawl-form4 queued in pending_filing, oldest
    first. Each is fetched (its directory listing, its XML and its header),
    archived, parsed and loaded, and its queue row marked done in the same
    transaction. A filing already loaded is skipped without an EDGAR request.

    A 4/A replaces the Form 4 it amends: the original's lines are deleted,
    whichever of the two loads first.

    First creates insider_transaction's partitions through next year, if any
    is missing, as create-partitions does. Then, if it loaded anything,
    rebuilds the cluster buys from every line, as detect-clusters does.

    A filing that fails is reported and recorded on its queue row, and the run
    carries on. Exits 1 if any filing failed or EDGAR blocked the run, and 130
    if Ctrl-C stopped it.
    """
    try:
        code = asyncio.run(_ingest_form4(concurrency=concurrency, limit=limit, force=force))
    except KeyboardInterrupt:
        typer.secho(
            "aborted: the filings in flight were rolled back; re-run to resume",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=_INTERRUPTED) from None
    if code:
        raise typer.Exit(code=code)


async def _ingest_form4(*, concurrency: int, limit: int | None, force: bool) -> int:
    """The job, its progress and summary. Returns the exit code."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    started = time.monotonic()
    total = 0

    def planned(plan: BackfillPlan) -> None:
        nonlocal total
        total = len(plan.work)
        _echo_form4_plan(plan, concurrency=concurrency)

    progress = Form4Progress(
        partitions_created=_echo_partitions,
        planned=planned,
        finished=_form4_progress(lambda: total),
        stopping=_echo_stopping,
    )
    try:
        ran = await ingest_form4(
            settings, concurrency=concurrency, limit=limit, force=force, progress=progress
        )
    except JobLocked as locked:
        _echo_locked(locked)
        return 0
    ingested = ran.value

    _echo_form4_summary(
        ingested.plan, ingested.ran, ingested.loaded, elapsed=time.monotonic() - started
    )
    if ingested.clusters is not None:
        _echo_clusters(ingested.clusters)
    if ingested.failed:
        return 1
    return _INTERRUPTED if ingested.not_started else 0


def _echo_form4_plan(plan: BackfillPlan, *, concurrency: int) -> None:
    total = len(plan.work) + len(plan.skipped) + plan.held_back
    parts = [_count(total, "Form 4")]
    if plan.skipped:
        parts.append(f"{len(plan.skipped)} already loaded")
    if plan.to_reprocess:
        parts.append(f"{plan.to_reprocess} to reload from the raw store")
    if plan.to_ingest:
        parts.append(f"{plan.to_ingest} to ingest")
    if plan.held_back:
        parts.append(f"{plan.held_back} held back by --limit")
    typer.echo(
        f"ingest-form4  {', '.join(parts)}" + (f" · {concurrency} at a time" if plan.work else "")
    )
    if not total:
        _line("hint", "nothing is queued or loaded; crawl-form4 finds the work")


def _form4_progress(
    total: Callable[[], int],
) -> Callable[[FilingResult, Form4Loaded | None], None]:
    """``[12/280] 0001062993-26-000001 4/A  Elastic N.V. · ABBASI SOHAIB · 3 lines · ESTC``."""
    finished = 0

    def echo(result: FilingResult, entry: Form4Loaded | None) -> None:
        nonlocal finished
        finished += 1
        accession = result.filing.accession_no
        if entry is None:
            typer.echo(
                f"[{finished}/{total()}] {accession} · {result.outcome.value} · {result.error}"
            )
            return
        typer.echo(f"[{finished}/{total()}] {accession} {_form4_line(entry)}")

    return echo


def _form4_line(entry: Form4Loaded) -> str:
    document, result = entry.document, entry.result
    parts = [
        document.issuer.name,
        document.owners[0].name
        + (f" +{len(document.owners) - 1}" if len(document.owners) > 1 else ""),
        _count(result.lines, "line"),
        document.issuer.trading_symbol or "no symbol",
    ]
    if result.security_link is not SecurityLink.LINKED:
        parts.append(f"security {result.security_link.value}")
    if result.amendment is not None:
        parts.append(f"amends: {result.amendment.value}")
    if result.lines_superseded:
        parts.append(f"replaced {_count(result.lines_superseded, 'line')}")
    if result.superseded_by is not None:
        parts.append("superseded by a 4/A")
    return f"{document.document_type:<3}  " + " · ".join(parts)


def _echo_form4_summary(
    plan: BackfillPlan, run: BackfillRun, loaded: dict[str, Form4Loaded], *, elapsed: float
) -> None:
    not_started = run.count(Outcome.NOT_STARTED)
    counts = [
        f"loaded {run.count(Outcome.OK)}",
        f"skipped {len(plan.skipped)}",
        f"failed {run.count(Outcome.FAILED)}",
    ]
    if not_started:
        counts.append(f"not started {not_started}")
    counts.append(f"elapsed {_duration(elapsed)}")
    typer.echo(f"ingest-form4  {'stopped' if not_started else 'done'}: {' · '.join(counts)}")

    results = [entry.result for entry in loaded.values()]
    if results:
        written = sum(result.lines_written for result in results)
        superseded = sum(result.lines_superseded for result in results)
        _line(
            "lines",
            f"{written:,} written" + (f", {superseded:,} replaced by 4/As" if superseded else ""),
        )
        links = Counter(result.security_link for result in results)
        _line(
            "securities",
            ", ".join(f"{links[link]} {link.value}" for link in SecurityLink if links[link]),
        )
        amendments = Counter(result.amendment for result in results if result.amendment)
        if amendments:
            _line(
                "4/As",
                ", ".join(
                    f"{amendments[link]} {link.value}" for link in AmendmentLink if amendments[link]
                ),
            )
    for result in run.results:
        if result.outcome is Outcome.FAILED:
            _line("failed", f"{result.filing.accession_no}  {result.error}")
    if run.rate_limited is not None:
        _line("error", f"EDGAR blocked the run: {run.rate_limited}. Re-run once it lifts")
    elif not_started:
        _line("interrupted", f"{not_started} not started. Re-run to resume")


# --- create-partitions -------------------------------------------------------


@app.command("create-partitions")
def create_partitions_command(
    through: Annotated[
        int | None,
        typer.Option(
            "--through",
            metavar="YEAR",
            min=2020,
            max=9998,
            help="Create every missing year through this one. Default: next year.",
        ),
    ] = None,
) -> None:
    """Create insider_transaction's yearly partitions, through next year.

    Every year from 2020 that has no partition gets one. Lines the default
    partition holds for a year are moved into it as it is created. Does
    nothing when every year exists, which is almost always: ingest-form4 runs
    the same step before every load.

    Creating a partition blocks reads of insider_transaction until it commits,
    which is a moment unless the default partition holds many lines to move.
    """
    asyncio.run(_create_partitions(through if through is not None else next_year()))


async def _create_partitions(through: int) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    async with track_run(settings, "create-partitions", through=through) as run:
        async with session_scope(settings) as session:
            created = await ensure_partitions(session, through=through)
        run.items_written = len(created)
        run.metrics["partitions_created"] = partition_metrics(created)
    if not created:
        typer.echo(f"create-partitions  every year through {through} has a partition")
    else:
        _echo_partitions(created)


def _echo_partitions(created: list[CreatedPartition]) -> None:
    typer.echo(f"create-partitions  {_count(len(created), 'partition')} created")
    for partition in created:
        moved = (
            f"  {_count(partition.rows_moved, 'line')} moved from the default partition"
            if partition.rows_moved
            else ""
        )
        _line(str(partition.year), partition.name + moved)


# --- detect-clusters ---------------------------------------------------------


@app.command("detect-clusters")
def detect_clusters_command() -> None:
    """Rebuild the cluster buys: several of a company's insiders buying within a window.

    Reads every open-market purchase (code P) in insider_transaction by an
    officer or director, outside a 10b5-1, employee stock purchase or dividend
    reinvestment plan. For each issuer, every purchase day anchors a window of
    that day and the days before it, and a window with enough distinct
    insiders qualifies. Overlapping windows are one episode. Every window and
    threshold /v1/insiders/clusters offers is written to insider_cluster,
    which is replaced whole, in one transaction.

    ingest-form4 does this after every run that loads something, so this is
    for after a change to how clusters are detected, and for looking: the
    summary counts the purchases that did not count and why, and lists the
    30-day, 3-insider episodes that are new or changed.

    Then drops every response the API has cached.
    """
    asyncio.run(_detect_clusters())


async def _detect_clusters() -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    async with track_run(settings, "detect-clusters") as run:
        clusters = await rebuild_clusters(settings)
        run.items_seen = clusters.lines.total()
        run.items_written = sum(clusters.written.values())
        run.metrics["clusters"] = cluster_metrics(clusters)
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    _echo_clusters(clusters)


def _echo_clusters(clusters: ClusterRebuild) -> None:
    """The default set's count and changes, then what the purchases came to."""
    changes = [f"{len(clusters.added)} new or changed"]
    if clusters.removed:
        changes.append(f"{len(clusters.removed)} gone")
    typer.echo(
        f"detect-clusters  {_count(clusters.episodes, 'episode')} of "
        f"{DEFAULT_MIN_INSIDERS}+ insiders in {DEFAULT_WINDOW} days: {', '.join(changes)}"
    )
    lines = clusters.lines
    left_out = [
        f"{lines[reason]} {label}"
        for reason, label in (
            ("role", "not by an officer or director"),
            ("10b5_1", "under a 10b5-1 plan"),
            ("plan", "by an ESPP or dividend plan"),
            ("derivative", "of derivatives"),
        )
        if lines[reason]
    ]
    _line(
        "purchases",
        f"{lines['counted']:,} counted" + (f"; left out {', '.join(left_out)}" if left_out else ""),
    )
    for episode in clusters.added:
        days = (episode.window_end - episode.window_start).days + 1
        _line(
            "new",
            f"{episode.window_start} to {episode.window_end} ({_count(days, 'day')})  "
            f"{episode.insider_count} insiders  {episode.issuer_name}",
        )
    for issuer_cik, window_start in clusters.removed:
        _line("gone", f"from {window_start}  CIK {issuer_cik}")


# --- seed-investors ----------------------------------------------------------


@app.command("create-user")
def create_user_command(
    email: Annotated[str, typer.Option("--email", help="The login.")],
    name: Annotated[str, typer.Option("--name", help="The name on the account.")],
    time_zone: Annotated[
        str,
        typer.Option("--time-zone", help="An IANA zone. Default: America/New_York."),
    ] = DEFAULT_TIME_ZONE,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read the password from stdin rather than prompting, for scripts.",
        ),
    ] = False,
) -> None:
    """Make an account, while signups are closed or for a developer.

    The password is prompted for, twice and hidden, or read from stdin with
    --password-stdin: never an option, which would put it in the shell's
    history and the process list. The same rules as registration apply. The
    email counts as verified: whoever runs this vouches for it.
    Untracked: a run row would put the account's email in the operations log.
    """
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        asyncio.run(_create_user(email, name, time_zone, password))
    except CommandError as exc:
        typer.echo(f"create-user: {exc}", err=True)
        raise typer.Exit(1) from None


async def _create_user(email: str, name: str, time_zone: str, password: str) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    email, name = account_rules.normalise_email(email), account_rules.normalise_name(name)
    for field, problem in (
        ("--name", account_rules.name_problem(name)),
        ("--email", account_rules.email_problem(email)),
        ("password", account_rules.password_problem(password)),
    ):
        if problem:
            raise CommandError(f"{field} {problem}")
    async with session_scope(settings) as session:
        if problem := await account_rules.time_zone_problem(session, time_zone):
            raise CommandError(f"--time-zone: {problem}")
        taken = await session.scalar(
            select(AppUser.id).where(AppUser.email == email, AppUser.deleted_at.is_(None))
        )
        if taken is not None:
            raise CommandError(f"{email} already has an account")
        session.add(
            AppUser(
                email=email,
                name=name,
                password_hash=await hash_password(password),
                time_zone=time_zone,
                email_verified_at=func.now(),
            )
        )
    typer.echo(f"create-user  {email}  ({name}, {time_zone})")


@app.command("seed-investors")
def seed_investors_command(
    path: Annotated[
        Path,
        typer.Option(
            "--file",
            help="The investor list to seed from.",
            show_default="data/investors.yaml",
        ),
    ] = DEFAULT_INVESTORS_PATH,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and report what would change. Write nothing."),
    ] = False,
) -> None:
    """Upsert the investor list into filer and filer_cik.

    Idempotent: a second run reports every filer unchanged and adds no CIKs.
    Never deletes, and refuses to move a CIK from one slug to another.
    """
    try:
        asyncio.run(_seed_investors(path, dry_run=dry_run))
    except (InvestorListError, SeedConflictError) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _seed_investors(path: Path, *, dry_run: bool) -> None:
    """Validate first, then write in one transaction.

    The file is validated before the filer tables are touched, so a malformed
    list fails with the problem named, rather than with a constraint violation
    that names a table. Only the run's own row is written before it.

    A dry run is the real run rolled back, not a separate code path: it goes
    through the same upserts and conflict check, so what it prints is what the
    real run will print. The run is recorded either way, with nothing written.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "seed-investors", file=path, dry_run=dry_run) as run:
        entries = load_investors(path)
        run.items_seen = len(entries)

        async with session_scope(settings) as session:
            result = await seed_investors(session, entries)
            if dry_run:
                await session.rollback()
        if not dry_run:
            run.items_written = len(result.created) + len(result.updated)
        # A renamed filer's old slug is in cached lists of a closed period's
        # owners, kept for a day.
        if run.items_written:
            run.metrics["cache_invalidated"] = await invalidate_cache(settings)

        logger.info(
            "investors.seeded",
            created=len(result.created),
            updated=len(result.updated),
            ciks_added=result.ciks_added,
            dry_run=dry_run,
        )
    _echo_seed(path, entries, result, dry_run=dry_run)


def _echo_seed(
    path: Path, entries: tuple[InvestorEntry, ...], result: SeedResult, *, dry_run: bool
) -> None:
    typer.echo(f"seed-investors  {path.name}" + ("  — dry run, nothing written" if dry_run else ""))
    _line(
        "filers",
        f"{len(entries)} listed: {len(result.created)} created, "
        f"{len(result.updated)} updated, {result.unchanged} unchanged",
    )
    _line(
        "ciks",
        f"{result.ciks_listed} listed: {result.ciks_added} added, "
        f"{result.ciks_reprioritised} reprioritised",
    )
    categories = Counter(entry.category.value for entry in entries)
    _line("categories", ", ".join(f"{name} {count}" for name, count in categories.most_common()))
    summed = [entry.slug for entry in entries if entry.overlap is OverlapPolicy.SUM]
    if summed:
        _line("overlap", f"sum: {', '.join(summed)}; every other filer: successor")
    for slug in result.created:
        _line("created", slug)
    for slug in result.updated:
        _line("updated", slug)
    # A CIK the list no longer mentions is still mapped, and still resolving
    # filings to this filer. Printed so that removing one from the file is not
    # mistaken for having removed it from the database.
    for slug, cik in result.unlisted_ciks:
        _line("kept", f"CIK {cik} on {slug} is not in the list — left mapped, not removed")


# --- audit-overlaps ----------------------------------------------------------


@app.command("audit-overlaps")
def audit_overlaps_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Audit one filer instead of all of them."),
    ] = None,
) -> None:
    """Compare filings from a filer's own CIKs that report the same period.

    Reads the loaded holdings, so it only knows about periods that have been
    ingested. For each overlap it says whether the two filings look like one
    book filed twice or two separate books, and whether the filer's overlap
    policy agrees. Writes nothing; exits 0 whatever it finds.
    """
    asyncio.run(_audit_overlaps(filer))


async def _audit_overlaps(slug: str | None) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "audit-overlaps", filer=slug) as run:
        async with session_scope(settings) as session:
            findings = await audit_overlaps(session, slug=slug)
        run.items_seen = len(findings)

    if not findings:
        typer.echo("audit-overlaps  no overlapping periods in the loaded filings")
        return

    conflicts = [finding for finding in findings if finding.conflict is not None]
    filers = len({finding.slug for finding in findings})
    typer.echo(
        f"audit-overlaps  {len(findings)} overlapping periods across {filers} filers, "
        f"{len(conflicts)} disagreeing with their policy"
    )
    for finding in findings:
        _echo_overlap(finding)


def _echo_overlap(finding: OverlapFinding) -> None:
    """Two lines per overlap: the comparison, then what the policy makes of it."""
    typer.echo(
        f"  {finding.slug}  {finding.period.isoformat()}  "
        f"{finding.primary_cik} vs {finding.other_cik}"
    )
    typer.echo(
        f"  {'':<{_LABEL_WIDTH}}{finding.primary_positions} vs {finding.other_positions} "
        f"positions, ${finding.primary_value:,.0f} vs ${finding.other_value:,.0f}, "
        f"{finding.identical_share:.0%} identical -> {finding.verdict.value}"
    )
    verdict = finding.conflict or "agrees"
    typer.echo(f"  {'':<{_LABEL_WIDTH}}policy {finding.policy.value}: {verdict}")


# --- audit-amendments --------------------------------------------------------


@app.command("audit-amendments")
def audit_amendments_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Audit one filer instead of all of them."),
    ] = None,
) -> None:
    """List every filer-period with more than one filing, and how it resolved.

    For each: which filings count toward the period's holdings, which do not,
    and why — replaced by a restatement, added by a new-holdings amendment, left
    out as an amendment of unknown kind. Periods whose resolution deserves a
    look before publishing are marked. Writes nothing; exits 0 whatever it finds.
    """
    asyncio.run(_audit_amendments(filer))


async def _audit_amendments(slug: str | None) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "audit-amendments", filer=slug) as run:
        async with session_scope(settings) as session:
            periods = await audit_amendments(session, slug=slug)
        run.items_seen = len(periods)

    if not periods:
        typer.echo("audit-amendments  no filer has more than one filing for any period")
        return

    flagged = [period for period in periods if period.concerns]
    filers = len({period.slug for period in periods})
    typer.echo(
        f"audit-amendments  {len(periods)} periods with more than one filing across "
        f"{filers} filers, {len(flagged)} to look at"
    )
    for period in periods:
        _echo_period(period)


def _echo_period(period: PeriodResolution) -> None:
    """A line for the period's outcome, one per filing in filed order, then concerns."""
    typer.echo(
        f"  {period.slug}  {_quarter(period.period)}  {period.resolution}: "
        f"{period.positions} positions, ${period.value_usd:,.0f}"
    )
    for filing in period.filings:
        typer.echo(
            f"    {filing.filed_at.date().isoformat()}  {filing.accession_no}  "
            f"{_form(filing):<28}{filing.positions:>6}  ${filing.value_usd:>19,.0f}  "
            f"{filing.reason}"
        )
    for concern in period.concerns:
        typer.secho(f"    ! {concern}", fg=typer.colors.YELLOW)


def _form(filing: PeriodFiling) -> str:
    """``13F-HR/A no.2 new holdings``: the form, and what an amendment claims to be."""
    parts = [filing.form_type]
    if filing.amendment_no is not None:
        parts.append(f"no.{filing.amendment_no}")
    if filing.amendment_kind is not None:
        parts.append(filing.amendment_kind.value.replace("_", " "))
    return " ".join(parts)


# --- check-data --------------------------------------------------------------

#: check-data's exit code when the checks ran and found something. Not 1 for
#: everything: a job that gates a publish on this command has to tell "someone
#: should look at the data" from "the command never looked", and Typer already
#: exits 2 on a bad option.
_FOUND_SOMETHING: Final = 1
_USAGE_ERROR: Final = 2


@app.command("check-data")
def check_data_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Check one filer instead of all of them."),
    ] = None,
    include_suspect: Annotated[
        bool,
        typer.Option(
            "--include-suspect",
            help="Also check suspect periods' positions, as recompute --include-suspect "
            "would publish them.",
        ),
    ] = False,
) -> None:
    """Check the loaded filings against each other before publishing them.

    Lists periods withheld from position_snapshot because a suspect filing
    counts toward them, periods whose top position is over 90% of the
    portfolio, positions whose share count grew more than 10,000% in a quarter
    other than across a split detect-splits has found, and quarters with no 13F
    between a filer's first and last. Most of these fire legitimately — a big
    enough split nothing detected is a 10,000% jump until you look at the
    price — and each is there to be looked at. Writes nothing.
    Exits 1 if anything was found, 2 if --filer names no filer.
    """
    try:
        report = asyncio.run(_check_data(filer, include_suspect=include_suspect))
    except UnknownFilerError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=_USAGE_ERROR) from failure
    if not report.clean:
        raise typer.Exit(code=_FOUND_SOMETHING)


async def _check_data(slug: str | None, *, include_suspect: bool) -> DataCheckReport:
    """Run the checks and print them. A run that finds something is still a
    ``success``: finding things is the job, and the exit code says it did."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(
        settings, "check-data", filer=slug, include_suspect=include_suspect
    ) as run:
        async with session_scope(settings) as session:
            filer_id = await _filer_id(session, slug)
            report = await check_data(session, filer_id=filer_id, include_suspect=include_suspect)
        run.items_seen = report.findings

    _echo_check_data(report, scope=slug or "every filer")
    return report


async def _filer_id(session: AsyncSession, slug: str | None) -> int | None:
    """The filer ``--filer`` names, or ``None`` for every filer.

    :raises UnknownFilerError: No filer has the slug. Raised, not treated as a
        filer with nothing loaded: for a command whose clean result means "safe
        to publish", a typo would otherwise pass every check.
    """
    if slug is None:
        return None
    (filer_id,) = await tracked_filer_ids(session, slug=slug)
    return filer_id


def _echo_check_data(report: DataCheckReport, *, scope: str) -> None:
    """A headline with every count, then each check's findings, in the order they ran."""
    counts = [
        _count(len(report.suspect_periods), "suspect period"),
        _count(len(report.concentrated), "concentrated period"),
        _count(len(report.jumps), "position jump"),
        _count(len(report.gaps), "filing gap"),
    ]
    if report.clean:
        typer.echo(f"check-data  {scope}: nothing to look at — {', '.join(counts)}")
        return
    typer.echo(f"check-data  {scope}: {_count(report.findings, 'finding')} — {', '.join(counts)}")

    if report.suspect_periods:
        published = (
            "checked below as recompute --include-suspect would publish them"
            if report.include_suspect
            else "withheld from position_snapshot"
        )
        typer.echo(f"  suspect periods: {published}")
        for period in report.suspect_periods:
            for filing in period.filings:
                typer.echo(
                    f"    {period.slug}  {_quarter(period.period)}  {filing.accession_no}  "
                    f"{filing.form_type}  failed {', '.join(filing.failed) or 'no recorded guard'}"
                )

    if report.concentrated:
        typer.echo("  concentrated periods: one position over 90% of the period's value")
        for found in report.concentrated:
            typer.echo(
                f"    {found.slug}  {_quarter(found.period)}  "
                f"{_security(found.cusip, found.name)}  {found.weight_pct:.1f}% of "
                f"${found.period_value:,.0f} across {_count(found.positions, 'position')}"
            )

    if report.jumps:
        typer.echo("  position jumps: shares up more than 10,000% on the quarter before")
        for jump in report.jumps:
            typer.echo(
                f"    {jump.slug}  {_quarter(jump.period)}  {_security(jump.cusip, jump.name)}"
            )
            typer.echo(
                f"    {'':<{_LABEL_WIDTH}}{jump.shares_before:,.0f} -> {jump.shares_after:,.0f} "
                f"(+{jump.change:,.0%}), price {_dollars(jump.price_before)} -> "
                f"{_dollars(jump.price_after)}"
            )

    if report.gaps:
        typer.echo("  filing gaps: quarters with no 13F loaded")
        for gap in report.gaps:
            missing = (
                gap.missing[0] if len(gap.missing) == 1 else f"{gap.missing[0]}-{gap.missing[-1]}"
            )
            on_file = (
                f"{_count(gap.unloaded, 'filing')} on file did not load: run backfill"
                if gap.unloaded
                else "nothing on file: check EDGAR, then discover-filings"
            )
            typer.echo(
                f"    {gap.slug}  {missing}  between {gap.after} and {gap.before}; {on_file}"
            )


def _security(cusip: str, name: str | None) -> str:
    return f"{cusip} {name}" if name else cusip


def _dollars(price: Decimal | None) -> str:
    return "n/a" if price is None else f"${price:,.2f}"


def _count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {plural or noun + 's'}"


# --- recompute ---------------------------------------------------------------


#: ``2026Q1``, or ``2026q1``.
_QUARTER: Final = re.compile(r"(\d{4})[Qq]([1-4])")


def _quarter_end(value: str) -> date:
    """``2026Q1`` -> ``2026-03-31``: the ``period_of_report`` of that quarter's 13Fs."""
    match = _QUARTER.fullmatch(value.strip())
    if match is None:
        raise typer.BadParameter(f"{value!r} is not a quarter: expected one like 2026Q1")
    year, quarter = int(match[1]), int(match[2])
    return date(year, 3 * quarter, 31 if quarter in (1, 4) else 30)


@app.command("recompute")
def recompute_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Only this filer's periods."),
    ] = None,
    period: Annotated[
        date | None,
        typer.Option(
            "--period",
            metavar="YYYYQN",
            parser=_quarter_end,
            help="Only this quarter, e.g. 2026Q1.",
        ),
    ] = None,
    everything: Annotated[
        bool,
        typer.Option("--all", help="Every filer's every period."),
    ] = False,
    include_suspect: Annotated[
        bool,
        typer.Option(
            "--include-suspect",
            help="Publish the periods a suspect filing counts toward, every row marked "
            "suspect, instead of withholding them.",
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh-views/--no-refresh-views",
            help="Refresh the materialised views after the rebuild, as refresh-views does.",
        ),
    ] = True,
) -> None:
    """Rebuild position_snapshot, the published portfolio, position_change, and implied_price.

    position_snapshot has one row per security per filer and period, summed over
    the filings that count once amendments and overlapping CIKs are resolved,
    with its weight as a percentage of the period. Common stock only: option
    lines and principal amounts are left out. position_change classifies each
    of those rows against the filer's previous period as new, add, trim or hold,
    with the deltas, and adds an exit for each position the previous period had
    and this one does not. A filer's latest period has no exits until it files
    the next. A period that a suspect filing counts toward is withheld
    from both unless --include-suspect, which publishes it with every row
    marked suspect. implied_price has each security's quarter-end price per
    period, the median of value / shares over the filers that held it, graded
    high, medium or low by how many of them agree.

    Rebuilds the (filer, period) pairs --filer and --period pick out, together
    or alone, or every pair with --all. Each filer's next period after a
    rebuilt one has its position_change rebuilt too, as its deltas start from
    it. The implied price of every security those pairs held, before or after,
    is rebuilt in its period. All of it in one transaction. ingest-filing and backfill do this for
    each filing they load; run it by hand after anything else that changes what
    counts, or with --all after a change to how either table is built.

    Then refreshes the materialised views, which aggregate both tables, unless
    --no-refresh-views. The refresh is recorded as a refresh-views run of its
    own, after the rebuild has committed. Drops every response the API has
    cached after each.

    Exits 1 if --filer names no filer, and 2 without --filer, --period or --all.
    """
    if everything == (filer is not None or period is not None):
        typer.secho(
            "error: --all rebuilds everything, so it takes no --filer or --period"
            if everything
            else "error: say what to rebuild: --filer, --period, or --all",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        asyncio.run(
            _recompute(filer, period=period, include_suspect=include_suspect, refresh=refresh)
        )
    except UnknownFilerError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _recompute(
    slug: str | None, *, period: date | None, include_suspect: bool, refresh: bool
) -> None:
    """Everything when neither ``slug`` nor ``period`` is given, which the command says as --all."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    described = " ".join(
        [*([] if slug is None else [slug]), *([] if period is None else [_quarter(period)])]
    )
    try:
        ran = await recompute_positions(
            settings, filer=slug, period=period, include_suspect=include_suspect
        )
    except JobLocked as locked:
        _echo_locked(locked)
        return
    _echo_rebuild(ran.value, scope=described or "every filer")
    # Even after a rebuild that published nothing: it may have deleted rows
    # that the views still count.
    if refresh:
        await _refresh_after(settings, ran.run.id)


def _echo_rebuild(rebuilt: Recomputed, *, scope: str) -> None:
    """What was published and what changed in it, then what was not, or was without a check."""
    rebuild = rebuilt.snapshot
    typer.echo(
        f"recompute  position_snapshot for {scope}: {_count(rebuild.positions, 'position')} "
        f"in {_count(rebuild.periods, 'period')} of {_count(rebuild.filers, 'filer')}"
    )
    # In the stored vocabulary, which is what a WHERE on the table will spell.
    _line("changes", f"position_change: {_actions(rebuilt.changes)}")
    if rebuilt.following:
        _line(
            "next",
            f"also the changes of {_count(len(rebuilt.following), 'next period')}, "
            f"which start from a rebuilt one: {_quarters(Scope.of(rebuilt.following))}",
        )
    _line("prices", f"implied_price: {_grades(rebuilt.prices)}")
    _line("splits", f"security_split: {_splits(rebuilt.splits)}")
    if rebuilt.crossing:
        _line(
            "across",
            f"also the changes of {_count(len(rebuilt.crossing), 'pair')} of other filers "
            f"across a split that moved: {_quarters(Scope.of(rebuilt.crossing))}",
        )
    if not rebuild.suspect_periods:
        return
    periods = _count(rebuild.suspect_periods, "period")
    if rebuild.include_suspect:
        _line("suspect", f"{periods} with a suspect filing published, every row marked suspect")
    else:
        _line(
            "withheld",
            f"{periods} with a suspect filing — check-data lists them; "
            "--include-suspect publishes them",
        )


def _actions(changes: ChangeRebuild) -> str:
    adjusted = f" ({changes.split:,} across a split)" if changes.split else ""
    return (
        f"{changes.new:,} new, {changes.add:,} add, {changes.trim:,} trim, "
        f"{changes.hold:,} hold, {changes.exit:,} exit{adjusted}"
    )


def _splits(splits: SplitRebuild) -> str:
    """The splits detected, and how many of them are new, gone or re-rated since the last time."""
    moved = (
        f"{len(splits.added):,} new, {len(splits.removed):,} gone, {len(splits.rerated):,} re-rated"
    )
    return f"{_count(len(splits.splits), 'split')} ({moved})"


def _grades(prices: PriceRebuild) -> str:
    """The implied prices rebuilt, by confidence, and those no holder put a value on."""
    return (
        f"{_count(prices.prices, 'price')} ({prices.high:,} high, "
        f"{prices.medium:,} medium, {prices.low:,} low, {prices.unpriced:,} unpriced)"
    )


def _quarters(scope: Scope) -> str:
    """The quarters a scope's pairs are in, each once, oldest first: ``2023Q3, 2023Q4``."""
    pairs: frozenset[Pair] = scope.pairs or frozenset()
    return ", ".join(_quarter(period) for period in sorted({period for _, period in pairs}))


# --- detect-splits -----------------------------------------------------------


@app.command("detect-splits")
def detect_splits_command(
    period: Annotated[
        date | None,
        typer.Option(
            "--period",
            metavar="YYYYQN",
            parser=_quarter_end,
            help="Only the splits dated this quarter, e.g. 2024Q2: the first after the split.",
        ),
    ] = None,
    everything: Annotated[
        bool,
        typer.Option("--all", help="Every security in every quarter."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be detected and rebuilt. Write nothing."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh-views/--no-refresh-views",
            help="Refresh the materialised views after the rebuild, as refresh-views does.",
        ),
    ] = True,
) -> None:
    """Detect stock splits from the holders' filings, and reclassify the changes across them.

    A split is a quarter in which the holders' share counts moved by a common
    ratio (3:2, 2:1, 3:1, 4:1, 5:1, 10:1, 20:1, 25:1, 50:1, or the reverse) and
    the implied price moved the other way. position_change compares a change
    across one against the previous shares times the ratio, so a holder who
    held through a 10:1 is a hold, not a +900% add.

    recompute does this for what it rebuilds, so this is for after a change
    to how splits are detected, and for looking: --dry-run lists every split
    it would write, and the outliers, quarters whose share counts and price
    moved apart like a split's without being detected as one. Look at both
    before the first real run rewrites five years of position_change.

    Writes security_split for --period or --all, then rebuilds the changes of
    every filer across a split that appeared, disappeared or changed its
    ratio, in one transaction. A second run finds nothing moved and rebuilds
    nothing. Then refreshes the materialised views, unless --no-refresh-views,
    and drops every response the API has cached.

    Exits 2 without --period or --all.
    """
    if everything == (period is not None):
        typer.secho(
            "error: --all detects everywhere, so it takes no --period"
            if everything
            else "error: say where to detect: --period or --all",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    asyncio.run(_detect_splits(period, dry_run=dry_run, refresh=refresh))


async def _detect_splits(period: date | None, *, dry_run: bool, refresh: bool) -> None:
    """One transaction, rolled back for a dry run, which is otherwise the same run."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(
        settings, "detect-splits", period=period, dry_run=dry_run, refresh_views=refresh
    ) as run:
        async with session_scope(settings) as session:
            found = await detect_splits(session, period=period)
            names = await _security_names(
                session,
                {
                    verdict.candidate.security_id
                    for verdict in (*found.splits.splits, *found.splits.outliers)
                }
                | {security_id for security_id, _ in found.splits.removed},
            )
            if dry_run:
                await session.rollback()
        splits = found.splits
        run.items_seen = len(splits.splits)
        if not dry_run:
            run.items_written = len(splits.moved)
            run.metrics["cache_invalidated"] = await invalidate_cache(settings)
        run.metrics["crossing_pairs"] = len(found.crossing)
        run.metrics["outliers"] = len(splits.outliers)

        logger.info(
            "security_split.detected",
            period=None if period is None else _quarter(period),
            splits=len(splits.splits),
            added=len(splits.added),
            removed=len(splits.removed),
            rerated=len(splits.rerated),
            outliers=len(splits.outliers),
            crossing_pairs=len(found.crossing),
            split_changes=found.changes.split,
            dry_run=dry_run,
        )
    _echo_detected(
        found, names, scope="every quarter" if period is None else _quarter(period), dry_run=dry_run
    )
    if refresh and not dry_run:
        await _refresh_after(settings, run.id)


async def _security_names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """Each security as a person looks it up: its ticker if it has one, and its CUSIP."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Security.id, Security.ticker, Security.cusip).where(Security.id.in_(sorted(ids)))
    )
    return {
        security_id: f"{ticker or '-':<6} {cusip}" for security_id, ticker, cusip in rows.tuples()
    }


def _echo_detected(found: Redetected, names: dict[int, str], *, scope: str, dry_run: bool) -> None:
    """The splits, marked where they differ from before, then the changes rebuilt, then outliers."""
    splits = found.splits
    rehearsal = " (dry run: nothing written)" if dry_run else ""
    typer.echo(f"detect-splits  {scope}: {_splits(splits)}{rehearsal}")
    for verdict in splits.splits:
        key = verdict.candidate.key
        mark = "new" if key in splits.added else "re-rated" if key in splits.rerated else ""
        typer.echo(f"  {mark:<9}{_verdict(verdict, names)}  {verdict.confidence}")
    for security_id, period in sorted(splits.removed, key=lambda key: (key[1], key[0])):
        typer.echo(f"  {'gone':<9}{_quarter(period)}  {names.get(security_id, security_id)}")
    would = "would rebuild" if dry_run else "rebuilt"
    typer.echo(
        f"  changes  {would} {_count(len(found.crossing), 'pair')} across a split that moved: "
        f"{_actions(found.changes)}"
    )
    if splits.outliers:
        typer.echo(
            f"  outliers {_count(len(splits.outliers), 'quarter')} whose share counts and price "
            "moved apart like a split's, not detected as one: not adjusted for"
        )
        for verdict in splits.outliers:
            typer.echo(f"           {_verdict(verdict, names)}  {verdict.reason}")


def _verdict(verdict: Verdict, names: dict[int, str]) -> str:
    """One line of evidence: quarter, security, ratio, holders agreeing, and the ratios."""
    candidate = verdict.candidate
    ratio = "-" if candidate.fraction is None else ratio_label(candidate.fraction)
    price = "n/a" if candidate.price_ratio is None else f"x{candidate.price_ratio:.4f}"
    return (
        f"{_quarter(candidate.period_of_report)}  "
        f"{names.get(candidate.security_id, str(candidate.security_id)):<16}  {ratio:>5}  "
        f"{candidate.agreeing:>3} of {candidate.holders:<3} holders, {candidate.exact} exact  "
        f"shares x{candidate.shares_ratio:.3f}, price {price}"
    )


# --- refresh-views -----------------------------------------------------------


#: What ``--view`` takes.
_VIEW_NAMES: Final = tuple(view.name for view in MATERIALISED_VIEWS)


def _view_name(value: str) -> str:
    if value not in _VIEW_NAMES:
        raise typer.BadParameter(
            f"{value!r} is not a materialised view: expected one of {', '.join(_VIEW_NAMES)}"
        )
    return value


@app.command("refresh-views")
def refresh_views_command(
    view: Annotated[
        str | None,
        typer.Option(
            "--view",
            metavar="NAME",
            parser=_view_name,
            help=f"Only this view, and any view that reads it: {', '.join(_VIEW_NAMES)}.",
        ),
    ] = None,
    concurrent: Annotated[
        bool,
        typer.Option(
            "--concurrent/--no-concurrent",
            help="Let reads of each view go on while it is refreshed. --no-concurrent is "
            "faster, but blocks reads of each view until the last one is done.",
        ),
    ] = True,
) -> None:
    """Refresh the materialised views the market-wide and per-filer reads are served from.

    mv_consensus_holdings, mv_quarter_flows, mv_filer_summary, mv_year_flows
    and mv_filing_feed aggregate position_snapshot and position_change, and
    each is as of its last refresh.
    recompute runs this after every rebuild, ingest-filing after a load that
    publishes, and backfill once at the end. Run it by hand after any of them
    was told --no-refresh-views.

    Concurrently by default, so reads of the views go on while it runs. It
    waits for any recompute in progress and holds off the next until it
    commits, so the views are refreshed from the same tables. A view that
    reads another is refreshed after it. Each view's refresh time is recorded
    in matview_refresh, and how long it took in this run's ingestion_run row.
    Then drops every response the API has cached.
    """
    asyncio.run(_refresh_views(view, concurrent=concurrent))


async def _refresh_views(view: str | None, *, concurrent: bool) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    await _refresh(settings, view=view, concurrent=concurrent)


async def _refresh_after(settings: Settings, after: uuid.UUID) -> None:
    """``refresh-views`` after a run that published, naming that run."""
    await _refresh(settings, after=after)


async def _refresh(
    settings: Settings,
    *,
    view: str | None = None,
    concurrent: bool = True,
    after: uuid.UUID | None = None,
) -> None:
    """One ``refresh-views`` run, by hand or after a run that published.

    A refresh already running is left to finish what this one would have
    done; this one says so and does nothing.
    """
    started = time.perf_counter()
    try:
        ran = await refresh_matviews(settings, view=view, concurrent=concurrent, after_run_id=after)
    except JobLocked as locked:
        _echo_locked(locked)
        return
    _echo_refreshed(ran.value.views, elapsed=time.perf_counter() - started, concurrent=concurrent)


def _echo_refreshed(refreshed: list[Refreshed], *, elapsed: float, concurrent: bool) -> None:
    width = max(len(view.name) for view in refreshed)
    blocking = "" if concurrent else ", not concurrently"
    typer.echo(
        f"refresh-views  {_count(len(refreshed), 'materialised view')} refreshed "
        f"in {_duration(elapsed)}{blocking}"
    )
    for view in refreshed:
        # Asked to be concurrent, only the first refresh of a view never
        # populated is not.
        how = "  (first refresh, not concurrent)" if concurrent and not view.concurrently else ""
        typer.echo(
            f"  {view.name:<{width}}  {_count(view.rows, 'row'):>14}  "
            f"{_duration(view.seconds):>6}{how}"
        )


# --- reconcile ---------------------------------------------------------------

#: Wide enough for every invariant's name, so the rules after it line up.
_INVARIANT_WIDTH: Final = max(len(invariant.name) for invariant in INVARIANTS)


@app.command("reconcile")
def reconcile_command(
    include_suspect: Annotated[
        bool,
        typer.Option(
            "--include-suspect",
            help="The tables were rebuilt with recompute --include-suspect: allow periods a "
            "suspect filing counts toward, as long as every row of one is marked suspect.",
        ),
    ] = False,
    sample: Annotated[
        int,
        typer.Option(
            "--sample", metavar="N", min=1, help="How many rows to show of each failing invariant."
        ),
    ] = SAMPLE,
) -> None:
    """Check that the published tables add up: against each other, the filings and the views.

    Reads position_snapshot, position_change and the materialised views as
    they are stored, and checks every invariant they must satisfy: weights that
    sum to 100, positions that trace to a non-suspect filing, a change for every
    position and an exit for every one sold out of, value deltas that add up to
    each portfolio's change in value, and views that hold what their queries
    return now. Lists each invariant that fails, with the rows that break it.
    Writes nothing but the record of its run. Exits 1 if any invariant fails.
    """
    report = asyncio.run(_reconcile(include_suspect=include_suspect, sample=sample))
    if not report.clean:
        raise typer.Exit(code=_FOUND_SOMETHING)


async def _reconcile(*, include_suspect: bool, sample: int) -> Reconciliation:
    """Check and print. As for ``check-data``, a run that finds something is a
    ``success``: finding things is the job, and the exit code says it did."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "reconcile", include_suspect=include_suspect) as run:
        async with session_scope(settings) as session:
            # Every invariant reads the same snapshot of the database. Otherwise
            # a rebuild that commits between two of them reads as the two
            # tables disagreeing.
            snapshot = {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
            await session.connection(execution_options=snapshot)
            report = await reconcile(session, include_suspect=include_suspect, sample=sample)
        run.items_seen = report.positions + report.changes
        run.metrics["violations"] = {
            checked.invariant.name: checked.violations for checked in report.checked
        }

    _echo_reconciliation(report)
    return report


def _echo_reconciliation(report: Reconciliation) -> None:
    """A headline with the verdict, then every invariant, each failing one with its rows."""
    total = len(report.checked)
    verdict = (
        f"all {total} invariants hold"
        if report.clean
        else f"{len(report.failed)} of {total} invariants fail"
    )
    published = ", published with --include-suspect" if report.include_suspect else ""
    typer.echo(
        f"reconcile  {_count(report.positions, 'position')} and "
        f"{_count(report.changes, 'change')} in {_count(report.periods, 'period')}"
        f"{published}: {verdict}"
    )
    for checked in report.checked:
        invariant = checked.invariant
        status = "ok  " if checked.holds else "FAIL"
        typer.echo(f"  {status}  {invariant.name:<{_INVARIANT_WIDTH}}  {invariant.rule}")
        if checked.holds:
            continue
        typer.echo(f"{'':8}{_count(checked.violations, 'row')} break it:")
        for violation in checked.sample:
            where = [
                part
                for part in (
                    violation.slug,
                    None if violation.period is None else _quarter(violation.period),
                    violation.cusip,
                )
                if part is not None
            ]
            typer.echo(f"{'':10}{'  '.join([*where, violation.problem])}")
            if violation.detail:
                typer.echo(f"{'':12}{violation.detail}")
        if checked.violations > len(checked.sample):
            typer.echo(f"{'':10}and {checked.violations - len(checked.sample):,} more")


# --- runs --------------------------------------------------------------------

#: Wide enough for every job name in use, so the columns after it line up.
_JOB_WIDTH: Final = 16

#: How much of a run's first error fits on its line under the run.
_ERROR_WIDTH: Final = 90


@app.command("runs")
def runs_command(
    job: Annotated[
        str | None,
        typer.Option("--job", metavar="NAME", help="Only runs of this job, e.g. backfill_13f."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", metavar="N", min=1, help="How many runs to list."),
    ] = 20,
) -> None:
    """List the most recent job runs, newest first, from ingestion_run.

    One line per run: when it started, the job, how it ended, how long it took,
    and what it counted. Under a run that did not succeed, the first line of
    its error. A run still marked running long after it started is a process
    that died without recording why. Writes nothing, and is not itself a run.
    """
    asyncio.run(_runs(job, limit=limit))


async def _runs(job_name: str | None, *, limit: int) -> None:
    """Not tracked: a listing that added a row to what it lists would show
    itself first, every time."""
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with session_scope(settings) as session:
        runs = await recent_runs(session, job_name=job_name, limit=limit)
        known = await job_names(session) if not runs and job_name is not None else []

    scope = f" of {job_name}" if job_name is not None else ""
    if not runs:
        # A name that matches nothing is more often a misremembered name than
        # a job that never ran, so say which names do match.
        hint = f"; jobs with runs: {', '.join(known)}" if known else ""
        typer.echo(f"runs  no runs{scope} recorded{hint}")
        return
    typer.echo(f"runs  {_count(len(runs), 'most recent run')}{scope}")
    typer.echo(
        f"  {'started':<25}  {'job':<{_JOB_WIDTH}}  {'status':<7}  {'elapsed':>7}  "
        f"{'seen':>6}  {'written':>7}  run_id"
    )
    for run in runs:
        _echo_run(run)


def _echo_run(run: RunSummary) -> None:
    """The run's line and, when it did not succeed, why, with how much more there is."""
    typer.echo(
        f"  {_instant(run.started_at):<25}  {run.job_name:<{_JOB_WIDTH}}  {run.status:<7}  "
        f"{_duration(run.elapsed.total_seconds()):>7}  {run.items_seen:>6}  "
        f"{run.items_written:>7}  {run.id}"
    )
    if run.error:
        first, *rest = run.error.splitlines()
        more = f"  (+{len(rest)} more)" if rest else ""
        typer.echo(f"  {'':<25}  {_truncate(first, _ERROR_WIDTH)}{more}")


# --- verify-investors --------------------------------------------------------

#: Column order for ``--csv``. A contract with whatever reads the file, so
#: append rather than reorder.
_CSV_COLUMNS: Final = (
    "status",
    "slug",
    "cik",
    "current",
    "listed_name",
    "edgar_name",
    "name_similarity",
    "filings_13f_hr",
    "earliest_period",
    "latest_period",
    "failures",
    "warnings",
    "error",
)

#: Enough of EDGAR's name to recognise it without pushing the flags off screen.
_TABLE_NAME_WIDTH: Final = 36


@app.command("verify-investors")
def verify_investors_command(
    path: Annotated[
        Path,
        typer.Option(
            "--file",
            help="The investor list to verify.",
            show_default="data/investors.yaml",
        ),
    ] = DEFAULT_INVESTORS_PATH,
    csv_path: Annotated[
        Path | None,
        typer.Option(
            "--csv",
            metavar="PATH",
            help="Also write the report as CSV to PATH. '-' writes CSV to stdout instead "
            "of the table.",
        ),
    ] = None,
    as_of: Annotated[
        datetime | None,
        typer.Option(
            "--as-of",
            formats=["%Y-%m-%d"],
            help="Judge staleness as of this date rather than today.",
        ),
    ] = None,
) -> None:
    """Check every CIK in the investor list against EDGAR's submissions index.

    Reports EDGAR's name, the 13F-HR count and the earliest and latest period
    per CIK. Exits 1 if any CIK is missing from EDGAR, has never filed a 13F-HR,
    could not be fetched, or is a current CIK that has stopped filing. Name
    mismatches and stale predecessor CIKs are printed for review but pass.

    Makes one or more requests to data.sec.gov per CIK. Needs no database.
    """
    try:
        failed = asyncio.run(
            _verify_investors(
                path, csv_path=csv_path, today=as_of.date() if as_of else date.today()
            )
        )
    except (InvestorListError, EdgarRateLimited, OSError) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure
    if failed:
        raise typer.Exit(code=1)


async def _verify_investors(path: Path, *, csv_path: Path | None, today: date) -> bool:
    """Run the checks and print them. Returns whether any CIK failed.

    The one job not recorded in ``ingestion_run``, because it has no database
    to record it in: it runs on the host and in a GitHub workflow, against
    EDGAR and the YAML file only. Its record is the workflow's run history and
    the CSV that run uploads.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    structlog.contextvars.bind_contextvars(job_name="verify-investors")

    entries = load_investors(path)
    async with EdgarClient(settings) as edgar:
        checks = await verify_investors(edgar, entries, today=today)

    failed = sum(1 for check in checks if check.failures)
    logger.info("investors.verified", ciks=len(checks), failed=failed)

    to_stdout = csv_path is not None and str(csv_path) == "-"
    if to_stdout:
        _write_csv(checks, sys.stdout)
    else:
        _echo_verification(path, entries, checks, today=today)
    if csv_path is not None and not to_stdout:
        with csv_path.open("w", newline="", encoding="utf-8") as out:
            _write_csv(checks, out)
    return failed > 0


def _echo_verification(
    path: Path, entries: tuple[InvestorEntry, ...], checks: tuple[CikCheck, ...], *, today: date
) -> None:
    typer.echo(
        f"verify-investors  {path.name}  as of {today.isoformat()}, "
        f"stale before {stale_cutoff(today).isoformat()}"
    )
    slug_width = max((len(check.slug) for check in checks), default=4)
    header = (
        f"  {'':<4}  {'slug':<{slug_width}}  {'cik':<10}  {'13F-HR':>6}  "
        f"{'earliest':<10}  {'latest':<10}  {'sim':>4}  {'edgar name':<{_TABLE_NAME_WIDTH}}  flags"
    )
    typer.echo(header)
    for check in checks:
        typer.echo(
            f"  {check.status:<4}  {check.slug:<{slug_width}}  {check.cik:<10}  "
            f"{check.thirteen_f_count:>6}  {_iso(check.earliest_period):<10}  "
            f"{_iso(check.latest_period):<10}  {_similarity(check):>4}  "
            f"{_truncate(check.edgar_name or '-', _TABLE_NAME_WIDTH):<{_TABLE_NAME_WIDTH}}  "
            f"{_flags(check)}".rstrip()
        )

    failed = [check for check in checks if check.failures]
    warned = [check for check in checks if check.warnings and not check.failures]
    typer.echo(
        f"  {len(entries)} filers, {len(checks)} CIKs: {len(failed)} failed, "
        f"{len(warned)} with warnings, {len(checks) - len(failed) - len(warned)} ok"
    )
    for check in checks:
        if check.error is not None:
            _line("error", f"{check.slug} CIK {check.cik}: {check.error}")


def _flags(check: CikCheck) -> str:
    """Failures in capitals, so they stand out from warnings in a long table."""
    flags = [flag.value.upper() for flag in check.failures] + [
        flag.value for flag in check.warnings
    ]
    if not check.current:
        flags.append("(predecessor)")
    return " ".join(flags)


def _write_csv(checks: tuple[CikCheck, ...], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(_CSV_COLUMNS)
    for check in checks:
        writer.writerow(
            (
                check.status,
                check.slug,
                check.cik,
                "true" if check.current else "false",
                check.listed_name,
                check.edgar_name or "",
                "" if check.name_similarity is None else f"{check.name_similarity:.2f}",
                check.thirteen_f_count,
                _iso(check.earliest_period, blank=""),
                _iso(check.latest_period, blank=""),
                ";".join(check.failures),
                ";".join(check.warnings),
                check.error or "",
            )
        )


def _similarity(check: CikCheck) -> str:
    return "-" if check.name_similarity is None else f"{check.name_similarity:.2f}"


def _iso(day: date | None, *, blank: str = "-") -> str:
    return blank if day is None else day.isoformat()


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# --- import-13f-list ---------------------------------------------------------


@app.command("import-13f-list")
def import_13f_list_command(
    period: Annotated[
        date,
        typer.Option(
            "--quarter",
            metavar="YYYYQN",
            parser=_quarter_end,
            help="The quarter whose list to import, e.g. 2026Q1.",
        ),
    ],
    path: Annotated[
        Path | None,
        typer.Option(
            "--file",
            metavar="PATH",
            help="Import this copy of the list instead of finding it on the SEC's page. "
            "It is archived like a download.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Fetch the list from the SEC again, or archive --file, replacing the "
            "copy already archived.",
        ),
    ] = False,
) -> None:
    """Import the SEC's Official List of Section 13(f) Securities for one quarter.

    Finds the quarter's text file on the SEC's 13(f) list page, archives it,
    and replaces that quarter's rows in sec_13f_security. Other quarters are
    left alone. A quarter already archived is imported from the archive, with
    no request to the SEC, unless --force.

    Refuses a list whose additions were mostly on the previous quarter's list
    already, which means the file is another quarter's. The check needs the
    previous quarter imported first. Quarters before 2020Q1 are PDF only and
    are refused.
    """
    try:
        asyncio.run(_import_13f_list(period, path=path, force=force))
    except (
        CommandError,
        ThirteenFListUnavailableError,
        ThirteenFListParseError,
        EdgarRateLimited,
        EdgarServerError,
        httpx.HTTPError,
        RawStoreError,
    ) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


@dataclass(frozen=True, slots=True)
class _ListCopy:
    """One quarter's list file, and where this run got it."""

    body: bytes
    key: str
    source: str
    """The URL it was downloaded from, the file it was read from, or ``archive``."""

    archived: bool
    """Whether this run wrote it to the archive."""


#: How the raw store is told a list file is text.
_LIST_CONTENT_TYPE: Final = "text/plain"


async def _import_13f_list(period: date, *, path: Path | None, force: bool) -> None:
    """Get the file, archive it, parse it, check it, write it: in that order.

    Archived before it is parsed, as a filing is, so a list that fails to
    parse or is refused is already kept for the fix.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    label = quarter_label(period)

    async with track_run(settings, "import-13f-list", quarter=label, file=path, force=force) as run:
        async with open_raw_store(settings) as store:
            if path is not None:
                copy = await _list_from_file(store, period, path, force=force)
            else:
                copy = await _list_from_sec(settings, store, period, force=force)

        parsed = parse_13f_list(copy.body)
        run.items_seen = parsed.lines

        async with session_scope(settings) as session:
            check = await check_additions(session, period, parsed)
            if not check.passed:
                raise CommandError(
                    f"{label}'s list marks {check.added:,} CUSIPs added, and "
                    f"{check.already_listed:,} of them were already on "
                    f"{quarter_label(check.previous)}'s list: it is probably another "
                    f"quarter's file. Nothing was written. It is archived at {copy.key}"
                )
            replaced = await load_13f_list(session, period, parsed)
            # The lists are what says a security is no longer listed.
            delistings = await flag_delistings(session)
        run.items_written = len(parsed.securities)

        # The API serves delisted_by, and the ticker ranges a delisting ends.
        if delistings.flagged or delistings.relisted:
            run.metrics["cache_invalidated"] = await invalidate_cache(settings)
        run.metrics |= {
            "delisted_flagged": delistings.flagged,
            "relisted": delistings.relisted,
            "source": copy.source,
            "raw_key": copy.key,
            "archived": copy.archived,
            "collapsed": parsed.collapsed,
            "added": parsed.count(ListStatus.ADDED),
            "deleted": parsed.count(ListStatus.DELETED),
            "replaced": replaced,
            "checked_against": quarter_label(check.previous) if check.previous_imported else None,
            "already_listed": check.already_listed,
        }
        logger.info(
            "thirteen_f_list.imported",
            quarter=label,
            securities=len(parsed.securities),
            lines=parsed.lines,
            collapsed=parsed.collapsed,
            replaced=replaced,
            raw_key=copy.key,
        )
    _echo_13f_list(period, copy, parsed, check, replaced=replaced, delistings=delistings)


async def _list_from_sec(
    settings: Settings, store: RawStore, period: date, *, force: bool
) -> _ListCopy:
    """The archived copy if there is exactly one, else whatever the SEC's page links.

    With one copy archived, no request is made. With none, or with several (the
    SEC renamed a file it had to correct), the page decides which is current,
    and that one is read from the archive if it is there.
    """
    archived = await store.list(thirteen_f_list_prefix(period))
    if len(archived) == 1 and not force:
        return _ListCopy(await store.get(archived[0]), archived[0], "archive", archived=False)

    async with EdgarClient(settings) as edgar:
        url = await find_list_url(edgar, period)
        key = thirteen_f_list_key(period, url.rsplit("/", 1)[-1])
        if key in archived and not force:
            return _ListCopy(await store.get(key), key, "archive", archived=False)
        body = await edgar.get_bytes(url)
    await store.put(key, body, _LIST_CONTENT_TYPE, overwrite=force)
    return _ListCopy(body, key, url, archived=True)


async def _list_from_file(store: RawStore, period: date, path: Path, *, force: bool) -> _ListCopy:
    """``--file``, archived under its own name like a download.

    Refused if its name says it is another quarter's, or if a different file of
    the same name is archived already and there is no --force to replace it:
    importing one file while the archive keeps another would make the archive
    wrong about what was imported.
    """
    label = quarter_label(period)
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError as failure:
        raise CommandError(f"could not read {path}: {failure.strerror or failure}") from failure
    named = filename_quarter(path.name)
    if named is not None and named != period:
        raise CommandError(
            f"{path.name} is {quarter_label(named)}'s list by its name, not {label}'s"
        )
    try:
        key = thirteen_f_list_key(period, path.name)
    except ValueError as failure:
        raise CommandError(f"{path.name!r} cannot be archived under that name") from failure
    if not force and await store.exists(key) and await store.get(key) != body:
        raise CommandError(f"a different {path.name} is archived at {key}; --force replaces it")
    await store.put(key, body, _LIST_CONTENT_TYPE, overwrite=force)
    return _ListCopy(body, key, str(path), archived=True)


def _echo_13f_list(
    period: date,
    copy: _ListCopy,
    parsed: ThirteenFList,
    check: AdditionsCheck,
    *,
    replaced: int,
    delistings: Delistings,
) -> None:
    typer.echo(f"import-13f-list  {quarter_label(period)}  {copy.key.rsplit('/', 1)[-1]}")
    _line("source", copy.key if copy.source == "archive" else copy.source)
    _line("archived", copy.key if copy.archived else "already archived")
    lines = f"{parsed.lines:,} read"
    if parsed.collapsed:
        lines += f", {parsed.collapsed:,} repeating a CUSIP and collapsed"
    _line("lines", lines)
    listed_options = sum(1 for security in parsed.securities if security.has_listed_options)
    _line(
        "securities",
        f"{len(parsed.securities):,}: {parsed.count(ListStatus.LISTED):,} listed, "
        f"{parsed.count(ListStatus.ADDED):,} added, {parsed.count(ListStatus.DELETED):,} deleted; "
        f"{listed_options:,} with listed options",
    )
    previous = quarter_label(check.previous)
    if not check.previous_imported:
        _line("checked", f"not checked: {previous} is not imported")
    else:
        already = f"{check.already_listed:,}" if check.already_listed else "none"
        _line("checked", f"{check.added:,} additions against {previous}: {already} already listed")
    _line("replaced", _count(replaced, "row") if replaced else "nothing, first import")
    _line(
        "delisted",
        f"{delistings.flagged:,} securities newly off the list, {delistings.relisted:,} back on",
    )


# --- resolve-cusips ------------------------------------------------------------


@app.command("resolve-cusips")
def resolve_cusips_command(
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            metavar="N",
            help="Only the N securities of highest aggregate holdings value.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Every security, resolved or not, asking OpenFIGI again rather than "
            "reusing its logged answers. Manual resolutions are still left alone.",
        ),
    ] = False,
) -> None:
    """Resolve unresolved securities to tickers, highest aggregate holdings value first.

    OpenFIGI first, for a US equity listing's ticker, FIGI and name. Then the
    SEC's 13(f) list, for a name only. Then a trigram match on that name
    against securities with tickers, which is never applied: each candidate is
    written to the manual_review queue. security.resolution_source records
    which source won.

    OpenFIGI's answers are reused from figi_response_log, except misses more
    than 30 days old, which are asked again. A security resolved with
    resolve-cusip is never touched, with or without --force. Then drops every
    response the API has cached.
    """
    try:
        asyncio.run(_resolve_cusips(limit=limit, force=force))
    except (
        OpenFIGIRateLimited,
        OpenFIGIServerError,
        OpenFIGIResponseError,
        httpx.HTTPError,
    ) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _resolve_cusips(*, limit: int | None, force: bool) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    started = time.perf_counter()
    try:
        ran = await resolve_cusips(settings, limit=limit, force=force)
    except JobLocked as locked:
        _echo_locked(locked)
        return
    _echo_resolved(
        ran.value.report,
        ran.value.coverage,
        limit=limit,
        force=force,
        elapsed=time.perf_counter() - started,
    )


def _echo_resolved(
    report: ResolutionReport,
    coverage: CusipCoverage,
    *,
    limit: int | None,
    force: bool,
    elapsed: float,
) -> None:
    which = "securities" if force else "unresolved securities"
    if limit is not None:
        which += f", the top {limit:,} by value"
    typer.echo(
        f"resolve-cusips  {report.candidates:,} {which}, {_held(report.exposure)} held, "
        f"in {_duration(elapsed)}" + ("  — --force: OpenFIGI asked again" if force else "")
    )
    if not report.candidates:
        _line("nothing", "every security has a ticker" if not force else "no securities")
    else:
        _line(
            "openfigi",
            f"{report.count(ResolutionSource.OPENFIGI):,} given a ticker "
            f"({_held(report.value(ResolutionSource.OPENFIGI))})",
        )
        _line(
            "13(f) list",
            f"{report.count(ResolutionSource.SEC_13F_LIST):,} named, no ticker "
            f"({_held(report.value(ResolutionSource.SEC_13F_LIST))})",
        )
        _line(
            "neither",
            f"{report.count(None):,} left with the name their filing gave "
            f"({_held(report.value(None))})",
        )
        _line("changed", _count(report.changed, "row"))
    if report.misses_retried:
        _line("retried", f"{report.misses_retried:,} OpenFIGI misses older than 30 days")
    if report.kept:
        _line("kept", f"{report.kept:,} OpenFIGI no longer lists keep the ticker they had")
    if report.renamed:
        _line("renamed", f"{report.renamed:,} under a new ticker; the old one's range ends today")
    if report.delistings is not None:
        _echo_delistings(report.delistings, coverage)
    if coverage.collisions:
        _line(
            "collisions",
            f"{_count(len(coverage.collisions), 'ticker')} held by two securities at once; "
            "see cusip-coverage",
        )
    _line(
        "review",
        f"{_count(report.reviews_queued, 'fuzzy match', 'fuzzy matches')} queued for "
        f"{_count(report.reviews_queued_for, 'security', 'securities')}; "
        f"{coverage.pending_reviews:,} pending in all",
    )
    if report.reviews_superseded:
        _line("", f"{report.reviews_superseded:,} pending closed: resolved by OpenFIGI")
    _line(
        "coverage",
        f"{_pct_text(coverage.value_pct)} by value, {_pct_text(coverage.row_pct)} by row",
    )


def _echo_delistings(delistings: Delistings, coverage: CusipCoverage) -> None:
    changes = []
    if delistings.flagged:
        changes.append(f"{delistings.flagged:,} newly")
    if delistings.relisted:
        changes.append(f"{delistings.relisted:,} listed again")
    _line(
        "delisted",
        f"{_count(coverage.delisted, 'security', 'securities')} no longer on the 13(f) list"
        + (f" ({', '.join(changes)})" if changes else ""),
    )


# --- cusip-coverage --------------------------------------------------------------


@app.command("cusip-coverage")
def cusip_coverage_command(
    top: Annotated[
        int,
        typer.Option(
            "--top", min=0, metavar="N", help="List the N unresolved securities of highest value."
        ),
    ] = 10,
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            min=0,
            max=100,
            metavar="PCT",
            help="Exit 1 if coverage by value is below this.",
        ),
    ] = float(DEFAULT_THRESHOLD_PCT),
) -> None:
    """Report the share of securities resolved to a ticker, by row and by value.

    By value, the aggregate holdings value of resolved securities over all of
    it, is the headline and the gate: below --threshold (95% by default) the
    aggregate rankings are quietly incomplete, and this exits 1. Lists the
    unresolved securities of highest value, with any fuzzy matches pending
    review for them: what to resolve next, with resolve-cusip.
    """
    passed = asyncio.run(_cusip_coverage(top=top, threshold=Decimal(str(threshold))))
    if not passed:
        raise typer.Exit(code=1)


async def _cusip_coverage(*, top: int, threshold: Decimal) -> bool:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "cusip-coverage", top=top, threshold=float(threshold)) as run:
        async with session_scope(settings) as session:
            coverage = await cusip_coverage(session, top=top)
        run.items_seen = coverage.securities
        run.metrics |= {
            "value_pct": float_pct(coverage.value_pct),
            "row_pct": float_pct(coverage.row_pct),
            "published_value_pct": float_pct(coverage.published_value_pct),
            "threshold_pct": float(threshold),
            "passes": coverage.passes(threshold),
            "pending_reviews": coverage.pending_reviews,
            "delisted": coverage.delisted,
            "collisions": len(coverage.collisions),
        }
    _echo_coverage(coverage, threshold=threshold, top=top)
    return coverage.passes(threshold)


def _echo_coverage(coverage: CusipCoverage, *, threshold: Decimal, top: int) -> None:
    verdict = "passes" if coverage.passes(threshold) else "BELOW"
    typer.echo(
        f"cusip-coverage  {_pct_text(coverage.value_pct)} resolved by value — "
        f"{verdict} the {float(threshold):g}% gate"
    )
    _line(
        "by value",
        f"{_held(coverage.resolved_value)} of {_held(coverage.value)} held  "
        f"{_pct_text(coverage.value_pct)}",
    )
    _line(
        "by row",
        f"{coverage.resolved:,} of {_count(coverage.securities, 'security', 'securities')}  "
        f"{_pct_text(coverage.row_pct)}",
    )
    _line(
        "published",
        f"{_held(coverage.published_resolved_value)} of {_held(coverage.published_value)} "
        f"in position_snapshot  {_pct_text(coverage.published_value_pct)}",
    )
    sources = [
        f"{source} {coverage.by_source[source]:,}"
        for source in sorted(coverage.by_source, key=lambda s: (s is None, s or ""))
        if source is not None
    ]
    sources.append(f"none {coverage.by_source.get(None, 0):,}")
    _line("sources", ", ".join(sources))
    _line(
        "delisted",
        f"{_count(coverage.delisted, 'security', 'securities')} no longer on the 13(f) list "
        f"({_held(coverage.delisted_value)} held)",
    )
    if coverage.unnamed:
        _line(
            "unnamed",
            f"{_count(coverage.unnamed, 'security', 'securities')} with no name: "
            "shown as the CUSIP alone",
        )
    _line(
        "review",
        f"{_count(coverage.pending_reviews, 'fuzzy match', 'fuzzy matches')} pending for "
        f"{_count(coverage.pending_review_securities, 'security', 'securities')}",
    )
    _echo_collisions(coverage, top=top)
    if not coverage.unresolved:
        return
    typer.echo("  unresolved, highest value first:")
    width = max(len(security.name or "") for security in coverage.unresolved)
    width = min(width, 32)
    for security in coverage.unresolved:
        named = "list" if security.source == ResolutionSource.SEC_13F_LIST.value else "filed"
        line = (
            f"    {security.cusip}  {(security.name or '')[:width]:<{width}}  "
            f"{_held(security.exposure):>10}  {named:<5}"
        )
        if security.suggestions:
            line += "  review: " + ", ".join(
                f"{s.ticker} ({s.similarity:.2f})" for s in security.suggestions
            )
        typer.echo(line)


def _echo_collisions(coverage: CusipCoverage, *, top: int) -> None:
    if not coverage.collisions:
        _line("collisions", "none: no ticker is held by two securities at once")
        return
    _line(
        "collisions",
        f"{_count(len(coverage.collisions), 'ticker')} held by two securities at once; "
        "settle with ticker-alias",
    )
    for collision in coverage.collisions[:top]:
        held = "; ".join(
            f"{s.cusip} {(s.name or '')[:24]} {_range(s.valid_from, s.valid_to)}"
            for s in collision.securities
        )
        typer.echo(f"    {collision.ticker:<8}  {held}")


def _range(start: date | None, end: date | None) -> str:
    return f"[{start or '…'}, {end or '…'})"


# --- resolve-cusip ---------------------------------------------------------------


@app.command("resolve-cusip")
def resolve_cusip_command(
    cusip: Annotated[str, typer.Argument(metavar="CUSIP", help="The nine-character CUSIP.")],
    ticker: Annotated[str, typer.Option("--ticker", help="The ticker it trades under.")],
    note: Annotated[
        str | None, typer.Option("--note", help="Why: kept on the security as resolution_note.")
    ] = None,
) -> None:
    """Resolve one CUSIP to a ticker by hand.

    Recorded as a manual resolution, which resolve-cusips never overwrites,
    --force included. A share class is written with a dot: BRK/B and brk.b
    are both stored as BRK.B. Closes the CUSIP's pending fuzzy matches,
    accepting any that suggested this ticker. Then drops every response the
    API has cached.
    """
    try:
        asyncio.run(_resolve_cusip(cusip, ticker, note=note))
    except CommandError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


#: What a ticker may be made of, once normalised: letters, digits, and the dot
#: or hyphen some share classes and units carry.
_TICKER: Final = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,15}")


async def _resolve_cusip(cusip: str, ticker: str, *, note: str | None) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    cusip = cusip.strip().upper()
    normalised = normalise_ticker(ticker)

    async with track_run(settings, "resolve-cusip", cusip=cusip, ticker=normalised) as run:
        if len(cusip) != 9 or not cusip.isalnum():
            raise CommandError(f"{cusip!r} is not a CUSIP: expected nine letters and digits")
        if not _TICKER.fullmatch(normalised):
            raise CommandError(f"{ticker!r} is not a ticker")
        run.items_seen = 1
        async with session_scope(settings) as session:
            try:
                resolved = await resolve_manually(session, cusip, normalised, note=note)
            except UnknownSecurityError as failure:
                raise CommandError(
                    f"no filing has reported {cusip}: there is no security to resolve"
                ) from failure
        run.items_written = 1
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
        logger.info(
            "resolution.manual",
            cusip=cusip,
            ticker=resolved.ticker,
            previous_ticker=resolved.previous_ticker,
            previous_source=resolved.previous_source,
        )
    _echo_manual(resolved, note=note)


def _echo_manual(resolved: ManualResolution, *, note: str | None) -> None:
    typer.echo(f"resolve-cusip  {_security(resolved.cusip, resolved.name)}")
    was = resolved.previous_ticker or "none"
    if resolved.previous_source:
        was += f", from {resolved.previous_source}"
    _line("ticker", f"{resolved.ticker}  (was {was})")
    if resolved.renamed:
        _line(
            "history",
            f"{resolved.previous_ticker}'s range ends today and {resolved.ticker}'s starts; "
            "ticker-alias sets the real date",
        )
    _line("source", "manual: resolve-cusips leaves it alone, --force included")
    if note:
        _line("note", note)
    closed = resolved.reviews_accepted + resolved.reviews_superseded
    if closed:
        _line(
            "review",
            f"{_count(closed, 'pending fuzzy match', 'pending fuzzy matches')} closed: "
            f"{resolved.reviews_accepted:,} accepted, {resolved.reviews_superseded:,} superseded",
        )


# --- ticker-alias ----------------------------------------------------------------


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as failure:
        raise typer.BadParameter(
            f"{value!r} is not a date: expected one like 2022-06-09"
        ) from failure


@app.command("ticker-alias")
def ticker_alias_command(
    cusip: Annotated[str, typer.Argument(metavar="CUSIP", help="The nine-character CUSIP.")],
    ticker: Annotated[str, typer.Option("--ticker", help="The ticker it traded under.")],
    valid_to: Annotated[
        date,
        typer.Option(
            "--to",
            metavar="YYYY-MM-DD",
            parser=_day,
            help="The first day it no longer traded under it: the rename or delisting.",
        ),
    ],
    valid_from: Annotated[
        date | None,
        typer.Option(
            "--from",
            metavar="YYYY-MM-DD",
            parser=_day,
            help="The first day it did. Leave out for as far back as we know.",
        ),
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="Why: kept on the history's row.")
    ] = None,
) -> None:
    """Record a former ticker: CUSIP traded as TICKER from --from until --to.

    For history no source we read has: OpenFIGI answers with today's ticker,
    so FB before Facebook became Meta is entered here. The ticker ranges it
    overlaps are trimmed to meet it, so entering FB up to 2022-06-09 starts
    META on 2022-06-09. A range it would swallow whole or cut in two is
    refused. The current ticker is set with resolve-cusip, not here. Then
    drops every response the API has cached.
    """
    try:
        asyncio.run(
            _ticker_alias(cusip, ticker, valid_from=valid_from, valid_to=valid_to, note=note)
        )
    except CommandError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _ticker_alias(
    cusip: str, ticker: str, *, valid_from: date | None, valid_to: date, note: str | None
) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    cusip = cusip.strip().upper()
    normalised = normalise_ticker(ticker)

    async with track_run(
        settings,
        "ticker-alias",
        cusip=cusip,
        ticker=normalised,
        valid_from=valid_from,
        valid_to=valid_to,
    ) as run:
        if len(cusip) != 9 or not cusip.isalnum():
            raise CommandError(f"{cusip!r} is not a CUSIP: expected nine letters and digits")
        if not _TICKER.fullmatch(normalised):
            raise CommandError(f"{ticker!r} is not a ticker")
        if valid_from is not None and valid_from >= valid_to:
            raise CommandError(f"--from {valid_from} is not before --to {valid_to}")
        run.items_seen = 1
        async with session_scope(settings) as session:
            try:
                entry = await add_ticker_history(
                    session,
                    cusip,
                    normalised,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    note=note,
                )
            except UnknownSecurityError as failure:
                raise CommandError(
                    f"no filing has reported {cusip}: there is no security to give a ticker"
                ) from failure
            except TickerHistoryConflictError as failure:
                raise CommandError(
                    f"{normalised} {_range(valid_from, valid_to)} does not fit {cusip}'s "
                    f"history: {failure}. Nothing was written."
                ) from failure
            await flag_delistings(session)
        run.items_written = 1
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    _echo_ticker_alias(entry, normalised, valid_from=valid_from, valid_to=valid_to, note=note)


def _echo_ticker_alias(
    entry: HistoryEntry,
    ticker: str,
    *,
    valid_from: date | None,
    valid_to: date,
    note: str | None,
) -> None:
    typer.echo(f"ticker-alias  {_security(entry.cusip, entry.name)}")
    _line("added", f"{ticker} {_range(valid_from, valid_to)}")
    for trimmed in entry.trimmed:
        _line(
            "trimmed",
            f"{trimmed.ticker} {_range(*trimmed.was)} to {_range(*trimmed.now)}",
        )
    _line("current", entry.current_ticker or "no ticker")
    if note:
        _line("note", note)


# --- fetch-quotes ----------------------------------------------------------------

#: How many tickers to name on a line that lists some, before "and N more".
_TICKERS_NAMED: Final = 8


@app.command("fetch-quotes")
def fetch_quotes_command(
    tickers: Annotated[
        list[str] | None,
        typer.Option(
            "--ticker",
            metavar="T",
            help="Only this ticker. Repeat it for more.",
        ),
    ] = None,
) -> None:
    """Fetch the latest close of every listed security from QUOTE_PROVIDER.

    Every security with a ticker that is not delisted, except a ticker two of
    them claim at once. Under QUOTE_PROVIDER=null, the default and
    production's, nothing is asked and nothing written. yfinance is for
    development: refused in production, and installed only with
    uv sync --group quotes.

    One ticker failing does not stop the rest; the provider failing does (exit
    1), keeping the closes written before it did. Then drops every response
    the API has cached, if anything was written.
    """
    try:
        asyncio.run(_fetch_quotes(tickers))
    except (QuoteProviderUnavailableError, QuoteFetchError) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _fetch_quotes(tickers: list[str] | None) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    started = time.perf_counter()
    try:
        ran = await fetch_prices(settings, tickers=tickers)
    except JobLocked as locked:
        _echo_locked(locked)
        return
    _echo_quotes(ran.value, tickers=tickers, elapsed=time.perf_counter() - started)


def _echo_quotes(report: QuoteReport, *, tickers: list[str] | None, elapsed: float) -> None:
    which = (
        _count(len(tickers), "ticker")
        if tickers
        else _count(report.listed, "listed security", "listed securities")
    )
    if report.provider == NullQuoteProvider.name:
        typer.echo(f"fetch-quotes  QUOTE_PROVIDER=null: nothing asked for {which}")
    else:
        typer.echo(
            f"fetch-quotes  {report.provider}: {report.asked:,} asked of {which}, "
            f"in {_duration(elapsed)}"
        )
        latest = f", latest {report.latest}" if report.latest else ""
        _line("written", _count(report.written, "close") + latest)
        if report.missing:
            _line(
                "no close",
                f"{len(report.missing):,}, delisted or unknown to {report.provider}: "
                + _some(report.missing),
            )
        if report.failed:
            _line("failed", f"{len(report.failed):,}: " + _some(sorted(report.failed)))
            for ticker, error in list(report.failed.items())[:_MAX_ECHOED_WARNINGS]:
                _line("", f"{ticker}: {error}")
        if report.ambiguous:
            _line(
                "ambiguous",
                f"{_count(len(report.ambiguous), 'ticker')} two listed securities claim, not "
                f"asked: {_some(report.ambiguous)}; see cusip-coverage",
            )
    if report.unknown:
        _line("unknown", f"no listed security has {_some(report.unknown)}")


def _some(tickers: list[str]) -> str:
    named = ", ".join(tickers[:_TICKERS_NAMED])
    rest = len(tickers) - _TICKERS_NAMED
    return named + (f" and {rest:,} more" if rest > 0 else "")


def _held(value: Decimal) -> str:
    """Dollars at a glance: ``$639.6bn``, ``$1.2tn``."""
    for divisor, suffix in ((Decimal("1e12"), "tn"), (Decimal("1e9"), "bn"), (Decimal("1e6"), "m")):
        if abs(value) >= divisor:
            return f"${value / divisor:,.1f}{suffix}"
    return f"${value:,.0f}"


def _pct_text(pct: Decimal | None) -> str:
    return "n/a" if pct is None else f"{pct:.2f}%"


if __name__ == "__main__":
    app()
