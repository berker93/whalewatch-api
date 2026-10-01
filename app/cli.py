"""Typer CLI: discover-filings, ingest-filing, seed-investors, verify-investors, audit-overlaps,
audit-amendments, check-data, recompute, backfill, runs, refresh-views.

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
    uv run python -m app.cli runs --job backfill_13f

Every run is recorded
---------------------
Every verb that touches the database runs its body inside
:func:`~app.jobs.tracking.track_run`, which writes an ``ingestion_run`` row as
it starts and its outcome as it ends, and binds the row's id as ``run_id`` on
every log line in between. ``runs`` lists them. The exceptions are ``runs``
itself, which reads the record, and ``verify-investors``, which has no database
to write it to. A test fails if a new verb is neither tracked nor one of those.

Exit codes
----------
Zero when the filing ends up loaded, and zero when it was already loaded and
this run was asked to leave it alone — "already done" is a success, or a
backfill script resuming over a thousand filings would fail on every one it had
finished. Non-zero for everything else: a filing that could not be found,
fetched, parsed or written. That is the contract the shell loop around this
command depends on, and it is why the failure paths below all funnel through
:class:`CommandError` rather than tracebacks.

``check-data`` is the exception, because finding something is its job: 1 means
it ran and found something to look at, and 2 means it could not run as asked.

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
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Annotated, Final, TextIO

import httpx
import structlog
import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.accession import normalise_accession
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.filer import OverlapPolicy
from app.db.models.filing import LOADED_STATUSES, Filing, ParseStatus
from app.db.models.pending_filing import PendingStatus
from app.db.queries.amendments import PeriodFiling, PeriodResolution, audit_amendments
from app.db.queries.checks import DataCheckReport, check_data
from app.db.queries.overlaps import OverlapFinding, audit_overlaps
from app.db.queries.runs import RunSummary, job_names, recent_runs
from app.db.session import create_engine, create_session_factory, session_scope
from app.derived.position_change import ChangeRebuild, recompute_position_change
from app.derived.position_snapshot import SnapshotRebuild, recompute_position_snapshot
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
    QueuedFiling,
    UnknownFilerError,
    default_since,
    discover_filings,
    mark_ingested,
    queued_filing,
    record_failure,
    tracked_filer_ids,
)
from app.ingestion.edgar.client import EdgarClient, EdgarRateLimited, EdgarServerError
from app.ingestion.edgar.documents import (
    FilingDocuments,
    FilingDocumentsError,
    fetch_13f_documents,
)
from app.ingestion.edgar.submissions import (
    Submission,
    SubmissionMalformedError,
    SubmissionNotFoundError,
    find_submission,
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
from app.ingestion.loaders import LoadResult, load_filing
from app.ingestion.normalisation import NormalisedFiling, NoteKind, normalise_filing
from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
    parse_information_table,
    parse_primary_doc,
)
from app.ingestion.verify_investors import CikCheck, stale_cutoff, verify_investors
from app.jobs.tracking import track_run
from app.storage.raw import RawStore, RawStoreError, open_raw_store

logger = get_logger(__name__)

#: The form types this command knows how to parse. Checked against EDGAR's own
#: word for what the submission is, before anything is fetched from the filing
#: directory, because the 13F parser applied to a Form 4 does not fail — it
#: finds no ``<infoTable>`` elements and yields an empty portfolio.
_THIRTEEN_F_FORMS: Final = ("13F-HR", "13F-NT")

#: Width of the label column in the summary. Wide enough for the longest label
#: below, which keeps the values in one column that the eye can run down.
_LABEL_WIDTH: Final = 12

#: How many warnings to print before summarising the rest. A filing whose units
#: are wrong has one note per position, and three thousand lines of them is not
#: a summary. ``filing.parse_notes`` has all of them.
_MAX_ECHOED_WARNINGS: Final = 10

#: Wide enough for every guard's name, so the details line up in one column.
_NOTE_KIND_WIDTH: Final = max(len(kind) for kind in NoteKind)


class CommandError(Exception):
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
) -> None:
    """Fetch, parse and load one 13F filing.

    Idempotent: running it twice leaves the database exactly as running it once
    did. Without ``--force`` a filing that is already loaded is left alone and
    reported as such.
    """
    try:
        asyncio.run(_ingest_filing(accession_no, cik=cik, force=force, dry_run=dry_run))
    except (
        CommandError,
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


async def _ingest_filing(accession_no: str, *, cik: str | None, force: bool, dry_run: bool) -> None:
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
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(
        settings, "ingest-filing", accession_no=accession_no, cik=cik, force=force, dry_run=dry_run
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
        _echo_report(report)


async def _fetch_and_load(
    settings: Settings, accession: str, *, cik: str, force: bool, dry_run: bool
) -> _Report:
    """Everything after the decision that there is work to do."""
    async with EdgarClient(settings) as edgar:
        submission, documents = await _fetch(edgar, accession, cik=cik)

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
    parsed = _parse(documents, filed_at=submission.filed_at)

    result = (
        None
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
    parsed: _Parsed,
    raw_prefix: str | None,
    source_url: str,
) -> LoadResult:
    """Write the parsed filing, in one transaction that ``session_scope`` commits."""
    async with session_scope(settings) as session:
        result = await _write(
            session,
            accession=accession,
            filed_at=filed_at,
            parsed=parsed,
            raw_prefix=raw_prefix,
            source_url=source_url,
        )
    _log_ingested(parsed, result)
    return result


# --- the steps ingest-filing and backfill share ------------------------------


@dataclass(frozen=True, slots=True)
class _Parsed:
    """Both documents parsed, and the verdict of the guards on them."""

    cover: PrimaryDoc
    table: InformationTable
    normalised: NormalisedFiling


async def _fetch(
    edgar: EdgarClient, accession: str, *, cik: str
) -> tuple[Submission, FilingDocuments]:
    """EDGAR's account of the submission, then its documents — unless it is not a 13F."""
    submission = await find_submission(edgar, cik=cik, accession_no=accession)
    _require_thirteen_f(submission)
    return submission, await fetch_13f_documents(edgar, cik=cik, accession_no=accession)


