"""``import-13f-list``, end to end: the SEC's page and file in, rows out.

Against a real Postgres and a real (temporary) raw store, with respx where
sec.gov would be. As in test_cli_ingest_filing, the command commits through its
own sessions, so tables are truncated around each test rather than rolled back.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
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
from app.db.models import IngestionRun, Sec13FSecurity
from app.ingestion.edgar.thirteen_f_list import LIST_PAGE_URL
from app.storage.raw import LocalRawStore
from tests.conftest import make_settings

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "13f_list"
PAGE: Final = (FIXTURES / "13flists.html").read_bytes()
LIST_2026Q1: Final = (FIXTURES / "13flist2026q1.txt").read_bytes()

Q1: Final = date(2026, 3, 31)
Q2: Final = date(2026, 6, 30)
Q1_URL: Final = "https://www.sec.gov/files/investment/13flist2026q1.txt"
Q2_URL: Final = "https://www.sec.gov/files/investment/13flist2026q2-txt.txt"
Q1_KEY: Final = "raw/13f-list/2026Q1/13flist2026q1.txt"
Q2_KEY: Final = "raw/13f-list/2026Q2/13flist2026q2-txt.txt"


def _list_line(cusip: str, name: str, description: str, status: str = "   ") -> bytes:
    """One line in the published layout."""
    return f"{cusip} {name:<30}{description:<27}{status}{'':<9}E\n".encode()


#: A 2026Q2 list that follows from the 2026Q1 fixture: Apple still listed,
#: one CUSIP the fixture does not have added.
LIST_2026Q2: Final = _list_line("037833100", "APPLE INC", "COM") + _list_line(
    "G0R99Z101", "NEW ACQUISITION CORP", "CL A", "*A*"
)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """The container's DSN, and an unthrottled EDGAR client; see test_cli_ingest_filing."""
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


def _truncate(engine: AsyncEngine) -> None:
    _execute(engine, text("TRUNCATE ingestion_run, sec_13f_security RESTART IDENTITY"))


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


def _rows(engine: AsyncEngine, period: date) -> dict[str, tuple[Any, ...]]:
    table = Sec13FSecurity
    rows = _fetch(
        engine,
        select(
            table.cusip,
            table.quarter,
            table.issuer_name,
            table.issuer_description,
            table.status,
            table.has_listed_options,
        ).where(table.period_of_report == period),
    )
    return {row[0]: row[1:] for row in rows}


def _archived(settings: Settings, key: str) -> bytes:
    return asyncio.run(LocalRawStore(settings.raw_store_local_root).get(key))


def _archive(settings: Settings, key: str, body: bytes) -> None:
    asyncio.run(LocalRawStore(settings.raw_store_local_root).put(key, body))


def _sec(*, q1: bytes = LIST_2026Q1, q2: bytes = LIST_2026Q2) -> None:
    respx.get(LIST_PAGE_URL).mock(Response(200, content=PAGE))
    respx.get(Q1_URL).mock(Response(200, content=q1))
    respx.get(Q2_URL).mock(Response(200, content=q2))


def _last_run(engine: AsyncEngine) -> tuple[Any, ...]:
    [row] = _fetch(
        engine,
        select(IngestionRun.status, IngestionRun.items_seen, IngestionRun.items_written)
        .add_columns(IngestionRun.metrics, IngestionRun.error)
        .where(IngestionRun.job_name == "import-13f-list")
        .order_by(IngestionRun.started_at.desc())
        .limit(1),
    )
    return row


# --- a first import ----------------------------------------------------------


@respx.mock
def test_an_import_finds_the_list_on_the_page_archives_it_and_writes_each_cusip_once(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine
) -> None:
    _sec()

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    assert result.exit_code == 0, result.output
    assert _archived(cli_settings, Q1_KEY) == LIST_2026Q1
    rows = _rows(migrated_engine, Q1)
    assert len(rows) == 15
    assert rows["037833100"] == ("2026Q1", "APPLE INC", "COM", "listed", True)
    assert rows["G0R21F121"][3] == "added"
    assert rows["G0223V105"][3] == "deleted"
    assert rows["M8216Q909"][3] == "listed"
    assert [call.request.url for call in respx.calls] == [LIST_PAGE_URL, Q1_URL]


