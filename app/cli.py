"""Typer CLI: discover-filings, ingest-filing, seed-investors, verify-investors, audit-overlaps,
backfill, recompute, refresh-views.

The operational interface. Celery's beat schedule is how this pipeline runs when
nobody is watching; this is how it runs when somebody is, and the two must not
be different code. Every verb here is the same function a task calls, wrapped in
argument parsing and a summary a person can read — so that a quarter that came
out wrong is re-run by hand, from a shell in the container, without a broker in
the loop and without anyone having to write a throwaway script at the point in
the incident where throwaway scripts are least trustworthy.

::

    uv run python -m app.cli discover-filings --filer berkshire-hathaway
    uv run python -m app.cli ingest-filing 0001067983-24-000011 --cik 1067983
    uv run python -m app.cli ingest-filing 0001067983-24-000011 --dry-run
    uv run python -m app.cli seed-investors
    uv run python -m app.cli verify-investors --csv verify-investors.csv
    uv run python -m app.cli audit-overlaps --filer pershing-square

Exit codes
----------
Zero when the filing ends up loaded, and zero when it was already loaded and
this run was asked to leave it alone — "already done" is a success, or a
backfill script resuming over a thousand filings would fail on every one it had
finished. Non-zero for everything else: a filing that could not be found,
fetched, parsed or written. That is the contract the shell loop around this
command depends on, and it is why the failure paths below all funnel through
:class:`CommandError` rather than tracebacks.

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
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, TextIO

import httpx
import structlog
import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.accession import normalise_accession
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.filer import OverlapPolicy
from app.db.models.filing import LOADED_STATUSES, Filing
from app.db.models.pending_filing import PendingStatus
from app.db.queries.overlaps import OverlapFinding, audit_overlaps
from app.db.session import create_engine, create_session_factory, session_scope
from app.ingestion.archive import archive_13f_documents
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
from app.ingestion.normalisation import NormalisedFiling, normalise_filing
from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
    parse_information_table,
    parse_primary_doc,
)
from app.ingestion.verify_investors import CikCheck, stale_cutoff, verify_investors
from app.storage.raw import RawStoreError, open_raw_store

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
    message. A dry run writes neither.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    accession = _normalise_accession(accession_no)
    structlog.contextvars.bind_contextvars(job_name="ingest-filing", accession_no=accession)

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
    _echo_report(report)


async def _fetch_and_load(
    settings: Settings, accession: str, *, cik: str, force: bool, dry_run: bool
) -> _Report:
    """Everything after the decision that there is work to do."""
    async with EdgarClient(settings) as edgar:
        submission = await find_submission(edgar, cik=cik, accession_no=accession)
        _require_thirteen_f(submission)
        documents = await fetch_13f_documents(edgar, cik=cik, accession_no=accession)

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
    cover = parse_primary_doc(documents.primary_doc)
    table = (
        parse_information_table(documents.info_table)
        if documents.info_table is not None
        else InformationTable(rows=(), warnings=())
    )
    normalised = normalise_filing(filed_at=submission.filed_at, cover=cover, table=table)

    result = (
        None
        if dry_run
        else await _load(
            settings,
            accession=accession,
            submission=submission,
            cover=cover,
            normalised=normalised,
            documents=documents,
            raw_prefix=raw_prefix,
        )
    )

    return _Report(
        accession_no=accession,
        submission=submission,
        cover=cover,
        table=table,
        normalised=normalised,
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
    submission: Submission,
    cover: PrimaryDoc,
    normalised: NormalisedFiling,
    documents: FilingDocuments,
    raw_prefix: str | None,
) -> LoadResult:
    """Write the parsed filing, in one transaction that ``session_scope`` commits.

    ``raw_key`` is the filing's archive *prefix*, not one document's key: a
    13F is several documents, and the prefix is what lists all of them. It is
    only ever ``None`` on a dry run, which never gets here.
    """
    async with session_scope(settings) as session:
        result = await load_filing(
            session,
            accession_no=accession,
            filed_at=submission.filed_at,
            primary_doc=cover,
            normalised=normalised,
            raw_key=raw_prefix,
            source_url=documents.primary_doc_url,
        )
        await mark_ingested(session, accession)
    logger.info(
        "filing.ingested",
        cik=cover.cik,
        period=cover.period_of_report.isoformat(),
        filing_id=result.filing_id,
        rows=result.holdings_loaded,
        status=normalised.parse_status.value,
    )
    return result


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
    lines = [f"{note.kind.value:<14} {note.detail}" for note in report.normalised.parse_notes]
    lines += [
        f"{'tolerated':<14} row {warning.row} {warning.field}: {warning.reason}"
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

    # Deferred holdings are counted rather than reported as the zero the loader
    # returns, because "0 positions loaded" is what a 13F-NT looks like and this
    # is the opposite: the positions exist, they are waiting on a filer. The
    # arithmetic is the loader's own — rows minus the ones it folded — so the
    # two branches print the same number for the same filing either way.
    positions = (
        len(report.table.rows) - result.rows_collapsed
        if result.holdings_deferred
        else result.holdings_loaded
    )
    parts.append(f"{positions} positions {'deferred' if result.holdings_deferred else 'loaded'}")
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
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    structlog.contextvars.bind_contextvars(job_name="discover-filings")

    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            filer_ids = await tracked_filer_ids(session, slug=slug)
        async with EdgarClient(settings) as edgar:
            results = [
                await discover_filings(sessions, edgar, filer_id, since=since)
                for filer_id in filer_ids
            ]
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

    The file is validated before a connection is opened, so a malformed list
    fails in milliseconds with the problem named, rather than after a connect
    and with a constraint violation that names a table.

    A dry run is the real run rolled back, not a separate code path: it goes
    through the same upserts and conflict check, so what it prints is what the
    real run will print.
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    structlog.contextvars.bind_contextvars(job_name="seed-investors")

    entries = load_investors(path)

    async with session_scope(settings) as session:
        result = await seed_investors(session, entries)
        if dry_run:
            await session.rollback()

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
    structlog.contextvars.bind_contextvars(job_name="audit-overlaps")

    async with session_scope(settings) as session:
        findings = await audit_overlaps(session, slug=slug)

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
    """Run the checks and print them. Returns whether any CIK failed."""
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
