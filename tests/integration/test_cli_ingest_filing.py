"""``ingest-filing``, end to end: EDGAR in, rows out.

The command is the whole pipeline behind one verb — discovery, fetch, parse,
normalise, load — and the reason to test it as a command rather than as five
units is that the seams between those steps are where the arguments get crossed.
A parser that is right about a period and a loader that is right about a period
still produce a wrong filing if the CLI hands one of them the other's.

Against a real Postgres, because the assertions worth making are about rows: one
filing after three runs, holdings that disappear when the document no longer
lists them, an exit code that a backfill loop can branch on. EDGAR is the only
thing faked — respx sits where sec.gov would, so the four requests the command
makes are scripted and the rate limiter is not waited on.

The commits here are real. ``session_scope`` owns its own engine and commits on
exit, which is the behaviour under test, so the transaction-rollback isolation
the rest of this package relies on does not apply and the ``clean_tables``
fixture truncates between tests instead.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import pytest
import respx
from httpx import Response
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket
from app.db.models import Filer, FilerCik, Filing, Holding, Security
from tests.conftest import make_settings

ACCESSION: Final = "0001067983-24-000011"
CIK: Final = "0001067983"
PERIOD: Final = date(2024, 3, 31)

APPLE: Final = "037833100"
COCA_COLA: Final = "191216100"

_SUBMISSIONS_URL: Final = f"https://data.sec.gov/submissions/CIK{CIK}.json"
_DIRECTORY: Final = "https://www.sec.gov/Archives/edgar/data/1067983/000106798324000011"

#: Post-cutover, so the multiplier is 1 and the dollar figures asserted on below
#: are the figures in the fixture documents.
_ACCEPTED: Final = "2024-05-15T20:05:04.000Z"


def _primary_doc(*, entry_total: int = 2, form_type: str = "13F-HR") -> bytes:
    """A 13F cover page, trimmed to the fields the parser keys the filing on.

    ``entry_total`` is the filer's own count of information-table rows, and the
    reason it is a parameter: setting it to something the table does not match
    is how a test provokes a ``suspect`` verdict without having to malform a
    document.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>{form_type}</submissionType>
    <filerInfo>
      <filer><credentials><cik>{CIK}</cik></credentials></filer>
      <periodOfReport>03-31-2024</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <filingManager><name>Berkshire Hathaway Inc</name></filingManager>
      <reportType>13F HOLDINGS REPORT</reportType>
    </coverPage>
    <summaryPage>
      <otherIncludedManagersCount>0</otherIncludedManagersCount>
      <tableEntryTotal>{entry_total}</tableEntryTotal>
      <tableValueTotal>3000000</tableValueTotal>
    </summaryPage>
  </formData>
