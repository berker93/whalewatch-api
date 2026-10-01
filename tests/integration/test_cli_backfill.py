"""``backfill``: the queue drained concurrently, and every way a run can be run again.

Against a real Postgres, because what matters about a backfill is what the
second run does with what the first one left: which filings it skips, how many
EDGAR requests it makes to find that out (none), and what the queue rows say
afterwards. EDGAR is faked with respx, as in ``test_cli_ingest_filing``, which
also means an unexpected request fails the filing that made it — so "no
request" is asserted both by counting calls and by the filing succeeding.

The commits are real, so tables are truncated between tests rather than rolled
back.
"""

import asyncio
import logging
import signal
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import pytest
import respx
from httpx import Request, Response
from sqlalchemy import Executable, insert, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket
from app.db.models import (
    Filer,
    FilerCik,
    Filing,
    Holding,
    IngestionRun,
    PendingFiling,
    PositionChange,
    PositionSnapshot,
    Security,
)
from app.db.models.base import Base
from app.storage.raw import LocalRawStore
from tests.conftest import make_settings

BERKSHIRE: Final = "0001067983"
PERSHING: Final = "0001336528"

APPLE: Final = "037833100"
COCA_COLA: Final = "191216100"


@dataclass(frozen=True)
class _Spec:
    """One filing as EDGAR would serve it."""

    accession_no: str
    cik: str
    period: date
    accepted: str

    @property
    def directory(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
            f"{self.accession_no.replace('-', '')}"
        )

    @property
    def prefix(self) -> str:
        return f"raw/13f/{self.cik}/{self.accession_no}/"


Q1: Final = _Spec("0001067983-24-000011", BERKSHIRE, date(2024, 3, 31), "2024-05-15T20:05:04.000Z")
Q2: Final = _Spec("0001067983-24-000022", BERKSHIRE, date(2024, 6, 30), "2024-08-14T20:05:04.000Z")
PS: Final = _Spec("0001336528-24-000033", PERSHING, date(2024, 6, 30), "2024-08-14T16:30:00.000Z")


def _primary_doc(spec: _Spec, *, entry_total: int = 2) -> bytes:
    period = spec.period
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>13F-HR</submissionType>
    <filerInfo>
      <filer><credentials><cik>{spec.cik}</cik></credentials></filer>
      <periodOfReport>{period.month:02d}-{period.day:02d}-{period.year}</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <filingManager><name>FILER {spec.cik}</name></filingManager>
      <reportType>13F HOLDINGS REPORT</reportType>
    </coverPage>
    <summaryPage>
      <otherIncludedManagersCount>0</otherIncludedManagersCount>
      <tableEntryTotal>{entry_total}</tableEntryTotal>
      <tableValueTotal>{sum(range(1, entry_total + 1)) * 1_000_000}</tableValueTotal>
    </summaryPage>
  </formData>
</edgarSubmission>
""".encode()


def _info_table(*cusips: str) -> bytes:
    """``cusips`` at a million dollars apiece times their position, at $100 a share."""
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


def _listing() -> Response:
    """A filing directory's ``index.json``: the cover page and the information table."""
    return Response(
        200,
        json={
            "directory": {
                "item": [
                    {"name": "primary_doc.xml", "type": "text.gif"},
                    {"name": "infotable.xml", "type": "text.gif"},
                ]
            }
        },
    )


