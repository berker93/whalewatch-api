"""Jobs against Postgres: one run at a time, one row per run, and the tasks run directly.

The lock is taken for real, on a connection of its own in another thread, as
another worker or a person at a shell would hold it. The tasks are called as
functions, the way a worker calls them once the message is off the broker,
so what is tested is the code that runs, without the broker in the loop.
"""

import asyncio
import logging
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Executable, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import IngestionRun
from app.derived.views import MATERIALISED_VIEWS
from app.jobs import tasks
from app.jobs.locks import job_lock
from app.jobs.tracking import JobLocked, TrackedRun, tracked_task
from tests.conftest import make_settings


@pytest.fixture
def committed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """Jobs commit through sessions of their own, so the tables are truncated
    around the test rather than rolled back."""
    monkeypatch.setattr("app.jobs.tasks.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    _truncate(migrated_engine)
    yield settings
    _truncate(migrated_engine)
    # A CLI run pointed logging at the runner's stderr, which is closed now.
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def _truncate(engine: AsyncEngine) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE ingestion_run, matview_refresh, pending_filing, position_change, "
                    "position_snapshot, holding, filing, security, filer_cik, filer "
                    "RESTART IDENTITY CASCADE"
                )
            )
            for view in MATERIALISED_VIEWS:
                await connection.execute(text(f"REFRESH MATERIALIZED VIEW {view.name}"))

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in await connection.execute(statement)]

    return asyncio.run(run())


