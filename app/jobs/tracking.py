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

Jobs: ``@tracked_task``
-----------------------
A job that runs on a schedule as well as by hand is an ``async`` function
decorated with :func:`tracked_task`, and both its entry points — the CLI verb
and the Celery task (app/jobs/tasks.py) — call the decorated function. The
decorator is everything the two must not each remember: the job's lock
(app/jobs/locks.py), its ``track_run``, and the ``job_name`` on every line::

    @tracked_task("discover-filings")
    async def discover_13f_filings(
        run: TrackedRun, settings: Settings, *, filer: str | None
    ) -> list[FilerDiscovery]: ...

    ran = await discover_13f_filings(settings, filer="berkshire-hathaway")
    ran.run.id, ran.value

The body is handed its ``TrackedRun`` and counts into it; the caller gets it
back with the body's value. A run that finds the lock taken never starts, and
leaves no row: :class:`JobLocked` says so, and both entry points treat it as
the non-event it is.
"""

from __future__ import annotations

import functools
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Concatenate

import structlog
from sqlalchemy import func, insert, update

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.ingestion_run import IngestionRun, RunStatus
from app.db.session import session_scope
from app.jobs.locks import job_lock

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


class JobLocked(Exception):  # noqa: N818
    """Another run of the job holds its lock, so this one did nothing.

    Not a failure, and nothing is recorded: the run that holds the lock is
    doing what this one would have done, and has a row of its own saying so.
    """

    def __init__(self, job_name: str) -> None:
        super().__init__(f"another {job_name} run is in progress; this one did nothing")
        self.job_name = job_name


@dataclass(frozen=True, slots=True)
class Ran[T]:
    """A finished run of a job: what it counted, and what its body returned."""

    run: TrackedRun
    value: T

    @property
    def status(self) -> RunStatus:
        """What its row says. A run that raised has no :class:`Ran` at all."""
        return RunStatus.PARTIAL if self.run.errors else RunStatus.SUCCESS


Context = Callable[[Settings, dict[str, Any]], dict[str, Any]]
"""The settings and the job's keyword arguments in, the row's ``context`` out."""


def tracked_task[**P, T](
    job_name: str, *, context: Context | None = None
) -> Callable[
    [Callable[Concatenate[TrackedRun, Settings, P], Awaitable[T]]],
    Callable[Concatenate[Settings, P], Coroutine[Any, Any, Ran[T]]],
]:
    """Make an ``async`` function a job: locked, tracked, and named in the log.

    The decorated function takes ``settings`` and the body's keyword
    arguments, and in order:

    1. Binds ``job_name`` into the logging context, so that even a run that
       goes no further says which job it was.
    2. Tries for the job's advisory lock, and raises :class:`JobLocked`
       without a row if another run has it.
    3. Runs the body inside :func:`track_run`, which writes the row, binds
       ``run_id`` and records the outcome, an exception included.

    :param job_name: The row's ``job_name`` and the lock's name: the CLI verb,
        or ``backfill_13f``, as before there were tasks, so that ``runs --job``
        reads one history whichever entry point started each run.
    :param context: Turns the keyword arguments into the row's ``context``,
        for a job that takes something that is not a parameter worth storing
        (a progress printer), is too long to store whole, or defaults to a
        setting the row should name. By default every keyword argument, as
        given.
    """
    to_context = context or _arguments

    def decorate(
        body: Callable[Concatenate[TrackedRun, Settings, P], Awaitable[T]],
    ) -> Callable[Concatenate[Settings, P], Coroutine[Any, Any, Ran[T]]]:
        # So that the row has every parameter, the ones left at their default
        # included, and two runs with the same parameters have the same context.
        defaults = {
            name: parameter.default
            for name, parameter in inspect.signature(body).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            and parameter.default is not inspect.Parameter.empty
        }

        @functools.wraps(body)
        async def job(settings: Settings, /, *args: P.args, **kwargs: P.kwargs) -> Ran[T]:
            arguments = {**defaults, **kwargs}
            with structlog.contextvars.bound_contextvars(job_name=job_name):
                async with job_lock(settings, job_name) as held:
                    if not held:
                        logger.info("job.skipped", reason="locked")
                        raise JobLocked(job_name)
                    async with track_run(
                        settings, job_name, **to_context(settings, arguments)
                    ) as run:
                        value = await body(run, settings, *args, **kwargs)
            return Ran(run=run, value=value)

        return job

    return decorate


def _arguments(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


def without_progress(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """Every keyword argument but ``progress``, which is a printer, not a parameter."""
    return {name: value for name, value in arguments.items() if name != "progress"}
