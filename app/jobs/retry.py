"""Which failures a task retries, and how long it waits first.

A task is retried when the thing it depends on had a bad moment, and never
when the task itself is wrong. Running a parser bug again three times produces
the same traceback three times and a backlog behind it; running a backfill
again after Postgres restarted finishes the backfill. So the retry is decided
by the exception, with no catch-all: what is not named here fails the run the
first time, with its traceback, for a person to read.

Transient
---------
- **The network.** ``httpx.TransportError`` (a refused connect, a read
  timeout, a dropped connection), and the bare ``ConnectionError`` and
  ``TimeoutError`` a socket raises below httpx and asyncpg.
- **A source saying "later".** :class:`EdgarServerError` and
  :class:`OpenFIGIServerError` (5xx), :class:`OpenFIGIRateLimited`, and
  :class:`EdgarRateLimited` for a 429. Each has had its client's own retries
  already; this is the slower loop around them.
- **Postgres saying "later".** A connection SQLAlchemy found dead, a pool
  checkout that timed out, and the SQLSTATEs below: a lost connection, a
  serialisation failure or deadlock, a server out of connections or shutting
  down, a lock not granted in time.

Not transient
-------------
Everything else, and two that look transient on purpose: a **403** from EDGAR
is a User-Agent the SEC has blocked, and retrying it "makes it permanent"
(docs/ingestion-spec.md), so :class:`EdgarRateLimited` for a 403 fails the
run. And Celery's ``SoftTimeLimitExceeded``: a job that took half an hour once
will take it again.

Backoff
-------
Exponential with full jitter, from :data:`BACKOFF_FACTOR_SECONDS`, capped at
:data:`BACKOFF_MAX_SECONDS`, for :data:`MAX_RETRIES` retries: a minute, two,
four, each drawn at random below its ceiling so that tasks failing together do
not come back together. Never sooner than a source asked for, when it said.
Every attempt is a run of its own, with a row of its own, so a retried job's
history shows each failure that led to the success.
"""

from __future__ import annotations

from typing import Final

import httpx
from celery.utils.time import get_exponential_backoff_interval
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.ingestion.edgar.client import EdgarRateLimited, EdgarServerError
from app.ingestion.enrichment.figi import OpenFIGIRateLimited, OpenFIGIServerError

MAX_RETRIES: Final = 3
BACKOFF_FACTOR_SECONDS: Final = 60
#: A quarter of an hour: past EDGAR's ten-minute block, and well inside the
#: hour's visibility timeout (app/jobs/celery_app.py), so a retry waiting out
#: its countdown is never redelivered as if lost.
BACKOFF_MAX_SECONDS: Final = 15 * 60

#: Postgres SQLSTATEs that are the server's moment, not the statement's fault.
#: Class 08 is every connection exception.
_TRANSIENT_SQLSTATES: Final = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53000",  # insufficient_resources
        "53300",  # too_many_connections
        "55P03",  # lock_not_available
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)

_TRANSIENT_TYPES: Final = (
    httpx.TransportError,
    ConnectionError,
    TimeoutError,
    PoolTimeoutError,
    EdgarServerError,
    OpenFIGIServerError,
    OpenFIGIRateLimited,
)


def is_transient(failure: BaseException) -> bool:
    """Whether running the job again, later, could go differently."""
    if isinstance(failure, EdgarRateLimited):
        return failure.status_code == 429
    if isinstance(failure, _TRANSIENT_TYPES):
        return True
    if isinstance(failure, DBAPIError):
        if failure.connection_invalidated:
            return True
        sqlstate = getattr(failure.orig, "sqlstate", None)
        return isinstance(sqlstate, str) and (
            sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES
        )
    return False


def retry_countdown(failure: BaseException, retries: int) -> int:
    """Seconds before retry number ``retries + 1``.

    :param retries: How many times the task has been retried already: Celery's
        ``request.retries``, ``0`` on the first failure.
    """
    countdown = get_exponential_backoff_interval(
        BACKOFF_FACTOR_SECONDS, retries, BACKOFF_MAX_SECONDS, full_jitter=True
    )
    asked = getattr(failure, "retry_after", None)
    if isinstance(asked, int | float):
        countdown = max(countdown, min(int(asked), BACKOFF_MAX_SECONDS))
    return countdown
