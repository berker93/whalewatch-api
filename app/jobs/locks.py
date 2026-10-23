"""One run of a job at a time, across every process that can start one.

Beat fires on the clock whether or not the last run has finished, a person can
start the same verb from a shell while the worker is running it, and a task
redelivered after its worker died can land next to the copy that did not. Two
backfills draining one queue would each fetch the same filings; two refreshes
would each wait on the other's locks. So every job holds a Postgres advisory
lock named after it for as long as it runs::

    SELECT pg_try_advisory_lock(hashtext('job:backfill_13f'))

Postgres is the one thing every process already shares, so this needs nothing
else running. *Try*, not wait: a run that finds the lock taken has nothing to
do that the run holding it is not already doing, and it exits cleanly
(:class:`~app.jobs.tracking.JobLocked`) rather than queueing behind it.

Session-level, on a connection of its own
-----------------------------------------
The lock is held on a connection opened for it, in autocommit, so it is never
"idle in transaction" for the half hour a backfill can take, and is never the
job's own connection, which commits and rolls back as the job goes. The lock
belongs to the connection, so a process that dies without releasing it —
``SIGKILL``, the OOM killer, a pulled cable — releases it anyway when Postgres
notices the connection has gone. There is no stale lock to clear by hand.

The keys share the one-``bigint`` key space with ``recompute``'s transaction
lock (app/derived/recompute.py), and cannot meet it: ``hashtext`` is an
``int4``, so every job's key is within 2^31 of zero, and ``wwrecomp`` is
nowhere near.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import create_engine

logger = get_logger(__name__)


def lock_key(job_name: str) -> str:
    """What ``hashtext`` is given: ``job:`` and the job's name."""
    return f"job:{job_name}"


@asynccontextmanager
async def job_lock(settings: Settings, job_name: str) -> AsyncIterator[bool]:
    """Try for ``job_name``'s lock and hold it for the block: whether it was got.

    Never waits. ``False`` means another run holds it, and the block should do
    nothing. Released on the way out however the block ends; if the release
    itself fails, the connection is closed regardless, which releases it too.

    :raises: Whatever connecting raises: a run that cannot reach the database
        to take its lock could not have done its work either.
    """
    key = lock_key(job_name)
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            held = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key}
                )
            )
            try:
                yield held
            finally:
                if held:
                    await _release(connection, key)
    finally:
        await engine.dispose()


async def _release(connection: AsyncConnection, key: str) -> None:
    """Best effort, like ``track_run``'s last write: the job's outcome is what
    the caller is waiting for, and a dropped connection has released the lock
    already."""
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key})
    except Exception as unreleased:
        logger.warning("job.unlock_failed", lock=key, error=str(unreleased))