def _runs(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    """Every run, oldest first: id, job, status and context."""
    return _fetch(
        engine,
        select(
            IngestionRun.id, IngestionRun.job_name, IngestionRun.status, IngestionRun.context
        ).order_by(IngestionRun.started_at),
    )


@contextmanager
def _held_elsewhere(settings: Settings, job_name: str) -> Iterator[None]:
    """``job_name``'s lock, held on another connection in another thread for the block."""
    taken, release = threading.Event(), threading.Event()

    async def hold() -> None:
        async with job_lock(settings, job_name) as held:
            if held:
                taken.set()
            await asyncio.to_thread(release.wait, 30)

    thread = threading.Thread(target=asyncio.run, args=(hold(),))
    thread.start()
    try:
        assert taken.wait(10), f"could not take {job_name}'s lock"
        yield
    finally:
        release.set()
        thread.join(10)


# --- the decorator -----------------------------------------------------------

_BODY_RAN: list[str] = []


@tracked_task("test-probe")
async def _probe(run: TrackedRun, settings: Settings, *, fail: bool = False) -> bool:
    """Whether a second run could take the job's lock while this one runs."""
    _BODY_RAN.append(run.job_name)
    async with job_lock(settings, "test-probe") as second:
        could = second
    run.items_seen = 1
    if fail:
        raise RuntimeError("the probe failed")
    return could


def test_a_run_holds_its_lock_records_its_row_and_releases_the_lock(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    first = asyncio.run(_probe(committed))
    second = asyncio.run(_probe(committed))

    assert first.value is second.value is False
    [(first_id, job, status, context), (second_id, *_)] = _runs(migrated_engine)
    assert (first_id, second_id) == (first.run.id, second.run.id)
    # The default is recorded too: two runs alike have the same context.
    assert (job, status, context) == ("test-probe", "success", {"fail": False})


def test_a_run_that_fails_is_recorded_and_still_releases_the_lock(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    with pytest.raises(RuntimeError, match="the probe failed"):
        asyncio.run(_probe(committed, fail=True))
    asyncio.run(_probe(committed))

    assert [(status, context) for _, _, status, context in _runs(migrated_engine)] == [
        ("failed", {"fail": True}),
        ("success", {"fail": False}),
    ]


def test_a_run_that_finds_the_lock_taken_does_nothing_and_leaves_no_row(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    _BODY_RAN.clear()

    with _held_elsewhere(committed, "test-probe"), pytest.raises(JobLocked):
        asyncio.run(_probe(committed))

    assert _BODY_RAN == []
    assert _runs(migrated_engine) == []


def test_locks_are_per_job(committed: Settings, migrated_engine: AsyncEngine) -> None:
    with _held_elsewhere(committed, "refresh-views"):
        asyncio.run(_probe(committed))

    assert [job for _, job, _, _ in _runs(migrated_engine)] == ["test-probe"]


# --- both entry points -------------------------------------------------------


def test_the_cli_reports_a_locked_job_as_skipped_and_exits_zero(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    with _held_elsewhere(committed, "refresh-views"):
        result = CliRunner().invoke(app, ["refresh-views"])

    assert result.exit_code == 0, result.output
    assert "skipped: another refresh-views run is in progress" in result.stderr
    assert _runs(migrated_engine) == []


def test_a_task_run_directly_leaves_the_row_its_verb_does(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    """The same job, job_name and context, whichever entry point ran it."""
    by_hand = CliRunner().invoke(app, ["refresh-views"])
    summary = tasks.refresh_matviews()

    assert by_hand.exit_code == 0, by_hand.output
    [(_, cli_job, _, cli_context), (task_id, task_job, task_status, task_context)] = _runs(
        migrated_engine
    )
    assert cli_job == task_job == summary["job"] == "refresh-views"
    assert cli_context == task_context == {"view": None, "concurrent": True, "after_run_id": None}
    assert summary == {
        "job": "refresh-views",
        "run_id": str(task_id),
        "status": task_status,
        "items_seen": len(MATERIALISED_VIEWS),
        "items_written": len(MATERIALISED_VIEWS),
        "errors": 0,
    }


def test_a_task_that_finds_its_job_locked_succeeds_as_a_skip(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    with _held_elsewhere(committed, "backfill_13f"):
        summary = tasks.ingest_pending_filings()

    assert summary == {"job": "backfill_13f", "skipped": "locked"}
    assert _runs(migrated_engine) == []


@pytest.mark.parametrize(
    ("task", "job_name"),
    [
        (tasks.discover_13f_filings, "discover-filings"),
        (tasks.ingest_pending_filings, "backfill_13f"),
        (tasks.ingest_form4, "ingest-form4"),
        (tasks.resolve_cusips, "resolve-cusips"),
        (tasks.fetch_prices, "fetch-quotes"),
        (tasks.refresh_matviews, "refresh-views"),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_each_task_runs_its_job_against_an_empty_database(
    committed: Settings, migrated_engine: AsyncEngine, task: Any, job_name: str
) -> None:
    """Nothing tracked, queued or held, so none of them asks EDGAR, OpenFIGI
    or a quote provider anything."""
    summary = task()

    [(run_id, job, status, _)] = _runs(migrated_engine)
    assert (summary["run_id"], summary["job"], summary["status"]) == (str(run_id), job, status)
    assert (job, status) == (job_name, "success")


def test_the_daily_crawl_with_no_watermark_fails_and_is_not_retried(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    """Nothing to catch up from is a person's to fix, not a moment to wait out."""
    with pytest.raises(LookupError):
        tasks.crawl_form4_daily()

    [(_, job, status, context)] = _runs(migrated_engine)
    assert (job, status, context["catch_up"]) == ("crawl-form4", "failed", True)


# --- the chain ---------------------------------------------------------------


def test_the_chain_hands_each_run_to_the_next(
    committed: Settings, migrated_engine: AsyncEngine
) -> None:
    """An ingest that published nothing stops the chain there; one that
    published pairs has them rebuilt, and the refresh after names the rebuild."""
    ingested = tasks.ingest_pending_filings()
    assert ingested["published"] == []
    assert tasks.recompute_positions(ingested)["skipped"]

    # As though it had published a pair: one with nothing in it rebuilds to nothing.
    published = {**ingested, "published": [[1, date(2024, 3, 31).isoformat()]]}
    recomputed = tasks.recompute_positions(published)
    refreshed = tasks.refresh_matviews(recomputed)

    runs = {job: (run_id, context) for run_id, job, _, context in _runs(migrated_engine)}
    assert set(runs) == {"backfill_13f", "recompute", "refresh-views"}
    assert runs["recompute"][1] == {
        "filer": None,
        "period": None,
        "pairs": 1,
        "include_suspect": False,
        "after_run_id": ingested["run_id"],
    }
    assert runs["refresh-views"][1]["after_run_id"] == recomputed["run_id"]
    assert uuid.UUID(refreshed["run_id"]) == runs["refresh-views"][0]
