"""Typer CLI: ingest-filing, backfill, recompute, refresh-views.

The operational interface. Celery's beat schedule is how this pipeline runs when
nobody is watching; this is how it runs when somebody is, and the two must not
be different code. Every verb here is the same function a task calls, wrapped in
argument parsing and a summary a person can read — so that a quarter that came
out wrong is re-run by hand, from a shell in the container, without a broker in
the loop and without anyone having to write a throwaway script at the point in
the incident where throwaway scripts are least trustworthy.

::

    uv run python -m app.cli ingest-filing 0001067983-24-000011 --cik 1067983
    uv run python -m app.cli ingest-filing 0001067983-24-000011 --dry-run

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
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final

import httpx
import structlog
import typer
from sqlalchemy import select

from app.core.accession import normalise_accession
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.filing import Filing, ParseStatus
from app.db.session import session_scope
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
from app.ingestion.loaders import LoadResult, load_filing
from app.ingestion.normalisation import NormalisedFiling, normalise_filing
from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
    parse_information_table,
    parse_primary_doc,
)

logger = get_logger(__name__)

#: Statuses that mean the filing is in the database with its holdings, so a
#: second run has nothing to add. ``pending`` is absent on purpose — it is what
#: a row discovered by the daily index looks like before anything fetched its
#: documents, and finishing that job is the most ordinary reason to run this
#: command. ``failed`` is absent for the same reason in reverse: re-running is
#: the fix.
_ALREADY_LOADED: Final = frozenset({ParseStatus.OK.value, ParseStatus.SUSPECT.value})

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
        typer.Option("--force", help="Re-fetch and re-load a filing that is already loaded."),
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
    """
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)

    accession = _normalise_accession(accession_no)
    structlog.contextvars.bind_contextvars(job_name="ingest-filing", accession_no=accession)

    known = await _known_filing(settings, accession)
    resolved_cik = _resolve_cik(cik, known=known, accession_no=accession)

    if known is not None and known.parse_status in _ALREADY_LOADED and not force:
        _echo_skip(accession, known)
        return

    async with EdgarClient(settings) as edgar:
        submission = await find_submission(edgar, cik=resolved_cik, accession_no=accession)
        _require_thirteen_f(submission)
        documents = await fetch_13f_documents(edgar, cik=resolved_cik, accession_no=accession)

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
        )
    )

    _echo_report(
        _Report(
            accession_no=accession,
            submission=submission,
            cover=cover,
            table=table,
            normalised=normalised,
            documents=documents,
            result=result,
            dry_run=dry_run,
        )
    )


async def _load(
    settings: Settings,
    *,
    accession: str,
    submission: Submission,
    cover: PrimaryDoc,
    normalised: NormalisedFiling,
    documents: FilingDocuments,
) -> LoadResult:
    """Write the parsed filing, in one transaction that ``session_scope`` commits.

    ``raw_key`` is deliberately not passed. Nothing archives the bytes to object
    storage yet, and :func:`~app.ingestion.loaders.load_filing` coalesces a
    ``None`` against whatever is already on the row — so a re-ingest from here
    cannot blank a key that a later archiving step has written.
    """
    async with session_scope(settings) as session:
        result = await load_filing(
            session,
            accession_no=accession,
            filed_at=submission.filed_at,
            primary_doc=cover,
            normalised=normalised,
            source_url=documents.primary_doc_url,
        )
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


async def _known_filing(settings: Settings, accession_no: str) -> _KnownFiling | None:
    """Look the accession number up in our own database.

    Two answers come out of this one query and both are load-bearing. The CIK is
    what makes ``--cik`` optional — a filing discovered through the daily index
    already has one, and re-running it should not require the operator to go and
    find it again. The status is what makes the command idempotent without
    re-fetching: "already loaded" is decided here, before a single EDGAR
    request, which is the difference between resuming a backfill and re-running
    it.
    """
    async with session_scope(settings) as session:
        row = (
            await session.execute(
                select(Filing.cik, Filing.parse_status).where(Filing.accession_no == accession_no)
            )
        ).first()
    return None if row is None else _KnownFiling(cik=row.cik, parse_status=row.parse_status)


def _resolve_cik(given: str | None, *, known: _KnownFiling | None, accession_no: str) -> str:
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


if __name__ == "__main__":
    app()