def _edgar(*specs: _Spec, primary_doc: dict[str, bytes] | None = None) -> None:
    """Script every request ingesting ``specs`` makes: one index per CIK, three
    documents per filing. ``primary_doc`` replaces a filing's cover page."""
    for cik in {spec.cik for spec in specs}:
        mine = [spec for spec in specs if spec.cik == cik]
        respx.get(f"https://data.sec.gov/submissions/CIK{cik}.json").mock(
            Response(
                200,
                json={
                    "cik": str(int(cik)),
                    "name": f"FILER {cik}",
                    "filings": {
                        "recent": {
                            "accessionNumber": [spec.accession_no for spec in mine],
                            "form": ["13F-HR" for _ in mine],
                            "acceptanceDateTime": [spec.accepted for spec in mine],
                            "filingDate": [spec.accepted[:10] for spec in mine],
                            "reportDate": [spec.period.isoformat() for spec in mine],
                            "primaryDocument": ["primary_doc.xml" for _ in mine],
                        },
                        "files": [],
                    },
                },
            )
        )
    for spec in specs:
        respx.get(f"{spec.directory}/index.json").mock(_listing())
        cover = (primary_doc or {}).get(spec.accession_no, _primary_doc(spec))
        respx.get(f"{spec.directory}/primary_doc.xml").mock(Response(200, content=cover))
        respx.get(f"{spec.directory}/infotable.xml").mock(
            Response(200, content=_info_table(APPLE, COCA_COLA))
        )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """The container's DSN, and a limiter that does not pace the fake EDGAR."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.ingestion.edgar.client.get_edgar_limiter",
        lambda rate_per_second: AsyncTokenBucket(10_000.0),
    )
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    _execute(
        migrated_engine,
        insert(Filer).values(
            [
                {"id": 1, "name": "Berkshire Hathaway Inc", "slug": "berkshire-hathaway"},
                {"id": 2, "name": "Pershing Square", "slug": "pershing-square"},
            ]
        ),
        insert(FilerCik).values(
            [{"filer_id": 1, "cik": BERKSHIRE}, {"filer_id": 2, "cik": PERSHING}]
        ),
    )
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE ingestion_run, pending_filing, holding, filing, security, filer_cik, filer "
            "RESTART IDENTITY CASCADE"
        ),
    )


def _execute(engine: AsyncEngine, *statements: Executable) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(statement)).all()]

    return asyncio.run(run())


def _enqueue(engine: AsyncEngine, *specs: _Spec) -> None:
    """Queue the filings as ``discover-filings`` would."""
    _execute(
        engine,
        insert(PendingFiling).values(
            [
                {
                    "accession_no": spec.accession_no,
                    "cik": spec.cik,
                    "form_type": "13F-HR",
                    "filing_date": date.fromisoformat(spec.accepted[:10]),
                    "report_date": spec.period,
                }
                for spec in specs
            ]
        ),
    )


def _recorded(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    """The run's row: status, the two counters, and the error."""
    return _fetch(
        engine,
        select(
            IngestionRun.status,
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.error,
        ),
    )


def _loaded(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(Filing.accession_no, Filing.parse_status).order_by(Filing.accession_no),
    )


def _queue(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(PendingFiling.accession_no, PendingFiling.status, PendingFiling.attempts).order_by(
            PendingFiling.accession_no
        ),
    )


# --- draining the queue ------------------------------------------------------


