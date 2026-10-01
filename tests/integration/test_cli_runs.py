"""Every verb records its run, and ``runs`` lists them.

The first half is the rule as a test: each command registered on the CLI is
either run here against an empty database and seen to leave exactly one
finished ``ingestion_run`` row of its job, with the ``run_id`` on its log
lines, or is named in :data:`UNTRACKED` with the reason it cannot be. A new
verb that is neither fails :func:`test_every_command_is_tracked_or_says_why_not`.
A verb that publishes may leave a ``refresh-views`` row after its own, which
is the refresh it ran: a run of its own, tested in test_materialised_views.

The second half is ``runs`` itself, over rows inserted directly so that their
times, statuses and errors are whatever the test needs.
"""

import asyncio
import logging
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import IngestionRun
from tests.conftest import make_settings

#: Each tracked verb, the job name it records under, an invocation that runs
#: in full against an empty database without touching EDGAR, and the status
#: that invocation ends in.
TRACKED: Final = {
    "ingest-filing": ("ingest-filing", ["0001067983-24-000011"], "failed"),
    "discover-filings": ("discover-filings", [], "success"),
    "backfill": ("backfill_13f", [], "success"),
    "seed-investors": ("seed-investors", ["--dry-run"], "success"),
    "audit-overlaps": ("audit-overlaps", [], "success"),
    "audit-amendments": ("audit-amendments", [], "success"),
    "check-data": ("check-data", [], "success"),
    "recompute": ("recompute", ["--all"], "success"),
    "refresh-views": ("refresh-views", [], "success"),
}