</edgarSubmission>
""".encode()


def _info_table(*cusips: str) -> bytes:
    """An information table holding ``cusips``, a million dollars apiece.

    The values are round and the share counts imply a $100 price, so the guards
    in :mod:`app.ingestion.normalisation` stay quiet unless a test asks them not
    to.
    """
    rows = "".join(
        f"""
  <infoTable>
    <nameOfIssuer>ISSUER {index}</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>{cusip}</cusip>
    <value>{1_000_000 * index}</value>
    <shrsOrPrnAmt>
      <sshPrnamt>{10_000 * index}</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>{10_000 * index}</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>"""
        for index, cusip in enumerate(cusips, start=1)
    )
    return (
        '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf'
        f'/informationtable">{rows}\n</informationTable>'
    ).encode()


def _submissions(*, form: str = "13F-HR") -> dict[str, object]:
    return {
        "cik": str(int(CIK)),
        "name": "BERKSHIRE HATHAWAY INC",
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION],
                "form": [form],
                "acceptanceDateTime": [_ACCEPTED],
                "filingDate": [_ACCEPTED[:10]],
                "reportDate": ["2024-03-31"],
                "primaryDocument": ["primary_doc.xml"],
            },
            "files": [],
        },
    }


def _index(*names: str) -> dict[str, object]:
    return {"directory": {"item": [{"name": name, "type": "text.gif"} for name in names]}}


def _edgar(
    *,
    form: str = "13F-HR",
    entry_total: int = 2,
    cusips: tuple[str, ...] = (APPLE, COCA_COLA),
    table_name: str = "infotable.xml",
) -> None:
    """Script the four requests one successful ingest makes.

    Registered as respx routes rather than as a transport, because the command
    builds its own :class:`~app.ingestion.edgar.client.EdgarClient` and there is
    deliberately no seam to inject one through — the CLI owning its client is
    part of what is under test.
    """
    respx.get(_SUBMISSIONS_URL).mock(Response(200, json=_submissions(form=form)))
    respx.get(f"{_DIRECTORY}/index.json").mock(
        Response(200, json=_index("primary_doc.xml", table_name))
    )
    respx.get(f"{_DIRECTORY}/primary_doc.xml").mock(
        Response(200, content=_primary_doc(entry_total=entry_total, form_type=form))
    )
    respx.get(f"{_DIRECTORY}/{table_name}").mock(Response(200, content=_info_table(*cusips)))


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """Point the command at the test container, and unthrottle its EDGAR client.

    Two patches, for two things the command is entitled to own. It reads
    ``get_settings()`` itself, so that is where the container's DSN goes; and it
    builds its own client, whose limiter is the process-global token bucket —
    paced at eight requests a second, which would add half a second per
    invocation to prove something already asserted in tests/test_edgar_client.py.
    """
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.ingestion.edgar.client.get_edgar_limiter",
        lambda rate_per_second: AsyncTokenBucket(10_000.0),
    )
    yield settings
    # The command configures logging onto whatever sys.stderr is at the time,
    # which under CliRunner is a buffer that is about to be discarded. Left
    # alone, the next test's first log line writes to a closed stream.
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    """Empty the tables around each test, because these writes really commit.

    ``session_scope`` owns its own engine and commits on the way out — that is
    the behaviour under test — so the connection-level rollback that isolates
    the rest of this package does not reach it.
    """
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text("TRUNCATE holding, filing, security, filer_cik, filer RESTART IDENTITY CASCADE"),
    )


def _execute(engine: AsyncEngine, *statements: Executable) -> None:
    """Run committing statements on a connection of this test's own.

    ``asyncio.run`` per call, matching how the session-scoped ``migrated_engine``
    is used elsewhere in this package: it is a NullPool engine precisely so that
    nothing it hands out is bound to a loop that outlives the call.
    """

    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    """Read committed rows back, outside whatever the command wrote them in."""

    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(statement)).all()]

    return asyncio.run(run())


def _register_filer(engine: AsyncEngine) -> None:
    """Make the filing's CIK resolvable, so its holdings load rather than defer.

    ``holding.filer_id`` is ``NOT NULL``, so a filing whose CIK maps to no filer
    loads its cover page and leaves the positions for later. That is a real
    state with its own test below; every other test here wants the finished one.
    """
    _execute(
        engine,
        insert(Filer).values(id=1, name="Berkshire Hathaway Inc", slug="berkshire-hathaway"),
        insert(FilerCik).values(filer_id=1, cik=CIK),
    )


# --- the happy path ----------------------------------------------------------


@respx.mock
def test_a_filing_is_fetched_parsed_and_loaded(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 0, result.output
    filings = _fetch(
        migrated_engine,
        select(Filing.accession_no, Filing.cik, Filing.period_of_report, Filing.parse_status),
    )
    assert filings == [(ACCESSION, CIK, PERIOD, "ok")]

    holdings = _fetch(
        migrated_engine,
        select(Security.cusip, Holding.value_usd, Holding.shares)
        .join(Security, Security.id == Holding.security_id)
        .order_by(Security.cusip),
    )
    assert holdings == [
        (APPLE, Decimal("1000000.00"), Decimal(10_000)),
        (COCA_COLA, Decimal("2000000.00"), Decimal(20_000)),
    ]


@respx.mock
def test_the_summary_names_the_filer_period_rows_and_total(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The acceptance criterion for the output, and it is not decoration: this
    summary is the only thing an operator re-running a quarter by hand sees
    before deciding whether the numbers are worth publishing."""
    _edgar()
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 0, result.output
    assert "Berkshire Hathaway Inc" in result.stdout
    assert "2024-03-31" in result.stdout
    assert "2024Q1" in result.stdout
    assert "2 rows parsed" in result.stdout
    assert "2 positions loaded" in result.stdout
    assert "$3,000,000.00" in result.stdout
    # The information table's name is unpredictable, so which file the portfolio
    # came out of has to be visible rather than inferred.
    assert "infotable.xml" in result.stdout


