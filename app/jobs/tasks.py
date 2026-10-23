"""Celery task definitions: each a bridge from a message to a job.

One implementation, two entry points. Every task here calls the same
``@tracked_task`` job its CLI verb does, so the run a worker does at 3am is
the run a person does from a shell, and leaves the same ``ingestion_run``
row under the same ``job_name``:

=========================  ==========================  ==============================
task                       CLI verb                    job
=========================  ==========================  ==============================
``discover_13f_filings``   ``discover-filings``        app/jobs/thirteen_f.py
``ingest_pending_filings`` ``backfill``                app/jobs/thirteen_f.py
``crawl_form4_daily``      ``crawl-form4 --catch-up``  app/jobs/form4.py
``ingest_form4``           ``ingest-form4``            app/jobs/form4.py
``resolve_cusips``         ``resolve-cusips``          app/jobs/enrichment.py
``fetch_prices``           ``fetch-quotes``            app/jobs/enrichment.py
``recompute_positions``    ``recompute``               app/jobs/derived.py
``refresh_matviews``       ``refresh-views``           app/jobs/derived.py
=========================  ==========================  ==============================

What a task adds is only what a message needs: arguments that are JSON
(dates as ISO strings), the bridge from Celery's sync world into
``asyncio.run``, a JSON summary as its result, and the retry. The job brings
the rest — its lock, its row, its log context — so neither entry point can
forget them.

A run that finds its job locked by another returns ``{"skipped": "locked"}``
and succeeds: an overlapping run is a non-event. A run that fails transiently
(app/jobs/retry.py) is retried with exponential backoff; any other failure
fails the task the first time.

Chains
------
:func:`thirteen_f_chain` is ingest, then recompute, then refresh, each link
given the result of the one before: the recompute rebuilds the pairs the
ingest published, and the refresh names the recompute's run as the one it
follows. A link whose predecessor did nothing does nothing too, and says so.
:func:`thirteen_f_poll` puts discovery in front of that, and
:func:`form4_chain` is the daily crawl and then its ingest.

Beat sends a task, not a chain, so :func:`poll_13f` and :func:`daily_form4`
apply them (app/jobs/schedule.py). They are not jobs: no lock and no row of
their own, since each link takes its own job's lock and writes its own row.

The exception is :func:`ping`, which checks the broker and the worker and
nothing else: a row every five minutes from the heartbeat would bury the runs
worth reading, and a ping that needed Postgres would fail for the wrong reason.

Found by autodiscovery (app/jobs/celery_app.py), never imported by hand, and
``shared_task`` so that a task binds to whichever app is current.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime
from typing import Any

from celery import Task, chain, shared_task

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.periods import parse_period
from app.derived.scope import Pair
from app.ingestion.insider_discovery import new_york_today
from app.jobs import derived, enrichment, form4, thirteen_f
from app.jobs.celery_app import COMPUTE_QUEUE, INGESTION_QUEUE
from app.jobs.retry import MAX_RETRIES, is_transient, retry_countdown
from app.jobs.schedule import should_poll_now
from app.jobs.tracking import JobLocked, Ran

logger = get_logger(__name__)

Summary = dict[str, Any]
"""What a task returns, and the next link in a chain is given: JSON."""


def _run[T](
    task: Task[Any, Any],
    job: Callable[..., Coroutine[Any, Any, Ran[T]]],
    *,
    extra: Callable[[T], Summary] | None = None,
    **arguments: Any,
) -> Summary:
    """Run ``job`` with ``arguments`` on a loop of its own; summarise it, skip it or retry it.

    :param extra: What the summary adds from the job's value, for the next link.
    """
    try:
        ran = asyncio.run(job(get_settings(), **arguments))
    except JobLocked as locked:
        return {"job": locked.job_name, "skipped": "locked"}
    except Exception as failure:
        if not is_transient(failure):
            raise
        retries, error = task.request.retries, f"{type(failure).__name__}: {failure}"
        if task.max_retries is not None and retries >= task.max_retries:
            logger.error("task.retries_exhausted", error=error, attempts=retries + 1)
            raise
        countdown = retry_countdown(failure, retries)
        logger.warning("task.retrying", error=error, attempt=retries + 1, countdown=countdown)
        raise task.retry(exc=failure, countdown=countdown) from failure
    return {
        "job": ran.run.job_name,
        "run_id": str(ran.run.id),
        "status": ran.status.value,
        "items_seen": ran.run.items_seen,
        "items_written": ran.run.items_written,
        "errors": len(ran.run.errors),
        **(extra(ran.value) if extra is not None else {}),
    }


def _skipped(job_name: str, reason: str) -> Summary:
    logger.info("task.skipped", job=job_name, reason=reason)
    return {"job": job_name, "skipped": reason}


def _day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


# --- 13F ---------------------------------------------------------------------


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def discover_13f_filings(
    self: Task[Any, Any],
    filer: str | None = None,
    since: str | None = None,
    full_history: bool = False,
) -> Summary:
    """``discover-filings``: queue every tracked filer's 13Fs not yet loaded.

    :param since: ISO date; five years ago when not given.
    """
    cutoff = thirteen_f.discovery_cutoff(_day(since), full_history=full_history, today=date.today())
    return _run(self, thirteen_f.discover_13f_filings, filer=filer, since=cutoff)


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def ingest_pending_filings(
    self: Task[Any, Any],
    filer: str | None = None,
    since: str | None = None,
    concurrency: int = thirteen_f.DEFAULT_CONCURRENCY,
    limit: int | None = None,
    force: bool = False,
) -> Summary:
    """``backfill``: ingest every queued 13F. Returns the pairs it published,
    which is what :func:`recompute_positions` is given in a chain."""
    return _run(
        self,
        thirteen_f.ingest_pending_filings,
        extra=_published,
        filer=filer,
        since=_day(since),
        concurrency=concurrency,
        limit=limit,
        force=force,
    )


def _published(backfilled: thirteen_f.Backfilled) -> Summary:
    return {
        "published": [
            [filer_id, period.isoformat()] for filer_id, period in sorted(backfilled.published)
        ]
    }


# --- Form 4 ------------------------------------------------------------------


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def crawl_form4_daily(self: Task[Any, Any], universe: str | None = None) -> Summary:
    """``crawl-form4 --catch-up``: every day since the watermark, through today."""
    return _run(self, form4.crawl_form4_daily, days=None, universe=universe, today=new_york_today())


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def ingest_form4(
    self: Task[Any, Any],
    concurrency: int = thirteen_f.DEFAULT_CONCURRENCY,
    limit: int | None = None,
    force: bool = False,
) -> Summary:
    """``ingest-form4``: load every queued Form 4, then rebuild the clusters."""
    return _run(self, form4.ingest_form4, concurrency=concurrency, limit=limit, force=force)


# --- enrichment --------------------------------------------------------------


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def resolve_cusips(self: Task[Any, Any], limit: int | None = None, force: bool = False) -> Summary:
    """``resolve-cusips``: unresolved securities to tickers, through OpenFIGI."""
    return _run(self, enrichment.resolve_cusips, limit=limit, force=force)


@shared_task(bind=True, queue=INGESTION_QUEUE, max_retries=MAX_RETRIES)
def fetch_prices(self: Task[Any, Any], tickers: list[str] | None = None) -> Summary:
    """``fetch-quotes``: the latest close of every listed security."""
    return _run(self, enrichment.fetch_prices, tickers=tickers)


# --- publishing --------------------------------------------------------------


@shared_task(bind=True, queue=COMPUTE_QUEUE, max_retries=MAX_RETRIES)
def recompute_positions(
    self: Task[Any, Any],
    upstream: Summary | None = None,
    filer: str | None = None,
    period: str | None = None,
    everything: bool = False,
    include_suspect: bool = False,
) -> Summary:
    """``recompute``: rebuild the derived tables.

    In a chain, ``upstream`` is the ingest's summary, and the rebuild is the
    pairs it published, or nothing if it published none. On its own, say what
    to rebuild: ``filer``, ``period`` (``2026Q1`` or ``2026-03-31``), or
    ``everything``, as the CLI makes a person say ``--all``.
    """
    if upstream is not None:
        if upstream.get("skipped") or not upstream.get("published"):
            return _skipped("recompute", "nothing published upstream")
        pairs: list[Pair] = [
            (int(filer_id), date.fromisoformat(day)) for filer_id, day in upstream["published"]
        ]
        return _run(
            self,
            derived.recompute_positions,
            pairs=pairs,
            include_suspect=include_suspect,
            after_run_id=uuid.UUID(upstream["run_id"]),
        )
    if everything == (filer is not None or period is not None):
        raise ValueError("say what to rebuild: filer, period, or everything, which takes neither")
    return _run(
        self,
        derived.recompute_positions,
        filer=filer,
        period=None if period is None else parse_period(period),
        include_suspect=include_suspect,
    )


@shared_task(bind=True, queue=COMPUTE_QUEUE, max_retries=MAX_RETRIES)
def refresh_matviews(
    self: Task[Any, Any],
    upstream: Summary | None = None,
    view: str | None = None,
    concurrent: bool = True,
) -> Summary:
    """``refresh-views``: refresh the materialised views.

    In a chain, after the run ``upstream`` summarises, unless it was skipped:
    a rebuild that published nothing may still have deleted rows the views
    count, so anything that ran is followed by a refresh.
    """
    if upstream is not None and upstream.get("skipped"):
        return _skipped("refresh-views", "nothing ran upstream")
    return _run(
        self,
        derived.refresh_matviews,
        view=view,
        concurrent=concurrent,
        after_run_id=None if upstream is None else uuid.UUID(upstream["run_id"]),
    )


# --- chains ------------------------------------------------------------------


def thirteen_f_chain(**ingest: Any) -> chain:
    """Ingest the queue, rebuild what it published, refresh the views.

    :param ingest: :func:`ingest_pending_filings`'s arguments.
    """
    return chain(
        ingest_pending_filings.si(**ingest),
        recompute_positions.s(),
        refresh_matviews.s(),
    )


def thirteen_f_poll(**ingest: Any) -> chain:
    """Discover new 13Fs for every tracked filer, then :func:`thirteen_f_chain`.

    The ingest does not depend on what discovery found: the queue may hold
    filings an earlier run failed to load, and this is when they are retried.
    """
    return chain(discover_13f_filings.si(), thirteen_f_chain(**ingest))


def form4_chain() -> chain:
    """Crawl the daily index since the watermark, then load what it queued."""
    return chain(crawl_form4_daily.si(), ingest_form4.si())


# --- beat triggers -----------------------------------------------------------


def _now() -> datetime:
    """The moment a trigger decides by; replaced in tests."""
    return datetime.now(UTC)


def _enqueued(name: str, signature: chain) -> Summary:
    """Apply ``signature`` and return at once, naming where its last link went.

    Never ``.get()`` on the result: waiting on a subtask inside a task can
    deadlock the worker, and Celery refuses it.
    """
    result = signature.apply_async()
    logger.info("task.chain_enqueued", chain=name, chain_task_id=result.id)
    return {"chain": name, "task_id": result.id}


@shared_task(bind=True, queue=COMPUTE_QUEUE)
def poll_13f(self: Task[Any, Any]) -> Summary:
    """Beat's ``poll-13f``: :func:`thirteen_f_poll`, when
    :func:`~app.jobs.schedule.should_poll_now` says so.

    Sent every half hour; polls each time in a filing window and once a day
    outside one, and otherwise returns having enqueued nothing.
    """
    if not should_poll_now(_now()):
        return {"chain": "13f-poll", "skipped": "outside the filing window"}
    return _enqueued("13f-poll", thirteen_f_poll())


@shared_task(bind=True, queue=COMPUTE_QUEUE)
def daily_form4(self: Task[Any, Any]) -> Summary:
    """Beat's ``daily-form4``: :func:`form4_chain`, the crawl since the
    watermark and then its ingest."""
    return _enqueued("form4", form4_chain())


# --- health ------------------------------------------------------------------


@shared_task(bind=True, queue=COMPUTE_QUEUE)
def ping(self: Task[Any, Any]) -> dict[str, object]:
    """The round trip: a caller sends it, a worker runs it, the caller reads
    the result back from the result backend.

    Through ``asyncio.run`` like every task above, so a pong also says the
    bridge from Celery's sync world to the pipeline's async one works in this
    worker.
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