def _parse(documents: FilingDocuments, *, filed_at: datetime) -> _Parsed:
    """Parse and normalise. Pure: the same bytes and ``filed_at`` give the same rows."""
    cover = parse_primary_doc(documents.primary_doc)
    table = (
        parse_information_table(documents.info_table)
        if documents.info_table is not None
        else InformationTable(rows=(), warnings=())
    )
    normalised = normalise_filing(filed_at=filed_at, cover=cover, table=table)
    return _Parsed(cover=cover, table=table, normalised=normalised)


async def _write(
    session: AsyncSession,
    *,
    accession: str,
    filed_at: datetime,
    parsed: _Parsed,
    raw_prefix: str | None,
    source_url: str,
) -> LoadResult:
    """Load the filing and mark its queue row ``done``, in the caller's transaction.

    ``raw_key`` is the filing's archive *prefix*, not one document's key: a
    13F is several documents, and the prefix is what lists all of them. It is
    only ever ``None`` on a dry run, which never gets here.
    """
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
    return result


def _log_ingested(parsed: _Parsed, result: LoadResult) -> None:
    """After the commit, so the line never reports a load that rolled back."""
    logger.info(
        "filing.ingested",
        cik=parsed.cover.cik,
        period=parsed.cover.period_of_report.isoformat(),
        filing_id=result.filing_id,
        rows=result.holdings_loaded,
        status=parsed.normalised.parse_status.value,
    )


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