@respx.mock
def test_the_source_url_recorded_is_the_one_actually_fetched(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Kept rather than rebuilt from the accession number later: EDGAR's archive
    path is EDGAR's convention and it has changed shape before."""
    _edgar()
    _register_filer(migrated_engine)

    runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert _fetch(migrated_engine, select(Filing.source_url)) == [
        (f"{_DIRECTORY}/primary_doc.xml",)
    ]


@respx.mock
def test_an_accession_number_without_dashes_is_accepted(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The undashed spelling is what appears in archive URLs, so it is what gets
    pasted. It has to reach ``filing.accession_no`` in the dashed form the
    database stores, or every later lookup misses a filing we have."""
    _edgar()
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION.replace("-", ""), "--cik", CIK])

    assert result.exit_code == 0, result.output
    assert _fetch(migrated_engine, select(Filing.accession_no)) == [(ACCESSION,)]


# --- running it twice --------------------------------------------------------


@respx.mock
def test_a_second_run_leaves_it_alone_and_still_succeeds(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """ "Already loaded" is a success, not a failure.

    A backfill script resuming over a thousand accession numbers re-runs every
    one it finished before it died, and a non-zero exit on those would make a
    successful resume indistinguishable from a broken one.
    """
    _edgar()
    _register_filer(migrated_engine)

    assert runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK]).exit_code == 0
    after_first = len(respx.calls)

    second = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert second.exit_code == 0
    assert "already loaded" in second.stdout
    # And it decided that without asking EDGAR, which is the difference between
    # resuming a backfill and re-running it.
    assert len(respx.calls) == after_first


@respx.mock
def test_forcing_a_reload_does_not_duplicate_the_filing_or_its_holdings(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()
    _register_filer(migrated_engine)

    for _ in range(3):
        assert (
            runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK, "--force"]).exit_code == 0
        )

    assert len(_fetch(migrated_engine, select(Filing.id))) == 1
    assert len(_fetch(migrated_engine, select(Holding.id))) == 2


@respx.mock
def test_a_forced_reload_drops_a_position_the_document_no_longer_reports(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The reason ``--force`` exists at all: a restated filing, or a parser fix.

    An upsert on the holdings' natural key would leave the sold position in the
    table forever, and the filing would report a holding it does not contain.
    """
    _edgar()
    _register_filer(migrated_engine)
    runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    respx.reset()
    _edgar(entry_total=1, cusips=(APPLE,))
    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK, "--force"])

    assert result.exit_code == 0, result.output
    assert _fetch(
        migrated_engine,
        select(Security.cusip).join(Holding, Holding.security_id == Security.id),
    ) == [(APPLE,)]


# --- --cik -------------------------------------------------------------------