@respx.mock
def test_the_summary_says_what_was_read_and_written(runner: CliRunner) -> None:
    _sec()

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    assert result.stdout.splitlines() == [
        "import-13f-list  2026Q1  13flist2026q1.txt",
        f"  source      {Q1_URL}",
        f"  archived    {Q1_KEY}",
        "  lines       19 read, 4 repeating a CUSIP and collapsed",
        "  securities  15: 10 listed, 3 added, 2 deleted; 5 with listed options",
        "  checked     not checked: 2025Q4 is not imported",
        "  replaced    nothing, first import",
    ]


@respx.mock
def test_the_run_is_recorded_with_where_the_list_came_from(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _sec()

    runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    status, seen, written, metrics, _ = _last_run(migrated_engine)
    assert (status, seen, written) == ("success", 19, 15)
    assert metrics == {
        "source": Q1_URL,
        "raw_key": Q1_KEY,
        "archived": True,
        "collapsed": 4,
        "added": 3,
        "deleted": 2,
        "replaced": 0,
        "checked_against": None,
        "already_listed": 0,
    }


# --- again -------------------------------------------------------------------


@respx.mock
def test_a_second_import_reads_the_archive_and_asks_the_sec_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _sec()
    runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])
    before = _rows(migrated_engine, Q1)
    calls = len(respx.calls)

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    assert result.exit_code == 0, result.output
    assert len(respx.calls) == calls
    assert _rows(migrated_engine, Q1) == before
    assert f"  source      {Q1_KEY}" in result.stdout
    assert "  archived    already archived" in result.stdout
    assert "  replaced    15 rows" in result.stdout


@respx.mock
def test_force_fetches_again_and_the_quarter_is_replaced_whole(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """A CUSIP the corrected file no longer has is gone, not left over."""
    _sec()
    runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])
    corrected = _list_line("037833100", "APPLE INC", "COM")
    respx.get(Q1_URL).mock(Response(200, content=corrected))

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1", "--force"])

    assert result.exit_code == 0, result.output
    assert list(_rows(migrated_engine, Q1)) == ["037833100"]
    assert _archived(cli_settings, Q1_KEY) == corrected


@respx.mock
def test_each_quarter_keeps_its_own_rows(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _sec()

    runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])
    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q2"])

    assert result.exit_code == 0, result.output
    assert len(_rows(migrated_engine, Q1)) == 15
    assert _rows(migrated_engine, Q2) == {
        "037833100": ("2026Q2", "APPLE INC", "COM", "listed", False),
        "G0R99Z101": ("2026Q2", "NEW ACQUISITION CORP", "CL A", "added", False),
    }
    assert "  checked     1 additions against 2026Q1: none already listed" in result.stdout