#: The verbs that do not record a run, and why. Keep this short.
UNTRACKED: Final = {
    "runs": "reads the record; a listing that added itself would show itself first",
    "verify-investors": "never opens a database: it runs on the host and in a GitHub workflow",
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """The container's DSN; see test_cli_ingest_filing."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE ingestion_run, matview_refresh, pending_filing, position_snapshot, "
            "holding, filing, security, filer_cik, filer RESTART IDENTITY CASCADE"
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


# --- the rule ----------------------------------------------------------------


def test_every_command_is_tracked_or_says_why_not() -> None:
    registered = {command.name for command in app.registered_commands}

    assert registered == TRACKED.keys() | UNTRACKED.keys(), (
        "a CLI verb must record its run with track_run (and be added to TRACKED), "
        "or be in UNTRACKED with the reason it cannot"
    )
    assert not TRACKED.keys() & UNTRACKED.keys()


@pytest.mark.parametrize("command", sorted(TRACKED))
def test_a_run_of_the_command_leaves_one_finished_row(
    runner: CliRunner, migrated_engine: AsyncEngine, command: str
) -> None:
    job_name, args, status = TRACKED[command]

    result = runner.invoke(app, [command, *args])

    assert result.exit_code == (1 if status == "failed" else 0), result.output
    [(run_id, recorded_status, finished_at)] = _fetch(
        migrated_engine,
        select(IngestionRun.id, IngestionRun.status, IngestionRun.finished_at).where(
            IngestionRun.job_name == job_name
        ),
    )
    assert recorded_status == status
    assert finished_at is not None
    # The run's first and last log lines, at least, carry the row's id.
    for event in ("ingestion_run.started", "ingestion_run.finished"):
        [line] = [
            line
            for line in result.stderr.splitlines()
            if event in line and f"job_name={job_name}" in line
        ]
        assert f"run_id={run_id}" in line


@pytest.mark.parametrize("command", sorted(UNTRACKED))
def test_the_untracked_commands_leave_no_row(
    runner: CliRunner, migrated_engine: AsyncEngine, command: str, tmp_path: Path
) -> None:
    """``verify-investors`` on a list that is not there, so it stops before a request."""
    args = ["--file", str(tmp_path / "missing.yaml")] if command == "verify-investors" else []

    runner.invoke(app, [command, *args])

    assert _fetch(migrated_engine, select(IngestionRun.id)) == []


def test_a_failed_ingest_records_why(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    """The run row says what stderr said, so the answer survives the terminal."""
    result = runner.invoke(app, ["ingest-filing", "0001067983-24-000011"])

    assert result.exit_code == 1
    [(error, context)] = _fetch(migrated_engine, select(IngestionRun.error, IngestionRun.context))
    assert error.startswith("CommandError: 0001067983-24-000011 is not in the database")
    assert context == {
        "accession_no": "0001067983-24-000011",
        "cik": None,
        "force": False,
        "dry_run": False,
        "refresh_views": True,
    }


def test_a_malformed_argument_is_still_a_recorded_run(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    result = runner.invoke(app, ["ingest-filing", "not-an-accession"])

    assert result.exit_code == 1
    [(status, context)] = _fetch(migrated_engine, select(IngestionRun.status, IngestionRun.context))
    assert status == "failed"
    assert context["accession_no"] == "not-an-accession"


def test_seed_investors_counts_the_list_and_writes_nothing_on_a_dry_run(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    dry = runner.invoke(app, ["seed-investors", "--dry-run"])
    real = runner.invoke(app, ["seed-investors"])

    assert dry.exit_code == real.exit_code == 0
    [(dry_run, dry_seen, dry_written), (real_run, real_seen, real_written)] = _fetch(
        migrated_engine,
        select(
            IngestionRun.context["dry_run"], IngestionRun.items_seen, IngestionRun.items_written
        ).order_by(IngestionRun.started_at),
    )
    assert (dry_run, real_run) == (True, False)
    assert dry_seen == real_seen > 0
    assert dry_written == 0
    # Every filer is new to an empty database.
    assert real_written == real_seen


# --- runs --------------------------------------------------------------------

_NOW: Final = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _run(
    engine: AsyncEngine,
    job_name: str,
    *,
    minutes_ago: int,
    status: str = "success",
    took: timedelta | None = timedelta(seconds=12),
    error: str | None = None,
    seen: int = 0,
    written: int = 0,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    started = _NOW - timedelta(minutes=minutes_ago)
    _execute(
        engine,
        insert(IngestionRun).values(
            id=run_id,
            job_name=job_name,
            status=status,
            started_at=started,
            finished_at=None if took is None else started + took,
            items_seen=seen,
            items_written=written,
            error=error,
        ),
    )
    return run_id


def test_runs_lists_the_latest_first_with_what_they_counted(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    old = _run(migrated_engine, "discover-filings", minutes_ago=90, seen=61, written=21)
    new = _run(
        migrated_engine,
        "backfill_13f",
        minutes_ago=10,
        status="partial",
        took=timedelta(minutes=3, seconds=12),
        error="0001193125-22-000123: FilingDocumentsError: no information table\n"
        "0001193125-22-000456: EdgarServerError: 503",
        seen=21,
        written=19,
    )

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "runs  2 most recent runs",
        "  started                    job               status   elapsed    seen  written  run_id",
        f"  2026-10-01 11:50:00+00:00  backfill_13f      partial    3m12s      21       19  {new}",
        "                             0001193125-22-000123: FilingDocumentsError: no information "
        "table  (+1 more)",
        f"  2026-10-01 10:30:00+00:00  discover-filings  success    12.0s      61       21  {old}",
    ]


def test_runs_filters_by_job_and_limits(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _run(migrated_engine, "backfill_13f", minutes_ago=30)
    latest = _run(migrated_engine, "backfill_13f", minutes_ago=20)
    _run(migrated_engine, "check-data", minutes_ago=10)

    result = runner.invoke(app, ["runs", "--job", "backfill_13f", "--limit", "1"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "runs  1 most recent run of backfill_13f"
    assert len(lines) == 3
    assert lines[2].endswith(str(latest))


def test_a_run_still_going_is_timed_to_now(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    """By the database's clock. A ``running`` row two hours old says so, which
    is what makes a process that died without recording it stand out."""
    _execute(
        migrated_engine,
        insert(IngestionRun).values(
            id=uuid.uuid4(), job_name="backfill_13f", started_at=text("now() - interval '2 hours'")
        ),
    )

    result = runner.invoke(app, ["runs"])

    [line] = [line for line in result.stdout.splitlines() if "backfill_13f" in line]
    assert line.split()[3:5] == ["running", "2h00m"]


def test_an_unknown_job_name_lists_the_ones_that_have_runs(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """``backfill`` is the verb; ``backfill_13f`` is the job. Easy to mix up."""
    _run(migrated_engine, "backfill_13f", minutes_ago=10)
    _run(migrated_engine, "check-data", minutes_ago=5)

    result = runner.invoke(app, ["runs", "--job", "backfill"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == (
        "runs  no runs of backfill recorded; jobs with runs: backfill_13f, check-data"
    )


def test_runs_on_an_empty_table(runner: CliRunner) -> None:
    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "runs  no runs recorded"
