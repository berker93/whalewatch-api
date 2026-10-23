"""The Celery application: Redis broker and result backend, two queues.

Ingestion has to run on a schedule and survive a process restart, which is what
this is for. A worker is started from the module attribute::

    celery -A app.jobs.celery_app:celery_app worker -Q ingestion --concurrency 1
    celery -A app.jobs.celery_app:celery_app worker -Q compute
    celery -A app.jobs.celery_app:celery_app beat

and those are the ``worker-ingestion``, ``worker-compute`` and ``beat`` services
in docker-compose.yml.

Tasks are sync, the pipeline is async
-------------------------------------
Celery has no event loop and is not going to grow one. A task's body bridges
with ``asyncio.run``, one loop per task, which is what
:func:`~app.db.session.session_scope` and the EDGAR limiter are already built
around::

    @shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=3)
    def ingest_filing_task(self: Task, accession_no: str) -> dict[str, object]:
        return asyncio.run(_ingest_filing(accession_no))

Redelivered, not lost
---------------------
``task_acks_late`` acknowledges a message when its task *finishes*, not when a
worker takes it, so a worker that dies mid-filing leaves the message unacked and
Redis hands it out again once ``visibility_timeout`` has passed. Prefetch of one
means a worker holds no message it has not started, so a dead worker strands one
task, not a prefetched handful. ``task_reject_on_worker_lost`` covers the case
``acks_late`` alone does not: a pool child killed outright (the OOM killer,
``kill -9``) is otherwise acknowledged as failed, which is lost by another name.

All of that is at-least-once delivery, and it is only safe because the loaders
are idempotent: a filing is keyed on its accession number, and loading it twice
is loading it once (app/ingestion/loaders/filing.py). A task that is not
idempotent does not belong on this app until it is.

The cost is that a task which reliably kills its worker is redelivered for ever.
A task that *raises*, or overruns its hard time limit, is acknowledged as failed
and not retried unless it asks to be.

Two queues
----------
``ingestion`` is long work against EDGAR: a backfill, a day of Form 4s. Its
worker runs one task at a time, because the EDGAR limiter is per process
(app/core/rate_limit.py) and two pool children would be two 8/s budgets against
one IP. A backfill's own concurrency is inside the task, on one event loop.
``compute`` is short work against Postgres: recomputes, view refreshes, the
ping. Separate workers mean a 30-minute backfill never queues a refresh behind
it. A task names its queue in its decorator; one that does not lands on
``compute``, the queue whose time limit it is least likely to need.
"""

from __future__ import annotations

import pkgutil
from typing import Any, Final

import structlog
from celery import Celery, Task
from celery.signals import setup_logging, task_postrun, task_prerun
from kombu import Exchange, Queue

import app
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.jobs.schedule import BEAT_SCHEDULE

INGESTION_QUEUE: Final = "ingestion"
COMPUTE_QUEUE: Final = "compute"

#: The soft limit raises ``SoftTimeLimitExceeded`` inside the task, which
#: ``track_run`` records as the run's failure. The hard limit is for a task
#: that swallowed it: the pool child is killed and replaced.
SOFT_TIME_LIMIT_SECONDS: Final = 30 * 60
HARD_TIME_LIMIT_SECONDS: Final = 35 * 60

#: How long Redis waits for an unacknowledged message before handing it to
#: another worker. It has to be longer than any task can run, or a task still
#: running is delivered a second time; a task cannot outlive the hard limit, so
#: an hour has 25 minutes of margin. It is also how long a task stranded by a
#: dead worker waits to be redelivered.
VISIBILITY_TIMEOUT_SECONDS: Final = 60 * 60


def create_celery(settings: Settings) -> Celery:
    """Build the Celery app for ``settings``: broker, backend, queues, limits.

    A factory for the reason :func:`app.main.create_app` is one: a test builds
    its own from its own settings. Tasks are ``shared_task``, so they bind to
    whichever app is current rather than to the module-level one below.
    """
    celery = Celery(
        "whalewatch",
        broker=settings.celery_redis_url,
        backend=settings.celery_redis_url,
    )
    celery.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=HARD_TIME_LIMIT_SECONDS,
        broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
        task_queues=tuple(_queue(name) for name in (INGESTION_QUEUE, COMPUTE_QUEUE)),
        task_default_queue=COMPUTE_QUEUE,
        # Off: a typo'd queue name would otherwise be created, and sit there
        # filling with tasks no worker consumes.
        task_create_missing_queues=False,
        # JSON only, both ways. Pickle would accept a task argument that runs
        # code when it is read, from anyone who can write to the broker.
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Long enough to read a result the next morning; results are never the
        # record of a run, ingestion_run is.
        result_expires=24 * 60 * 60,
        # "started" in Flower and in AsyncResult.state, rather than "pending"
        # for the whole half hour a backfill takes.
        task_track_started=True,
        # The beat schedule's crontabs are New York times: EDGAR's day.
        timezone="America/New_York",
        beat_schedule=BEAT_SCHEDULE,
        # Celery 6 stops retrying the broker connection at startup unless asked;
        # a worker that starts before Redis is healthy should wait for it.
        broker_connection_retry_on_startup=True,
    )
    # A `tasks` module in any package under app/ registers itself; nothing has
    # to import it. Lazy: the packages are imported when the worker starts, not
    # when this module is.
    celery.autodiscover_tasks(_task_packages)
    return celery


def _queue(name: str) -> Queue:
    """A queue with an exchange and routing key of its own name.

    Spelled out because a bare ``Queue(name)`` is bound to the *default*
    queue's exchange and key, so both queues would share the binding
    ``compute -> compute``, and anything published to that exchange by key
    would be copied into both: one task run once by each worker. Celery's own
    sends happen to go by queue name and miss it; a caller passing ``exchange``
    or ``routing_key`` would not.
    """
    return Queue(name, Exchange(name, type="direct"), routing_key=name)


def _task_packages() -> list[str]:
    """Every top-level package under ``app``, for autodiscovery to look in."""
    return [f"app.{module.name}" for module in pkgutil.iter_modules(app.__path__) if module.ispkg]


@setup_logging.connect
def _configure_logging(**_: Any) -> None:
    """Route Celery's logging through ours.

    Connecting anything to this signal stops Celery installing its own
    handlers, so worker and beat lines arrive as the same structured events as
    the API's, through :func:`~app.core.logging.configure_logging`.
    """
    configure_logging(get_settings())


@task_prerun.connect
def _bind_task(task_id: str, task: Task[Any, Any], **_: Any) -> None:
    """Put ``task_id`` and ``task_name`` on every line the task logs, as
    ``request_id`` is on every line a request does. ``asyncio.run`` copies the
    context into its loop, so the coroutines inside carry them too."""
    structlog.contextvars.bind_contextvars(task_id=task_id, task_name=task.name)


@task_postrun.connect
def _unbind_task(**_: Any) -> None:
    structlog.contextvars.unbind_contextvars("task_id", "task_name")


celery_app = create_celery(get_settings())
