"""``track_run``: one ``ingestion_run`` row per run, true however the run ends.

Against a real Postgres, because what is being asserted is what a second
connection can see — the row while the run is going, and the row after the
job's own transaction has rolled back — and a test session that shares the
job's connection would see neither. ``track_run`` commits for real, so the
table is truncated around each test rather than rolled back.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.db.models import Filer, IngestionRun
from app.db.session import session_scope
from app.jobs.tracking import track_run

_COLUMNS = (
    IngestionRun.id,
    IngestionRun.job_name,
    IngestionRun.status,
    IngestionRun.items_seen,
    IngestionRun.items_written,
    IngestionRun.error,
)


@pytest.fixture(autouse=True)
async def clean_tables(migrated_engine: AsyncEngine) -> AsyncIterator[None]:
    await _truncate(migrated_engine)
    yield
    await _truncate(migrated_engine)


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    structlog.contextvars.clear_contextvars()


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE ingestion_run, filer_cik, filer RESTART IDENTITY CASCADE")
        )


async def _rows(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    """Every run, as a connection other than the job's sees it."""
    async with engine.connect() as connection:
        return [tuple(row) for row in await connection.execute(select(*_COLUMNS))]


async def _finished(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        return bool(await connection.scalar(select(IngestionRun.finished_at.is_not(None))))


# --- how a run ends ----------------------------------------------------------


async def test_the_row_says_running_while_the_job_runs(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """Committed before the body starts, so a run that is going is visible as
    one, from anywhere."""
    async with track_run(settings, "test-job") as run:
        assert await _rows(migrated_engine) == [(run.id, "test-job", "running", 0, 0, None)]
        assert not await _finished(migrated_engine)


async def test_a_clean_run_is_a_success_with_its_counters(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    async with track_run(settings, "test-job") as run:
        run.items_seen = 3
        run.items_written = 2

    assert await _rows(migrated_engine) == [(run.id, "test-job", "success", 3, 2, None)]
    assert await _finished(migrated_engine)


async def test_a_run_with_item_errors_is_partial_and_lists_them(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    async with track_run(settings, "test-job") as run:
        run.items_seen = 3
        run.items_written = 1
        run.errors.append("0001067983-24-000011: FilingParseError: no <infoTable>")
        run.errors.append("0001067983-24-000022: EdgarServerError: 503")

    assert await _rows(migrated_engine) == [
        (
            run.id,
            "test-job",
            "partial",
            3,
            1,
            "0001067983-24-000011: FilingParseError: no <infoTable>\n"
            "0001067983-24-000022: EdgarServerError: 503",
        )
    ]


async def test_a_run_that_raises_is_failed_and_the_exception_is_not_swallowed(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """The failure first, then whatever items had already gone wrong, and the
    counters as they stood when it stopped."""
    failure = RuntimeError("EDGAR is blocking us")

    with pytest.raises(RuntimeError) as raised:
        async with track_run(settings, "test-job") as run:
            run.items_seen = 5
            run.items_written = 2
            run.errors.append("CIK 0001336528: not found")
            raise failure

    assert raised.value is failure
    assert await _rows(migrated_engine) == [
        (
            run.id,
            "test-job",
            "failed",
            5,
            2,
            "RuntimeError: EDGAR is blocking us\nCIK 0001336528: not found",
        )
    ]
    assert await _finished(migrated_engine)


async def test_the_record_outlives_the_transaction_it_records(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """The reason the row has sessions of its own. The job's write rolls back
    with the job; the account of why it did must not."""
    with pytest.raises(RuntimeError):
        async with track_run(settings, "test-job"), session_scope(settings) as session:
            session.add(Filer(name="Rolled Back", slug="rolled-back"))
            await session.flush()
            raise RuntimeError("the load failed mid-transaction")

    async with migrated_engine.connect() as connection:
        assert (await connection.execute(select(Filer.slug))).all() == []
    [(_, _, status, _, _, error)] = await _rows(migrated_engine)
    assert (status, error) == ("failed", "RuntimeError: the load failed mid-transaction")


async def test_a_cancelled_run_is_failed_rather_than_left_running(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """What a second Ctrl-C does to a backfill. ``CancelledError`` is not an
    ``Exception``; catching only those would leave the row ``running`` for good."""
    started = asyncio.Event()

    async def job() -> None:
        async with track_run(settings, "test-job"):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [(_, _, status, _, _, error)] = await _rows(migrated_engine)
    assert (status, error) == ("failed", "CancelledError")


async def test_a_failure_to_record_the_end_does_not_hide_the_jobs_own_error(
    settings: Settings, migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most likely the database has gone, which is why the job failed. The job's
    error is the one worth seeing; the row stays ``running``."""

    @asynccontextmanager
    async def unreachable(_: Settings) -> AsyncIterator[AsyncSession]:
        raise OSError("connection refused")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="the job's own error"):
        async with track_run(settings, "test-job"):
            monkeypatch.setattr("app.jobs.tracking.session_scope", unreachable)
            raise RuntimeError("the job's own error")

    [(_, _, status, _, _, _)] = await _rows(migrated_engine)
    assert status == "running"


# --- what it records ---------------------------------------------------------


async def test_the_parameters_are_stored_as_jsonb(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    """A date and a path as strings, ``None`` as null: queryable with ``@>``."""
    async with track_run(
        settings,
        "test-job",
        filer=None,
        since=date(2024, 1, 1),
        file=Path("data/investors.yaml"),
        limit=5,
        force=True,
    ) as run:
        pass

    async with migrated_engine.connect() as connection:
        context = await connection.scalar(
            select(IngestionRun.context).where(IngestionRun.id == run.id)
        )
        forced = await connection.scalar(
            select(IngestionRun.id).where(IngestionRun.context.contains({"force": True}))
        )
    assert context == {
        "filer": None,
        "since": "2024-01-01",
        "file": "data/investors.yaml",
        "limit": 5,
        "force": True,
    }
    assert forced == run.id


async def test_run_id_is_bound_for_the_run_and_only_the_run(settings: Settings) -> None:
    """Every line logged inside carries the row's id. Afterwards the binding
    is gone, and whatever was bound before is as it was."""
    structlog.contextvars.bind_contextvars(accession_no="0001067983-24-000011")

    async with track_run(settings, "test-job") as run:
        inside = structlog.contextvars.get_contextvars()

    assert inside == {
        "accession_no": "0001067983-24-000011",
        "run_id": str(run.id),
        "job_name": "test-job",
    }
    assert structlog.contextvars.get_contextvars() == {"accession_no": "0001067983-24-000011"}


async def test_each_run_gets_its_own_id(settings: Settings, migrated_engine: AsyncEngine) -> None:
    async with track_run(settings, "test-job") as first:
        pass
    async with track_run(settings, "test-job") as second:
        pass

    assert first.id != second.id
    assert {row[0] for row in await _rows(migrated_engine)} == {first.id, second.id}


# --- the table's own rules ---------------------------------------------------


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"status": "done", "finished_at": text("now()")}, "ck_ingestion_run_status_is_known"),
        ({"status": "success"}, "ck_ingestion_run_finished_when_not_running"),
        (
            {"status": "running", "finished_at": text("now()")},
            "ck_ingestion_run_finished_when_not_running",
        ),
        (
            {"status": "partial", "finished_at": text("now()")},
            "ck_ingestion_run_an_unsuccessful_run_says_why",
        ),
        (
            {"status": "failed", "finished_at": text("now()")},
            "ck_ingestion_run_an_unsuccessful_run_says_why",
        ),
        ({"items_seen": -1}, "ck_ingestion_run_items_seen_is_not_negative"),
        ({"items_written": -1}, "ck_ingestion_run_items_written_is_not_negative"),
    ],
)
async def test_a_row_that_contradicts_itself_is_rejected(
    db_session: AsyncSession, columns: dict[str, Any], constraint: str
) -> None:
    """A finished run has a finish time and a running one does not; a run that
    did not succeed says why. Otherwise the table answers its one question with
    a row that has to be second-guessed."""
    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            db_session.add(IngestionRun(id=uuid.uuid4(), job_name="test-job", **columns))
            await db_session.flush()


async def test_the_server_fills_in_a_row_inserted_by_hand(db_session: AsyncSession) -> None:
    run = IngestionRun(job_name="by-hand")
    db_session.add(run)
    await db_session.flush()
    await db_session.refresh(run)

    assert isinstance(run.id, uuid.UUID)
    assert (run.status, run.items_seen, run.items_written, run.context) == ("running", 0, 0, {})
    assert run.started_at is not None
    assert run.finished_at is None
