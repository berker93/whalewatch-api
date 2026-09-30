"""``discover-filings``: EDGAR's listing, less what is loaded, into ``pending_filing``.

Against a real Postgres because the behaviour worth asserting is the set
difference and the queue rows it leaves — including what a second run, a failed
load and a hand-deleted filing do to them. EDGAR is faked with respx, as in
``test_cli_ingest_filing``; the commits are real, so tables are truncated
between tests rather than rolled back.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
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
from app.db.models import Filer, FilerCik, Filing, PendingFiling
from app.db.session import create_session_factory
from app.ingestion.discovery import default_since, discover_filings
from app.ingestion.edgar.client import EdgarClient
from tests.conftest import make_settings

BERKSHIRE: Final = "0001067983"
PERSHING_OLD: Final = "0001336528"
PERSHING_NEW: Final = "0002026053"

OLD: Final = "0001193125-15-000001"
LOADED: Final = "0001193125-24-000001"
NEW: Final = "0001193125-24-000002"
AMENDED: Final = "0001193125-24-000003"
NOTICE: Final = "0001193125-24-000004"
FORM_4: Final = "0001193125-24-000005"

#: (accession, form, filingDate, reportDate), the rows Berkshire's index lists.
_BERKSHIRE_ROWS: Final = (
    (OLD, "13F-HR", "2015-02-17", "2014-12-31"),
    (LOADED, "13F-HR", "2024-05-15", "2024-03-31"),
    (NEW, "13F-HR", "2024-08-14", "2024-06-30"),
    (AMENDED, "13F-HR/A", "2024-11-14", "2024-06-30"),
    (NOTICE, "13F-NT", "2024-08-14", "2024-06-30"),
    (FORM_4, "4", "2024-08-14", ""),
)

#: Keeps the pre-2020 filing out, so --all has something to add.
_SINCE: Final = "2020-01-01"


def _submissions(cik: str, *rows: tuple[str, str, str, str]) -> dict[str, object]:
    return {
        "cik": str(int(cik)),
        "name": f"FILER {cik}",
        "filings": {
            "recent": {
                "accessionNumber": [row[0] for row in rows],
                "form": [row[1] for row in rows],
                "filingDate": [row[2] for row in rows],
                "reportDate": [row[3] for row in rows],
            },
            "files": [],
        },
    }


def _edgar(cik: str, *rows: tuple[str, str, str, str]) -> None:
    respx.get(f"https://data.sec.gov/submissions/CIK{cik}.json").mock(
        Response(200, json=_submissions(cik, *rows))
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """The container's DSN and an unthrottled client; see test_cli_ingest_filing."""
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
    yield
    _truncate(migrated_engine)


@pytest.fixture
def berkshire(migrated_engine: AsyncEngine) -> None:
    """Berkshire tracked, one filing of its index loaded."""
    _execute(
        migrated_engine,
        insert(Filer).values(id=1, name="Berkshire Hathaway Inc", slug="berkshire-hathaway"),
        insert(FilerCik).values(filer_id=1, cik=BERKSHIRE),
    )
    _load(migrated_engine, LOADED, status="ok")


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE pending_filing, holding, filing, security, filer_cik, filer "
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


def _load(engine: AsyncEngine, accession_no: str, *, status: str) -> None:
    """A ``filing`` row in ``status``, as the loader would leave one."""
    _execute(
        engine,
        insert(Filing).values(
            accession_no=accession_no,
            cik=BERKSHIRE,
            filer_id=1,
            form_type="13F-HR",
            period_of_report=date(2024, 3, 31),
            filed_at=datetime(2024, 5, 15, 20, tzinfo=UTC),
            value_multiplier=1,
            parse_status=status,
        ),
    )


def _queue(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            PendingFiling.accession_no,
            PendingFiling.cik,
            PendingFiling.form_type,
            PendingFiling.status,
        ).order_by(PendingFiling.accession_no),
    )


def _enqueue(engine: AsyncEngine, accession_no: str, **columns: Any) -> None:
    _execute(
        engine,
        insert(PendingFiling).values(
            accession_no=accession_no,
            cik=BERKSHIRE,
            form_type="13F-HR",
            filing_date=date(2024, 8, 14),
            **columns,
        ),
    )