@respx.mock
def test_every_queued_filing_is_loaded_and_its_queue_row_marked_done(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(Q1, Q2, PS)
    _enqueue(migrated_engine, Q1, Q2, PS)

    result = runner.invoke(app, ["backfill", "--concurrency", "3"])

    assert result.exit_code == 0, result.output
    assert _loaded(migrated_engine) == [
        (Q1.accession_no, "ok"),
        (Q2.accession_no, "ok"),
        (PS.accession_no, "ok"),
    ]
    assert _queue(migrated_engine) == [
        (Q1.accession_no, "done", 0),
        (Q2.accession_no, "done", 0),
        (PS.accession_no, "done", 0),
    ]
    assert len(_fetch(migrated_engine, select(Holding.id))) == 6


def _published(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    """Both derived tables, every column but ``computed_at``, in key order."""
    published = {}
    models: tuple[type[Base], ...] = (PositionSnapshot, PositionChange)
    for model in models:
        mapper = inspect(model)
        published[model.__tablename__] = _fetch(
            engine,
            select(*(column for column in mapper.columns if column.name != "computed_at")).order_by(
                *mapper.primary_key
            ),
        )
    return published


@respx.mock
def test_each_filing_is_published_as_it_loads_as_one_rebuild_of_everything_would(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Three at once, two of them one filer's consecutive quarters, finishing in
    whatever order they finish. Each load publishes its own period and rebuilds
    the changes of the period after, one load at a time, so Q2's changes are
    against Q1 whichever landed first."""
    _edgar(Q1, Q2, PS)
    _enqueue(migrated_engine, Q1, Q2, PS)

    result = runner.invoke(app, ["backfill", "--concurrency", "3"])
    published = _published(migrated_engine)
    rebuilt = runner.invoke(app, ["recompute", "--all"])

    assert result.exit_code == 0, result.output
    assert rebuilt.exit_code == 0, rebuilt.output
    assert _published(migrated_engine) == published
    assert {row[1] for row in published["position_change"]} == {Q1.period, Q2.period}


@respx.mock
def test_the_progress_lines_and_summary(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    """One at a time, so the lines come out in a known order."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    result = runner.invoke(app, ["backfill", "--concurrency", "1"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "backfill  every filer: 2 filings, 2 to ingest · 1 at a time"
    assert lines[1] == "[1/2] berkshire-hathaway 2024Q1 · 2 rows · ok"
    assert lines[2] == "[2/2] berkshire-hathaway 2024Q2 · 2 rows · ok"
    assert lines[3].startswith(
        "backfill  done: succeeded 2 · skipped 0 · failed 0 · suspect 0 · elapsed "
    )


@respx.mock
def test_documents_are_archived_before_they_are_loaded(
    runner: CliRunner, migrated_engine: AsyncEngine, cli_settings: Settings
) -> None:
    _edgar(Q1)
    _enqueue(migrated_engine, Q1)

    assert runner.invoke(app, ["backfill"]).exit_code == 0

    assert _fetch(migrated_engine, select(Filing.raw_key)) == [(Q1.prefix,)]
    assert _archived(cli_settings, Q1)[f"{Q1.prefix}primary_doc.xml"] == _primary_doc(Q1)


# --- running it again --------------------------------------------------------


@respx.mock
def test_a_second_run_skips_everything_without_an_edgar_request(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)
    assert runner.invoke(app, ["backfill"]).exit_code == 0
    requests = len(respx.calls)

    second = runner.invoke(app, ["backfill"])

    assert second.exit_code == 0, second.output
    assert len(respx.calls) == requests
    assert "2 filings, 2 already loaded" in second.stdout
    assert "succeeded 0 · skipped 2 · failed 0 · suspect 0" in second.stdout


@respx.mock
def test_a_run_that_stopped_partway_resumes_where_it_left_off(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """``--limit`` stands in for the crash: the first run does the oldest
    filing and no more, and the second does only what is left."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    first = runner.invoke(app, ["backfill", "--limit", "1"])

    assert first.exit_code == 0, first.output
    assert "1 to ingest, 1 held back by --limit" in first.stdout
    assert _loaded(migrated_engine) == [(Q1.accession_no, "ok")]

    second = runner.invoke(app, ["backfill", "--limit", "1"])

    assert second.exit_code == 0, second.output
    # The loaded filing is skipped and does not count against the limit.
    assert "1 already loaded, 1 to ingest" in second.stdout
    assert _loaded(migrated_engine) == [(Q1.accession_no, "ok"), (Q2.accession_no, "ok")]
    assert respx.get(f"{Q1.directory}/index.json").call_count == 1


@respx.mock
def test_a_queue_row_left_behind_by_a_load_elsewhere_is_marked_done(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """A filing loaded without going through the queue is finished work, and
    the queue should stop listing it as outstanding."""
    _edgar(Q1)
    _enqueue(migrated_engine, Q1)
    assert runner.invoke(app, ["backfill"]).exit_code == 0
    _execute(migrated_engine, text("UPDATE pending_filing SET status = 'pending'"))
    requests = len(respx.calls)

    result = runner.invoke(app, ["backfill"])

    assert result.exit_code == 0, result.output
    assert _queue(migrated_engine) == [(Q1.accession_no, "done", 0)]
    assert len(respx.calls) == requests


@respx.mock
def test_ctrl_c_finishes_the_filing_in_flight_and_starts_no_more(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """A real SIGINT, raised while the first filing is mid-request. That
    filing loads; the second is never started, and is still queued for the
    next run."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    def interrupt(request: Request) -> Response:
        signal.raise_signal(signal.SIGINT)
        return _listing()

    respx.get(f"{Q1.directory}/index.json").side_effect = interrupt

    result = runner.invoke(app, ["backfill", "--concurrency", "1"])

    assert result.exit_code == 130, result.output
    assert "stopping" in result.stderr
    assert _loaded(migrated_engine) == [(Q1.accession_no, "ok")]
    assert _queue(migrated_engine) == [
        (Q1.accession_no, "done", 0),
        (Q2.accession_no, "pending", 0),
    ]
    assert "not started 1" in result.stdout
    assert "Re-run to resume" in result.stdout


# --- one filing failing ------------------------------------------------------


@respx.mock
def test_a_failing_filing_is_recorded_and_the_rest_still_load(
    runner: CliRunner, migrated_engine: AsyncEngine, cli_settings: Settings
) -> None:
    truncated = _primary_doc(Q1)[:200]
    _edgar(Q1, Q2, primary_doc={Q1.accession_no: truncated})
    _enqueue(migrated_engine, Q1, Q2)

    result = runner.invoke(app, ["backfill", "--concurrency", "1"])

    assert result.exit_code == 1
    assert _loaded(migrated_engine) == [(Q2.accession_no, "ok")]
    [(status, attempts, last_error)] = _fetch(
        migrated_engine,
        select(PendingFiling.status, PendingFiling.attempts, PendingFiling.last_error).where(
            PendingFiling.accession_no == Q1.accession_no
        ),
    )
    assert (status, attempts) == ("failed", 1)
    assert last_error
    # Archived all the same: the fix for a parser that rejects it is a reparse.
    assert _archived(cli_settings, Q1)[f"{Q1.prefix}primary_doc.xml"] == truncated

    assert f"berkshire-hathaway 2024Q1 · FAILED · {Q1.accession_no} · " in result.stdout
    assert "succeeded 1 · skipped 0 · failed 1 · suspect 0" in result.stdout
    # Listed again at the end, where a long run's progress has scrolled away.
    [summary_line] = [line for line in result.stdout.splitlines() if line.startswith("  failed")]
    assert Q1.accession_no in summary_line


@respx.mock
def test_a_suspect_filing_loads_and_is_counted_as_suspect(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(Q1, primary_doc={Q1.accession_no: _primary_doc(Q1, entry_total=3)})
    _enqueue(migrated_engine, Q1)

    result = runner.invoke(app, ["backfill"])

    assert result.exit_code == 0, result.output
    assert _loaded(migrated_engine) == [(Q1.accession_no, "suspect")]
    assert "berkshire-hathaway 2024Q1 · 2 rows · suspect" in result.stdout
    assert "succeeded 0 · skipped 0 · failed 0 · suspect 1" in result.stdout


# --- the run's record --------------------------------------------------------


@respx.mock
def test_a_clean_run_is_recorded_and_every_log_line_carries_its_id(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The question this exists for, as a query: did the backfill finish?
    And the run_id on the row is the one to grep for, which returns every
    line, the per-filing ones and the HTTP requests included."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    result = runner.invoke(app, ["backfill", "--concurrency", "2", "--since", "2024-01-01"])

    assert result.exit_code == 0, result.output
    assert _recorded(migrated_engine) == [("success", 2, 2, None)]
    [(run_id, context)] = _fetch(migrated_engine, select(IngestionRun.id, IngestionRun.context))
    assert context == {
        "filer": None,
        "since": "2024-01-01",
        "concurrency": 2,
        "limit": None,
        "force": False,
    }

    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines
    assert all(f"run_id={run_id}" in line for line in lines), result.stderr
    assert sum("filing.ingested" in line for line in lines) == 2


@respx.mock
def test_a_run_with_a_failed_filing_is_partial_and_names_it(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(Q1, Q2, primary_doc={Q1.accession_no: _primary_doc(Q1)[:200]})
    _enqueue(migrated_engine, Q1, Q2)

    result = runner.invoke(app, ["backfill", "--concurrency", "1"])

    assert result.exit_code == 1
    [(status, seen, written, error)] = _recorded(migrated_engine)
    assert (status, seen, written) == ("partial", 2, 1)
    # One line, in the queue row's words.
    [(last_error,)] = _fetch(
        migrated_engine,
        select(PendingFiling.last_error).where(PendingFiling.accession_no == Q1.accession_no),
    )
    assert error == f"{Q1.accession_no}: {last_error}"


@respx.mock
def test_a_run_stopped_by_ctrl_c_is_partial_and_says_so(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Finished cleanly, but not finished: the next run has work to do."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    def interrupt(request: Request) -> Response:
        signal.raise_signal(signal.SIGINT)
        return _listing()

    respx.get(f"{Q1.directory}/index.json").side_effect = interrupt

    result = runner.invoke(app, ["backfill", "--concurrency", "1"])

    assert result.exit_code == 130, result.output
    assert _recorded(migrated_engine) == [
        ("partial", 2, 1, "stopped with 1 not started; re-run to resume")
    ]


@respx.mock
def test_a_resumed_run_with_nothing_left_records_nothing_taken_on(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Skipped filings are not what the run took on, as with --limit, so a
    resume of a finished backfill is 0 of 0 rather than 0 of 2."""
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)
    assert runner.invoke(app, ["backfill"]).exit_code == 0

    assert runner.invoke(app, ["backfill"]).exit_code == 0

    assert _fetch(
        migrated_engine,
        select(IngestionRun.status, IngestionRun.items_seen, IngestionRun.items_written).order_by(
            IngestionRun.started_at
        ),
    ) == [("success", 2, 2), ("success", 0, 0)]


def test_a_run_that_cannot_start_is_failed(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    result = runner.invoke(app, ["backfill", "--filer", "no-such-fund"])

    assert result.exit_code == 1
    assert _recorded(migrated_engine) == [
        ("failed", 0, 0, "UnknownFilerError: no filer has the slug 'no-such-fund'")
    ]


# --- --force -----------------------------------------------------------------


def _archived(settings: Settings, spec: _Spec) -> dict[str, bytes]:
    async def read() -> dict[str, bytes]:
        store = LocalRawStore(settings.raw_store_local_root)
        return {key: await store.get(key) for key in await store.list(spec.prefix)}

    return asyncio.run(read())


def _rearchive(settings: Settings, key: str, body: bytes) -> None:
    asyncio.run(LocalRawStore(settings.raw_store_local_root).put(key, body, overwrite=True))


@respx.mock
def test_force_reparses_the_archived_documents_without_an_edgar_request(
    runner: CliRunner, migrated_engine: AsyncEngine, cli_settings: Settings
) -> None:
    """The payoff for archiving before parsing: a parser fix is a reparse of
    stored bytes. The archive is edited here to stand in for "what the fixed
    parser reads differently", and EDGAR is left unscripted, so a request
    would fail the filing rather than go unnoticed."""
    _edgar(Q1)
    _enqueue(migrated_engine, Q1)
    assert runner.invoke(app, ["backfill"]).exit_code == 0
    [(filed_at,)] = _fetch(migrated_engine, select(Filing.filed_at))
    _rearchive(cli_settings, f"{Q1.prefix}primary_doc.xml", _primary_doc(Q1, entry_total=1))
    _rearchive(cli_settings, f"{Q1.prefix}infotable.xml", _info_table(APPLE))
    respx.reset()

    result = runner.invoke(app, ["backfill", "--force"])

    assert result.exit_code == 0, result.output
    assert not respx.calls
    assert "1 to reprocess from the raw store" in result.stdout
    assert _fetch(
        migrated_engine,
        select(Security.cusip).join(Holding, Holding.security_id == Security.id),
    ) == [(APPLE,)]
    # From the row, since the documents do not carry it; the units hang off it.
    assert _fetch(migrated_engine, select(Filing.filed_at)) == [(filed_at,)]
    assert _fetch(migrated_engine, select(Filing.source_url)) == [
        (f"{Q1.directory}/primary_doc.xml",)
    ]


@respx.mock
def test_force_fails_a_filing_loaded_before_it_was_archived_and_says_why(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Rather than quietly going to EDGAR for it, which is what --force
    promises not to do."""
    _execute(
        migrated_engine,
        insert(Filing).values(
            accession_no=Q1.accession_no,
            cik=BERKSHIRE,
            filer_id=1,
            form_type="13F-HR",
            period_of_report=Q1.period,
            filed_at=datetime(2024, 5, 15, 20, 5, 4, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        ),
    )

    result = runner.invoke(app, ["backfill", "--force"])

    assert result.exit_code == 1
    assert not respx.calls
    assert "nothing to reprocess" in result.stdout
    assert f"ingest-filing {Q1.accession_no} --force" in result.stdout


# --- narrowing the run -------------------------------------------------------


@respx.mock
def test_filer_restricts_the_run_to_one_filer(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(Q1, PS)
    _enqueue(migrated_engine, Q1, PS)

    result = runner.invoke(app, ["backfill", "--filer", "pershing-square"])

    assert result.exit_code == 0, result.output
    assert _loaded(migrated_engine) == [(PS.accession_no, "ok")]
    assert _queue(migrated_engine) == [
        (Q1.accession_no, "pending", 0),
        (PS.accession_no, "done", 0),
    ]


def test_an_unknown_filer_is_an_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["backfill", "--filer", "no-such-fund"])

    assert result.exit_code == 1
    assert "no-such-fund" in result.stderr


@respx.mock
def test_since_leaves_older_filings_alone(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _edgar(Q1, Q2)
    _enqueue(migrated_engine, Q1, Q2)

    result = runner.invoke(app, ["backfill", "--since", "2024-06-01"])

    assert result.exit_code == 0, result.output
    assert _loaded(migrated_engine) == [(Q2.accession_no, "ok")]


def test_an_empty_queue_says_where_the_work_comes_from(runner: CliRunner) -> None:
    result = runner.invoke(app, ["backfill"])

    assert result.exit_code == 0, result.output
    assert "discover-filings" in result.stdout
