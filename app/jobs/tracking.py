"""Every job records its own run, so "did the backfill finish?" is a SQL query.

The rule is simple and has no exceptions: a job's body runs inside
:func:`track_run`. That writes an ``ingestion_run`` row as the job starts,
binds the row's id into the logging context as ``run_id`` for the duration,
and updates the row as the job ends — ``success``, ``partial`` or ``failed``,
with its counters and whatever went wrong::

    async with track_run(settings, "backfill_13f", filer=slug, force=force) as run:
        run.items_seen = len(plan.work)
        ...
        run.items_written += 1
        run.errors.append(f"{accession_no}: {describe(failure)}")

Any line in :attr:`TrackedRun.errors` makes the run ``partial``; an exception
out of the block makes it ``failed`` and is re-raised untouched. Anything else
the job measured goes in :attr:`TrackedRun.metrics`, which is written with the
outcome. The query, the
row and the log then agree::

    SELECT status, items_seen, items_written, error
    FROM ingestion_run WHERE job_name = 'backfill_13f'
    ORDER BY started_at DESC LIMIT 1;

Its own sessions
----------------
The row is written through :func:`~app.db.session.session_scope`, never the
job's session. A job that fails mid-transaction rolls back, and a run record
written in that transaction would roll back with it, leaving the one run worth
asking about as the one with no row. The two short sessions also mean nothing
is held open across the run itself, which can sit for ten minutes waiting out an
EDGAR block.

``BaseException``, not ``Exception``
------------------------------------
A second Ctrl-C reaches the job as :class:`asyncio.CancelledError`, which is not
an ``Exception``. Catching only those would leave an abandoned backfill saying
``running`` forever, which is the wrong answer to the question this module
exists to answer. Nothing can be done about ``SIGKILL``: an old ``running`` row
is a process that died without the chance to say so.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, insert, update

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.ingestion_run import IngestionRun, RunStatus
from app.db.session import session_scope

logger = get_logger(__name__)


@dataclass(slots=True)
class TrackedRun:
    """What a job counts while it runs. Written to its row when the block exits."""

    id: uuid.UUID
    """The row's id, and the ``run_id`` on every log line inside the block."""

    job_name: str

    items_seen: int = 0
    """What the job took on, in its own unit."""

    items_written: int = 0
    """How many of those it wrote."""

    errors: list[str] = field(default_factory=list)
    """One line per item that went wrong. Any at all make the run ``partial``."""

    metrics: dict[str, Any] = field(default_factory=dict)
    """What the run measured beyond the counters, such as how long each view
    took to refresh. Stored as ``jsonb``, as ``context`` is."""


@asynccontextmanager
async def track_run(settings: Settings, job_name: str, **context: Any) -> AsyncIterator[TrackedRun]:
    """Record one run of ``job_name`` in ``ingestion_run``, from start to finish.

    :param settings: Where the database is. The row is written through sessions
        of its own, never the job's; see the module docstring.
    :param job_name: The ``job_name`` the logs use: the CLI verb, or
        ``backfill_13f``.
    :param context: The run's parameters, stored as ``jsonb``. Anything JSON
        has no type for (a date, a path) is stored as its ``str()``.
    :raises: Whatever the block raises, after it is recorded. Also whatever
        writing the starting row raises: a job whose run cannot be recorded
        does not start.
    """
    run = TrackedRun(id=uuid.uuid4(), job_name=job_name)
    parameters = _jsonable(context)

    # Bound before the first write, so a failure to write the row is logged
    # under the id the row would have had.
    with structlog.contextvars.bound_contextvars(run_id=str(run.id), job_name=job_name):
        async with session_scope(settings) as session:
            await session.execute(
                insert(IngestionRun).values(id=run.id, job_name=job_name, context=parameters)
            )
        logger.info("ingestion_run.started", context=parameters)

        try:
            yield run
        except BaseException as failure:
            # The failure first: it is why the run stopped, and the first line
            # is the one a listing shows.
            await _finish(settings, run, RunStatus.FAILED, [_describe(failure), *run.errors])
            raise
        await _finish(
            settings, run, RunStatus.PARTIAL if run.errors else RunStatus.SUCCESS, run.errors
        )


async def _finish(
    settings: Settings, run: TrackedRun, status: RunStatus, errors: list[str]
) -> None:
    """Write the run's outcome to its row, then log it.

    Best effort, like the queue's own failure records. The likeliest reason
    this write fails is that the database has gone, which is often why the job
    failed too, and raising from here would replace the job's own error with a
    second-hand one. The row is left ``running``, and the warning carries the
    ``run_id`` that says which.
    """
    try:
        async with session_scope(settings) as session:
            await session.execute(
                update(IngestionRun)
                .where(IngestionRun.id == run.id)
                .values(
                    status=status.value,
                    finished_at=func.now(),
                    items_seen=run.items_seen,
                    items_written=run.items_written,
                    error="\n".join(errors) or None,
                    metrics=_jsonable(run.metrics),
                )
            )
    except Exception as unrecorded:
        logger.warning("ingestion_run.record_failed", status=status.value, error=str(unrecorded))

    log = {
        RunStatus.SUCCESS: logger.info,
        RunStatus.PARTIAL: logger.warning,
        RunStatus.FAILED: logger.error,
    }[status]
    log(
        "ingestion_run.finished",
        status=status.value,
        items_seen=run.items_seen,
        items_written=run.items_written,
        errors=len(errors),
    )


def _describe(failure: BaseException) -> str:
    """``ExceptionType: message``, as the queue rows say it, or just the type
    when there is no message — which is how a cancellation arrives."""
    message = str(failure)
    return f"{type(failure).__name__}: {message}" if message else type(failure).__name__


def _jsonable(context: dict[str, Any]) -> dict[str, Any]:
    """``context`` or ``metrics`` as ``jsonb`` will take it: dates, paths and
    UUIDs as strings."""
    parameters: dict[str, Any] = json.loads(json.dumps(context, default=str))
    return parameters