# --- the set difference ------------------------------------------------------


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_filings_not_loaded_are_queued_and_the_loaded_one_is_not(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert result.exit_code == 0, result.output
    # The notice, the Form 4 and the 2015 filing are not in range or not 13F-HRs.
    assert _queue(migrated_engine) == [
        (NEW, BERKSHIRE, "13F-HR", "pending"),
        (AMENDED, BERKSHIRE, "13F-HR/A", "pending"),
    ]


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_the_report_gives_found_already_ingested_and_new_per_filer(
    runner: CliRunner,
) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert "filed since 2020-01-01" in result.stdout
    assert "found  ingested     new" in result.stdout
    assert "berkshire-hathaway       3         1       2" in result.stdout
    assert "1 filers: 3 found, 1 already ingested, 2 new" in result.stdout


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_all_reaches_back_past_the_default_window(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    result = runner.invoke(app, ["discover-filings", "--all"])

    assert result.exit_code == 0, result.output
    assert "full history" in result.stdout
    assert [row[0] for row in _queue(migrated_engine)] == [OLD, NEW, AMENDED]


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_the_default_window_is_five_years(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    today = date.today()
    _edgar(
        BERKSHIRE,
        (OLD, "13F-HR", (today - timedelta(days=6 * 366)).isoformat(), "2014-12-31"),
        (NEW, "13F-HR", (today - timedelta(days=30)).isoformat(), "2024-06-30"),
    )

    result = runner.invoke(app, ["discover-filings"])

    assert result.exit_code == 0, result.output
    assert f"filed since {default_since(today).isoformat()}" in result.stdout
    assert [row[0] for row in _queue(migrated_engine)] == [NEW]


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_a_filing_row_that_never_loaded_is_still_new(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """``filing`` rows in ``pending`` or ``failed`` have no holdings, and
    ``ingest-filing`` treats them as work to do. Discovery agrees."""
    _load(migrated_engine, NEW, status="failed")
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert NEW in [row[0] for row in _queue(migrated_engine)]


# --- running it again --------------------------------------------------------


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_a_second_run_leaves_the_queue_as_it_was(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)
    runner.invoke(app, ["discover-filings", "--since", _SINCE])
    first = _fetch(migrated_engine, select(PendingFiling.accession_no, PendingFiling.discovered_at))

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert result.exit_code == 0, result.output
    # Still reported as new: nobody has loaded them. Still queued once each.
    assert "2 new" in result.stdout
    assert (
        _fetch(migrated_engine, select(PendingFiling.accession_no, PendingFiling.discovered_at))
        == first
    )


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_a_filing_that_failed_to_load_is_found_again_with_its_history(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The reason for a set difference: no special case finds this again."""
    _enqueue(migrated_engine, NEW, status="failed", attempts=2, last_error="EdgarServerError: 503")
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert "2 new" in result.stdout
    assert _fetch(
        migrated_engine,
        select(PendingFiling.status, PendingFiling.attempts, PendingFiling.last_error).where(
            PendingFiling.accession_no == NEW
        ),
    ) == [("failed", 2, "EdgarServerError: 503")]


@respx.mock
@pytest.mark.usefixtures("berkshire")
def test_the_queue_is_reconciled_with_what_is_loaded(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """A row loaded behind the queue's back is marked done; a row marked done
    whose filing is gone goes back to pending."""
    _enqueue(migrated_engine, LOADED, status="pending")
    _enqueue(migrated_engine, NEW, status="done")
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    runner.invoke(app, ["discover-filings", "--since", _SINCE])

    statuses = dict(
        _fetch(migrated_engine, select(PendingFiling.accession_no, PendingFiling.status))
    )
    assert statuses == {LOADED: "done", NEW: "pending", AMENDED: "pending"}


# --- filers and their CIKs ---------------------------------------------------


@pytest.fixture
def pershing(migrated_engine: AsyncEngine) -> None:
    _execute(
        migrated_engine,
        insert(Filer).values(id=2, name="Pershing Square", slug="pershing-square"),
        insert(FilerCik).values(filer_id=2, cik=PERSHING_OLD, priority=0),
        insert(FilerCik).values(filer_id=2, cik=PERSHING_NEW, priority=1),
    )


@respx.mock
@pytest.mark.usefixtures("pershing")
def test_every_cik_a_filer_files_under_is_listed(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(PERSHING_OLD, ("0001336528-24-000001", "13F-HR", "2024-02-14", "2023-12-31"))
    _edgar(PERSHING_NEW, ("0002026053-25-000001", "13F-HR", "2025-05-15", "2025-03-31"))

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert result.exit_code == 0, result.output
    # Each under the CIK whose archive holds it, which ingest-filing will need.
    assert [row[:2] for row in _queue(migrated_engine)] == [
        ("0001336528-24-000001", PERSHING_OLD),
        ("0002026053-25-000001", PERSHING_NEW),
    ]


@respx.mock
@pytest.mark.usefixtures("pershing")
def test_a_cik_edgar_cannot_list_fails_the_run_but_not_the_other_ciks(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    respx.get(f"https://data.sec.gov/submissions/CIK{PERSHING_OLD}.json").mock(Response(404))
    _edgar(PERSHING_NEW, ("0002026053-25-000001", "13F-HR", "2025-05-15", "2025-03-31"))

    result = runner.invoke(app, ["discover-filings", "--since", _SINCE])

    assert result.exit_code == 1
    assert "FAILED" in result.stdout
    assert f"pershing-square CIK {PERSHING_OLD}" in result.stdout
    assert [row[0] for row in _queue(migrated_engine)] == ["0002026053-25-000001"]


@respx.mock
@pytest.mark.usefixtures("berkshire", "pershing")
def test_filer_limits_the_run_to_one_filer(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    result = runner.invoke(app, ["discover-filings", "--filer", "berkshire-hathaway"])

    assert result.exit_code == 0, result.output
    assert "pershing-square" not in result.stdout
    # Only Berkshire's index was asked for; Pershing's routes were never mocked.
    assert len(respx.calls) == 1


def test_an_unknown_slug_is_an_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["discover-filings", "--filer", "no-such-fund"])

    assert result.exit_code == 1
    assert "no-such-fund" in result.stderr


def test_since_and_all_together_are_refused(runner: CliRunner) -> None:
    result = runner.invoke(app, ["discover-filings", "--since", _SINCE, "--all"])

    assert result.exit_code == 2
    assert "contradict" in result.stderr


# --- the function the command wraps ------------------------------------------


@respx.mock
@pytest.mark.usefixtures("berkshire")
async def test_discover_filings_returns_the_new_refs(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    _edgar(BERKSHIRE, *_BERKSHIRE_ROWS)

    async with EdgarClient(settings) as edgar:
        discovery = await discover_filings(
            create_session_factory(migrated_engine), edgar, 1, since=date(2020, 1, 1)
        )

    assert discovery.already_ingested == {LOADED}
    assert [row.filing.accession_no for row in discovery.new] == [NEW, AMENDED]
    assert all(row.cik == BERKSHIRE for row in discovery.new)