@respx.mock
def test_with_several_copies_archived_the_page_says_which_is_current(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """The SEC replacing a file under a new name leaves two in the archive. The
    one the page links is used, from the archive, without downloading it."""
    _sec()
    _archive(cli_settings, Q1_KEY, LIST_2026Q1)
    _archive(cli_settings, "raw/13f-list/2026Q1/13flist2026q1-old.txt", b"not this one")

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    assert result.exit_code == 0, result.output
    assert [call.request.url for call in respx.calls] == [LIST_PAGE_URL]
    assert len(_rows(migrated_engine, Q1)) == 15


# --- refusals ----------------------------------------------------------------


@respx.mock
def test_a_list_whose_additions_were_already_listed_is_refused_but_archived(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """The SEC's 2025Q3 file is its 2025Q1 file. This is that, a quarter on."""
    stale = _list_line("037833100", "APPLE INC", "COM", "*A*") + _list_line(
        "084670702", "BERKSHIRE HATHAWAY INC DEL", "CL B NEW", "*A*"
    )
    _sec(q2=stale)
    runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q2"])

    assert result.exit_code == 1
    assert (
        "2026Q2's list marks 2 CUSIPs added, and 2 of them were already on 2026Q1's list"
        in result.stderr
    )
    assert _rows(migrated_engine, Q2) == {}
    assert _archived(cli_settings, Q2_KEY) == stale
    status, *_, error = _last_run(migrated_engine)
    assert status == "failed"
    assert "probably another quarter's file" in error


@respx.mock
def test_a_pdf_only_quarter_is_refused_without_fetching_the_pdf(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _sec()

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2019Q4"])

    assert result.exit_code == 1
    assert "only as a PDF" in result.stderr
    assert [call.request.url for call in respx.calls] == [LIST_PAGE_URL]
    assert _last_run(migrated_engine)[0] == "failed"


@respx.mock
def test_a_malformed_list_is_archived_before_it_fails_and_nothing_is_written(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine
) -> None:
    _sec(q1=b"<html>Page Not Found</html>\n")

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1"])

    assert result.exit_code == 1
    assert "line 1: columns 1-9 are not a CUSIP" in result.stderr
    assert _archived(cli_settings, Q1_KEY) == b"<html>Page Not Found</html>\n"
    assert _rows(migrated_engine, Q1) == {}


def test_a_quarter_that_is_not_one_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026-03"])

    assert result.exit_code == 2
    assert "is not a quarter" in result.stderr


# --- --file ------------------------------------------------------------------


@respx.mock
def test_file_imports_a_local_copy_and_archives_it_without_the_sec(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    path = tmp_path / "13flist2026q1.txt"
    path.write_bytes(LIST_2026Q1)

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1", "--file", str(path)])

    assert result.exit_code == 0, result.output
    assert len(respx.calls) == 0
    assert _archived(cli_settings, Q1_KEY) == LIST_2026Q1
    assert len(_rows(migrated_engine, Q1)) == 15
    assert f"  source      {path}" in result.stdout


def test_file_named_for_another_quarter_is_refused(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    path = tmp_path / "13flist2026q2-txt.txt"
    path.write_bytes(LIST_2026Q2)

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1", "--file", str(path)])

    assert result.exit_code == 1
    assert "13flist2026q2-txt.txt is 2026Q2's list by its name, not 2026Q1's" in result.stderr
    assert _rows(migrated_engine, Q1) == {}


def test_file_that_differs_from_the_archived_copy_needs_force(
    runner: CliRunner, cli_settings: Settings, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    _archive(cli_settings, Q1_KEY, LIST_2026Q1)
    path = tmp_path / "13flist2026q1.txt"
    path.write_bytes(_list_line("037833100", "APPLE INC", "COM"))
    args = ["import-13f-list", "--quarter", "2026Q1", "--file", str(path)]

    refused = runner.invoke(app, args)
    forced = runner.invoke(app, [*args, "--force"])

    assert refused.exit_code == 1
    assert f"a different 13flist2026q1.txt is archived at {Q1_KEY}" in refused.stderr
    assert forced.exit_code == 0, forced.output
    assert list(_rows(migrated_engine, Q1)) == ["037833100"]


def test_a_missing_file_is_one_line(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "nowhere.txt"

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1", "--file", str(path)])

    assert result.exit_code == 1
    assert result.stderr.strip().splitlines()[-1].startswith(f"error: could not read {path}")


def test_rows_from_another_quarter_never_count_against_the_check(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Apple listed two quarters back, not one, does not make it a re-addition."""
    _execute(
        migrated_engine,
        insert(Sec13FSecurity).values(
            period_of_report=date(2025, 9, 30),
            cusip="037833100",
            issuer_name="APPLE INC",
            status="listed",
            has_listed_options=True,
        ),
    )
    path = tmp_path / "13flist2026q1.txt"
    path.write_bytes(_list_line("037833100", "APPLE INC", "COM", "*A*"))

    result = runner.invoke(app, ["import-13f-list", "--quarter", "2026Q1", "--file", str(path)])

    assert result.exit_code == 0, result.output
    assert "not checked: 2025Q4 is not imported" in result.stdout