@respx.mock
def test_the_cik_is_optional_once_the_filing_is_in_the_database(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Discovery writes the row before anything fetches its documents, so the
    ordinary case — finishing a filing the daily index found — already knows the
    CIK and should not make an operator look it up again."""
    _edgar()
    _register_filer(migrated_engine)
    _execute(
        migrated_engine,
        insert(Filing).values(
            accession_no=ACCESSION,
            cik=CIK,
            form_type="13F-HR",
            period_of_report=PERIOD,
            filed_at=datetime(2024, 5, 15, 20, 5, 4, tzinfo=UTC),
            value_multiplier=1,
            parse_status="pending",
        ),
    )

    result = runner.invoke(app, ["ingest-filing", ACCESSION])

    assert result.exit_code == 0, result.output
    assert _fetch(migrated_engine, select(Filing.parse_status)) == [("ok",)]


@respx.mock
def test_an_unknown_filing_with_no_cik_fails_before_touching_edgar(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """It cannot be derived from the accession number, whose leading digits are
    the transmitting agent's CIK rather than the filer's — a distinction worth a
    real error message, because the archive path built from the wrong one does
    not exist."""
    _edgar()

    result = runner.invoke(app, ["ingest-filing", ACCESSION])

    assert result.exit_code == 1
    assert "--cik" in result.stderr
    assert not respx.calls
    assert _fetch(migrated_engine, select(Filing.id)) == []


# --- --dry-run ---------------------------------------------------------------


@respx.mock
def test_a_dry_run_reports_everything_and_writes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "$3,000,000.00" in result.stdout
    assert "2 rows parsed" in result.stdout
    assert _fetch(migrated_engine, select(Filing.id)) == []
    assert _fetch(migrated_engine, select(Holding.id)) == []
    assert _fetch(migrated_engine, select(Security.id)) == []


@respx.mock
def test_a_dry_run_reports_the_skip_it_would_have_made(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """A rehearsal that does not rehearse the decision is not worth much: the
    dry run has to say "this would do nothing" when that is the answer."""
    _edgar()
    _register_filer(migrated_engine)
    runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK, "--dry-run"])

    assert result.exit_code == 0
    assert "already loaded" in result.stdout


# --- what the loader could not finish ----------------------------------------


@respx.mock
def test_an_unresolved_filer_defers_the_holdings_and_says_so(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The one outcome the summary must not report as a number.

    ``holding.filer_id`` is ``NOT NULL``, so a filing whose CIK is not yet a
    known filer loads its cover page and no positions — and "0 holdings" is
    exactly what a legitimate ``13F-NT`` prints. The two mean opposite things.
    """
    _edgar()

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 0, result.output
    assert "DEFERRED" in result.stdout
    assert "2 positions deferred" in result.stdout
    assert len(_fetch(migrated_engine, select(Filing.id))) == 1
    assert _fetch(migrated_engine, select(Holding.id)) == []


@respx.mock
def test_resolving_the_filer_and_re_running_finishes_the_job(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Which is what makes deferral a pause rather than a loss."""
    _edgar()
    runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK, "--force"])

    assert result.exit_code == 0, result.output
    assert len(_fetch(migrated_engine, select(Holding.id))) == 2


# --- filings we do not believe, and filings we cannot read -------------------


@respx.mock
def test_a_filing_that_fails_a_guard_still_loads_and_is_flagged(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Flagged, not rejected. A cover page declaring three rows over a table of
    two means something went missing, and withholding a portfolio that is
    mostly right leaves a hole shaped exactly like a manager who filed nothing.
    """
    _edgar(entry_total=3)
    _register_filer(migrated_engine)

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 0, result.output
    assert "suspect" in result.stdout
    assert "entry_count" in result.stdout
    assert _fetch(migrated_engine, select(Filing.parse_status)) == [("suspect",)]
    assert len(_fetch(migrated_engine, select(Holding.id))) == 2


@respx.mock
def test_a_form_this_command_cannot_parse_is_refused_before_it_is_fetched(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Handing a Form 4 to the 13F parser raises nothing — there are no
    ``<infoTable>`` elements in it, so it loads clean with zero holdings, which
    is a valid ``13F-NT``. The wrong form would load as a correct filing of the
    right one."""
    _edgar(form="4")

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 1
    assert "13F" in result.stderr
    assert _fetch(migrated_engine, select(Filing.id)) == []


@respx.mock
def test_a_filing_edgar_does_not_have_exits_non_zero_and_writes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    respx.get(_SUBMISSIONS_URL).mock(Response(404))

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 1
    assert _fetch(migrated_engine, select(Filing.id)) == []


@respx.mock
def test_a_directory_whose_only_candidate_is_not_an_information_table_fails(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Rather than loading the filing with an empty portfolio, which is the
    failure mode :mod:`app.ingestion.edgar.documents` exists to convert into an
    exception."""
    respx.get(_SUBMISSIONS_URL).mock(Response(200, json=_submissions()))
    respx.get(f"{_DIRECTORY}/index.json").mock(
        Response(200, json=_index("primary_doc.xml", "exhibit99.xml"))
    )
    respx.get(f"{_DIRECTORY}/primary_doc.xml").mock(Response(200, content=_primary_doc()))
    respx.get(f"{_DIRECTORY}/exhibit99.xml").mock(
        Response(200, content=b"<exhibit>see attached</exhibit>")
    )

    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", CIK])

    assert result.exit_code == 1
    assert _fetch(migrated_engine, select(Filing.id)) == []


@pytest.mark.parametrize("argument", ["not-an-accession", "12345", "0001067983-24-00001X"])
def test_a_malformed_accession_number_is_rejected(runner: CliRunner, argument: str) -> None:
    result = runner.invoke(app, ["ingest-filing", argument, "--cik", CIK])

    assert result.exit_code == 1
    assert "accession number" in result.stderr


def test_a_malformed_cik_is_rejected(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ingest-filing", ACCESSION, "--cik", "berkshire"])

    assert result.exit_code == 1
    assert "CIK" in result.stderr