def _require_thirteen_f(submission: Submission) -> None:
    """Refuse anything this command cannot parse, before fetching its documents.

    A guard rather than a filter because of how the alternative fails. Handing a
    Form 4 to the 13F parser raises nothing: there are no ``<infoTable>``
    elements in it, so the information table comes back empty, the guards have
    no declared totals to check it against, and the filing loads clean with zero
    holdings — which is a valid ``13F-NT``. The wrong form loads as a correct
    filing of the right one.
    """
    form = submission.form_type.upper()
    if not form.startswith(_THIRTEEN_F_FORMS):
        raise CommandError(
            f"{submission.accession_no} is a {submission.form_type or 'unknown'} filing; "
            f"ingest-filing reads {' and '.join(_THIRTEEN_F_FORMS)} (and their /A amendments)"
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

    positions = _positions(report.table, result)
    parts.append(f"{positions} positions {'deferred' if result.holdings_deferred else 'loaded'}")
    if result.rows_collapsed:
        parts.append(f"{result.rows_collapsed} folded into another line")
    return ", ".join(parts)


def _positions(table: InformationTable, result: LoadResult) -> int:
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
    cutoff = None if full_history else since.date() if since else default_since(date.today())
    try:
        failed = asyncio.run(_discover_filings(filer, since=cutoff))
    except (UnknownFilerError, EdgarRateLimited) as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure
    if failed:
        raise typer.Exit(code=1)


async def _discover_filings(slug: str | None, *, since: date | None) -> bool:
    """Discover filer by filer, each committed on its own. Returns whether any CIK failed.

    One engine for the run rather than a ``session_scope`` per filer: each of
    those builds and disposes an engine, and this opens two sessions per filer
    across a hundred filers.

    Counted filer by filer, so a run that EDGAR blocks partway records what the
    filers before the block found and queued, which are committed.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "discover-filings", filer=slug, since=since) as run:
        engine = create_engine(settings)
        sessions = create_session_factory(engine)
        results: list[FilerDiscovery] = []
        try:
            async with sessions() as session:
                filer_ids = await tracked_filer_ids(session, slug=slug)
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

    _echo_discovery(results, since=since)
    return any(result.failures for result in results)


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


# --- backfill ----------------------------------------------------------------

#: The most filings in flight at once. Bounded by the database rather than by
#: EDGAR: every worker writes through the run's one engine, whose pool is five
#: connections plus ten overflow, and a worker past that waits out
#: ``pool_timeout`` and fails its filing. EDGAR stopped rewarding concurrency
#: well before this, since every worker draws on the one rate limiter.
_MAX_CONCURRENCY: Final = 15

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
            max=_MAX_CONCURRENCY,
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
) -> None:
    """Ingest every queued 13F, several at a time. Re-run it to resume.

    Works through pending_filing, which discover-filings fills. A filing
    already loaded is skipped without an EDGAR request; with --force it is
    re-parsed from the raw store instead, also without one. A filing that fails
    is reported and recorded on its queue row, and the run carries on.

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
    slug: str | None, *, since: date | None, concurrency: int, limit: int | None, force: bool
) -> int:
    """Plan from the database, work through the plan, summarise. Returns the exit code.

    One engine, one raw store and one EDGAR client for the whole run, shared by
    every worker. The client is opened even for a run that will only reprocess,
    because opening it sends nothing; the plan is what keeps such a run off
    the network, by giving the workers nothing to fetch.

    The run is ``partial`` whenever this exits non-zero without raising: a
    filing failed, EDGAR blocked the run, or Ctrl-C stopped it short. Each
    failed filing is a line of ``ingestion_run.error``, as on its queue row.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    started = time.monotonic()

    async with track_run(
        settings,
        "backfill_13f",
        filer=slug,
        since=since,
        concurrency=concurrency,
        limit=limit,
        force=force,
    ) as run:
        engine = create_engine(settings)
        sessions = create_session_factory(engine)
        backfilled = BackfillRun(results=(), rate_limited=None)
        try:
            async with sessions.begin() as session:
                plan = await plan_backfill(
                    session, slug=slug, since=since, force=force, limit=limit
                )
            _echo_plan(plan, slug=slug, since=since, concurrency=concurrency)
            run.items_seen = len(plan.work)

            if plan.work:
                async with open_raw_store(settings) as store, EdgarClient(settings) as edgar:
                    shared = _Shared(sessions=sessions, store=store, edgar=edgar)
                    with stop_on_interrupt(notify=_echo_stopping) as stop:
                        backfilled = await run_backfill(
                            plan.work,
                            process=partial(_backfill_filing, shared),
                            on_failure=partial(record_attempt, sessions),
                            on_result=_progress(len(plan.work)),
                            concurrency=concurrency,
                            stop=stop,
                        )
        finally:
            await engine.dispose()

        run.items_written = backfilled.count(Outcome.OK) + backfilled.count(Outcome.SUSPECT)
        run.errors.extend(
            f"{result.filing.accession_no}: {result.error}"
            for result in backfilled.results
            if result.outcome is Outcome.FAILED
        )
        not_started = backfilled.count(Outcome.NOT_STARTED)
        if not_started:
            run.errors.append(f"stopped with {not_started} not started; re-run to resume")

    _echo_backfill_summary(plan, backfilled, elapsed=time.monotonic() - started)
    if backfilled.rate_limited is not None or backfilled.count(Outcome.FAILED):
        return 1
    return _INTERRUPTED if backfilled.count(Outcome.NOT_STARTED) else 0


@dataclass(frozen=True, slots=True)
class _Shared:
    """What every worker in a run shares."""

    sessions: async_sessionmaker[AsyncSession]
    store: RawStore
    edgar: EdgarClient


async def _backfill_filing(shared: _Shared, filing: PlannedFiling) -> FilingResult:
    """One filing, archived before it is parsed and parsed before it is loaded.

    A filing the plan has as loaded is in the work only because of ``--force``,
    and is reprocessed: its archived documents, parsed again with the
    ``filed_at`` its row already holds. That branch makes no EDGAR request. The
    other is ``ingest-filing``'s, except that it never overwrites an archived
    document — backfill does not replace what it has with what EDGAR serves
    today, and ``ingest-filing --force`` is there for the filing that should.
    """
    accession = filing.accession_no
    if filing.loaded is None:
        submission, documents = await _fetch(shared.edgar, accession, cik=filing.cik)
        raw_prefix = await archive_13f_documents(
            shared.store, documents, cik=filing.cik, accession_no=accession
        )
        filed_at = submission.filed_at
    else:
        if filing.loaded.raw_key is None:
            raise CommandError(
                f"{accession} was loaded before its documents were archived, so there is "
                f"nothing to reprocess: ingest-filing {accession} --force fetches them again"
            )
        raw_prefix = filing.loaded.raw_key
        documents = await read_13f_documents(
            shared.store, raw_prefix, directory_url=_directory_url(filing)
        )
        filed_at = filing.loaded.filed_at

    parsed = _parse(documents, filed_at=filed_at)
    async with shared.sessions.begin() as session:
        result = await _write(
            session,
            accession=accession,
            filed_at=filed_at,
            parsed=parsed,
            raw_prefix=raw_prefix,
            source_url=documents.primary_doc_url,
        )
    _log_ingested(parsed, result)

    return FilingResult(
        filing=filing,
        outcome=(
            Outcome.SUSPECT if parsed.normalised.parse_status is ParseStatus.SUSPECT else Outcome.OK
        ),
        period=parsed.cover.period_of_report,
        rows=_positions(parsed.table, result),
        deferred=result.holdings_deferred,
    )


def _directory_url(filing: PlannedFiling) -> str:
    """The EDGAR directory an archived filing was fetched from.

    Taken from the URL recorded when it was, rather than rebuilt from today's
    archive convention, which is the reason ``filing.source_url`` is kept.
    """
    if filing.loaded is not None and filing.loaded.source_url is not None:
        return filing.loaded.source_url.rsplit("/", 1)[0]
    return EdgarClient.filing_index_url(filing.cik, filing.accession_no).rsplit("/", 1)[0]


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


def _progress(total: int) -> Callable[[FilingResult], None]:
    """A printer for ``[347/2103] berkshire-hathaway 2022Q3 · 41 rows · ok``.

    Numbered in the order filings finish, which with several in flight is not
    quite the order they started, so the count always reads as how far along
    the run is.
    """
    finished = 0

    def echo(result: FilingResult) -> None:
        nonlocal finished
        finished += 1
        typer.echo(f"[{finished}/{total}] {_describe(result)}")

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


# --- seed-investors ----------------------------------------------------------


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
    portfolio, positions whose share count grew more than 10,000% in a quarter,
    and quarters with no 13F between a filer's first and last. Most of these
    fire legitimately — a big enough stock split is a 10,000% jump until you
    look at the price — and each is there to be looked at. Writes nothing.
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


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"


# --- recompute ---------------------------------------------------------------


@app.command("recompute")
def recompute_command(
    filer: Annotated[
        str | None,
        typer.Option("--filer", metavar="SLUG", help="Rebuild one filer's rows instead of all."),
    ] = None,
    include_suspect: Annotated[
        bool,
        typer.Option(
            "--include-suspect",
            help="Publish the periods a suspect filing counts toward, every row marked "
            "suspect, instead of withholding them.",
        ),
    ] = False,
) -> None:
    """Rebuild position_snapshot, the published portfolio, and position_change, what changed in it.

    position_snapshot has one row per security per filer and period, summed over
    the filings that count once amendments and overlapping CIKs are resolved,
    with its weight as a percentage of the period. Common stock only: option
    lines and principal amounts are left out. position_change classifies each
    of those rows against the filer's previous period as new, add, trim or hold,
    with the deltas. A period that a suspect filing counts toward is withheld
    from both unless --include-suspect, which publishes it with every row
    marked suspect. Both tables are rebuilt in one transaction. Run check-data
    first. Exits 1 if --filer names no filer.
    """
    try:
        asyncio.run(_recompute(filer, include_suspect=include_suspect))
    except UnknownFilerError as failure:
        typer.secho(f"error: {failure}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from failure


async def _recompute(slug: str | None, *, include_suspect: bool) -> None:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    async with track_run(settings, "recompute", filer=slug, include_suspect=include_suspect) as run:
        async with session_scope(settings) as session:
            filer_id = await _filer_id(session, slug)
            rebuild = await recompute_position_snapshot(
                session, filer_id=filer_id, include_suspect=include_suspect
            )
            # From the snapshot rows just written, in the same transaction: a
            # reader sees both tables rebuilt, or neither.
            changes = await recompute_position_change(session, filer_id=filer_id)
        # Periods, not positions, so that the gap between the two is the
        # periods withheld for a suspect filing.
        run.items_seen = rebuild.periods + rebuild.withheld
        run.items_written = rebuild.periods

        logger.info(
            "position_snapshot.recomputed",
            filer=slug,
            positions=rebuild.positions,
            periods=rebuild.periods,
            suspect_periods=rebuild.suspect_periods,
            include_suspect=include_suspect,
        )
        logger.info(
            "position_change.recomputed",
            filer=slug,
            new=changes.new,
            add=changes.add,
            trim=changes.trim,
            hold=changes.hold,
        )
    _echo_rebuild(rebuild, changes, scope=slug or "every filer")


def _echo_rebuild(rebuild: SnapshotRebuild, changes: ChangeRebuild, *, scope: str) -> None:
    """What was published and what changed in it, then what was not, or was without a check."""
    typer.echo(
        f"recompute  position_snapshot for {scope}: {_count(rebuild.positions, 'position')} "
        f"in {_count(rebuild.periods, 'period')} of {_count(rebuild.filers, 'filer')}"
    )
    # In the stored vocabulary, which is what a WHERE on the table will spell.
    _line(
        "changes",
        f"position_change: {changes.new:,} new, {changes.add:,} add, "
        f"{changes.trim:,} trim, {changes.hold:,} hold",
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


if __name__ == "__main__":
    app()
