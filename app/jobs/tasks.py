"""Celery task definitions.

Each task's body runs inside :func:`app.jobs.tracking.track_run`, exactly as
each CLI verb's does, so a scheduled run leaves the same ``ingestion_run`` row
as one started by hand. The exception is :func:`ping`, which checks the broker
and the worker and nothing else: a row a minute from the heartbeat would bury
the runs worth reading, and a ping that needed Postgres would fail for the
wrong reason.

Found by autodiscovery (app/jobs/celery_app.py), never imported by hand, and
``shared_task`` so that a task binds to whichever app is current.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from celery import Task, shared_task

from app.jobs.celery_app import COMPUTE_QUEUE


@shared_task(bind=True, queue=COMPUTE_QUEUE)
def ping(self: Task[Any, Any]) -> dict[str, object]:
    """The round trip: a caller sends it, a worker runs it, the caller reads
    the result back from the result backend.

    Through ``asyncio.run`` like every task that will follow it, so a pong also
    says the bridge from Celery's sync world to the pipeline's async one works
    in this worker.
    """
    return asyncio.run(_pong(self.request.hostname))


async def _pong(hostname: str | None) -> dict[str, object]:
    await asyncio.sleep(0)
    return {
        "pong": True,
        "worker": hostname,
        "pid": os.getpid(),
        "at": datetime.now(UTC).isoformat(),
    }
