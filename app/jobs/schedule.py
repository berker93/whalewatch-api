"""Celery beat schedule.

What runs when nobody is watching. Crontabs here are New York times: the app's
``timezone`` (app/jobs/celery_app.py) is EDGAR's.

Only the heartbeat so far. The tasks for the jobs in
docs/ingestion-spec.md#orchestration exist (app/jobs/tasks.py), and so do their
chains — the daily Form 4 crawl, the filing-season 13F poll — but nothing sends
them on a clock yet. Beat sends a task, not a chain, so each chain will want a
small task of its own that applies it.
Each entry names its task by its registered name, a string, so this module
imports no task and the schedule cannot be the reason a task module was loaded.
"""

from __future__ import annotations

from typing import Any, Final

BEAT_SCHEDULE: Final[dict[str, dict[str, Any]]] = {
    # Proves beat, the broker and the compute worker are all up, from the
    # outside: Flower shows one every five minutes succeeding, or it does not.
    "heartbeat": {
        "task": "app.jobs.tasks.ping",
        "schedule": 5 * 60.0,
        # A ping nobody consumed is stale before the next one is sent; it
        # should not pile up behind a stopped worker.
        "options": {"expires": 5 * 60.0},
    },
}
