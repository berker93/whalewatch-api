"""The tasks without a broker or a database: which failures retry, how long
they wait, which queue each task is on, and how the chains are linked.

The tasks are run as functions, or with ``apply()``, which runs a task and
its retries in this process. The jobs they call are replaced, so nothing here
needs Postgres; tests/integration/test_jobs.py runs the real ones.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.ingestion.edgar.client import EdgarRateLimited, EdgarServerError
from app.ingestion.enrichment.figi import OpenFIGIRateLimited, OpenFIGIResponseError
from app.jobs import tasks
from app.jobs.celery_app import COMPUTE_QUEUE, INGESTION_QUEUE
from app.jobs.errors import JobError
from app.jobs.retry import (
    BACKOFF_FACTOR_SECONDS,
    BACKOFF_MAX_SECONDS,
    MAX_RETRIES,
    is_transient,
    retry_countdown,
)
from app.jobs.tracking import JobLocked, Ran, TrackedRun

# --- what retries ------------------------------------------------------------


class _AdaptedError(Exception):
    """What asyncpg's adapted error looks like to SQLAlchemy: a ``sqlstate``."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str, *, invalidated: bool = False) -> DBAPIError:
    return DBAPIError("SELECT 1", None, _AdaptedError(sqlstate), connection_invalidated=invalidated)


def _rate_limited(status_code: int) -> EdgarRateLimited:
    return EdgarRateLimited(url="https://www.sec.gov/x", status_code=status_code, retry_after=600)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionResetError("reset"),
        TimeoutError(),
        PoolTimeoutError(),
        EdgarServerError(url="https://www.sec.gov/x", status_code=503),
        _rate_limited(429),
        OpenFIGIRateLimited(retry_after=30),
        _db_error("08006"),
        _db_error("40P01"),
        _db_error("40001"),
        _db_error("57P01"),
        _db_error("53300"),
        _db_error("XX000", invalidated=True),
    ],
    ids=repr,
)
def test_the_moments_a_dependency_has_are_retried(failure: BaseException) -> None:
    assert is_transient(failure)


@pytest.mark.parametrize(
    "failure",
    [
        # A User-Agent SEC has blocked: retrying it makes it permanent.
        _rate_limited(403),
        httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "https://x"),
            response=httpx.Response(404),
        ),
        OpenFIGIResponseError("misaligned"),
        IntegrityError("INSERT", None, _AdaptedError("23505")),
        _db_error("42P01"),
        JobError("not a 13F"),
        ValueError("a bug"),
        FileNotFoundError("sp500.csv"),
        SoftTimeLimitExceeded(),
    ],
    ids=repr,
)
def test_anything_else_fails_the_first_time(failure: BaseException) -> None:
    assert not is_transient(failure)


def test_the_backoff_doubles_up_to_its_cap_with_jitter_below_each_step() -> None:
    for retries in range(6):
        ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_FACTOR_SECONDS * 2**retries)
        drawn = {retry_countdown(httpx.ConnectError("x"), retries) for _ in range(50)}
        assert all(0 <= countdown <= ceiling for countdown in drawn)
        # Full jitter: fifty draws from a range that wide do not all agree.
        assert len(drawn) > 1


def test_the_backoff_is_never_sooner_than_a_source_asked() -> None:
    assert retry_countdown(_rate_limited(429), 0) >= 600
    assert retry_countdown(_rate_limited(429), 5) <= BACKOFF_MAX_SECONDS


# --- the task wrapper --------------------------------------------------------


def _ran(job_name: str, value: Any = None, **counted: Any) -> Ran[Any]:
    return Ran(run=TrackedRun(id=uuid.uuid4(), job_name=job_name, **counted), value=value)


def _fake_job(*outcomes: BaseException | Ran[Any]) -> tuple[Any, list[dict[str, Any]]]:
    """A job that raises or returns each outcome in turn, and the calls it got."""
    calls: list[dict[str, Any]] = []
    remaining = list(outcomes)

    async def job(settings: object, **arguments: Any) -> Ran[Any]:
        calls.append(arguments)
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return job, calls


def test_a_task_returns_its_runs_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _ran("fetch-quotes", items_seen=3, items_written=2, errors=["BAD: no close"])
    job, calls = _fake_job(ran)
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)

    summary = tasks.fetch_prices(tickers=["NVDA"])

    assert calls == [{"tickers": ["NVDA"]}]
    assert summary == {
        "job": "fetch-quotes",
        "run_id": str(ran.run.id),
        "status": "partial",
        "items_seen": 3,
        "items_written": 2,
        "errors": 1,
    }


def test_a_locked_job_is_a_successful_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = _fake_job(JobLocked("fetch-quotes"))
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)

    assert tasks.fetch_prices() == {"job": "fetch-quotes", "skipped": "locked"}


def test_a_transient_failure_asks_celery_to_retry_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = httpx.ConnectError("refused")
    job, _ = _fake_job(failure)
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)
    asked: dict[str, Any] = {}

    def retry(**options: Any) -> Retry:
        asked.update(options)
        return Retry()

    monkeypatch.setattr(tasks.fetch_prices, "retry", retry)

    with pytest.raises(Retry):
        tasks.fetch_prices()

    assert asked["exc"] is failure
    assert 0 <= asked["countdown"] <= BACKOFF_FACTOR_SECONDS


def test_a_permanent_failure_is_raised_without_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    job, calls = _fake_job(JobError("not a 13F"), _ran("fetch-quotes"))
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)

    result = tasks.fetch_prices.apply()

    assert result.state == "FAILURE"
    assert isinstance(result.result, JobError)
    assert len(calls) == 1


def test_a_task_that_recovers_on_a_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """``apply()`` runs the retries here and now, without their countdown."""
    job, calls = _fake_job(
        httpx.ConnectError("refused"), _db_error("40P01"), _ran("fetch-quotes", items_written=1)
    )
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)

    tasks.fetch_prices.apply()

    assert len(calls) == 3


def test_retries_give_up_with_the_original_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    job, calls = _fake_job(*(httpx.ConnectError("refused") for _ in range(MAX_RETRIES + 1)))
    monkeypatch.setattr("app.jobs.enrichment.fetch_prices", job)

    result = tasks.fetch_prices.apply()

    assert len(calls) == MAX_RETRIES + 1
    assert result.state == "FAILURE"
    assert isinstance(result.result, httpx.ConnectError)


# --- arguments ---------------------------------------------------------------


def test_dates_arrive_as_iso_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    job, calls = _fake_job(_ran("backfill_13f", SimpleNamespace(published=frozenset())))
    monkeypatch.setattr("app.jobs.thirteen_f.ingest_pending_filings", job)

    summary = tasks.ingest_pending_filings(since="2024-01-01", limit=10)

    [arguments] = calls
    assert arguments["since"].isoformat() == "2024-01-01"
    assert (arguments["limit"], arguments["concurrency"], arguments["force"]) == (10, 5, False)
    assert summary["published"] == []


def test_recompute_on_its_own_must_be_told_what_to_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, calls = _fake_job(_ran("recompute"))
    monkeypatch.setattr("app.jobs.derived.recompute_positions", job)

    with pytest.raises(ValueError, match="say what to rebuild"):
        tasks.recompute_positions()
    with pytest.raises(ValueError, match="say what to rebuild"):
        tasks.recompute_positions(filer="a-fund", everything=True)
    tasks.recompute_positions(period="2026Q1")

    assert [arguments["period"].isoformat() for arguments in calls] == ["2026-03-31"]


# --- chains ------------------------------------------------------------------


def test_recompute_after_an_ingest_rebuilds_what_it_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, calls = _fake_job(_ran("recompute"))
    monkeypatch.setattr("app.jobs.derived.recompute_positions", job)
    ingest_run = uuid.uuid4()

    tasks.recompute_positions(
        {"run_id": str(ingest_run), "published": [[7, "2024-03-31"], [7, "2024-06-30"]]}
    )

    [arguments] = calls
    assert [(filer, period.isoformat()) for filer, period in arguments["pairs"]] == [
        (7, "2024-03-31"),
        (7, "2024-06-30"),
    ]
    assert arguments["after_run_id"] == ingest_run


@pytest.mark.parametrize(
    "upstream",
    [{"job": "backfill_13f", "skipped": "locked"}, {"run_id": str(uuid.uuid4()), "published": []}],
)
def test_recompute_after_an_ingest_that_published_nothing_does_nothing(
    monkeypatch: pytest.MonkeyPatch, upstream: dict[str, Any]
) -> None:
    job, calls = _fake_job()
    monkeypatch.setattr("app.jobs.derived.recompute_positions", job)

    summary = tasks.recompute_positions(upstream)

    assert summary["skipped"] and calls == []


def test_refresh_follows_any_rebuild_that_ran_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, calls = _fake_job(_ran("refresh-views"))
    monkeypatch.setattr("app.jobs.derived.refresh_matviews", job)
    rebuild = uuid.uuid4()

    assert tasks.refresh_matviews({"job": "recompute", "skipped": "nothing published"})["skipped"]
    # A rebuild that published nothing may still have deleted rows the views count.
    tasks.refresh_matviews({"run_id": str(rebuild), "items_written": 0})

    assert [arguments["after_run_id"] for arguments in calls] == [rebuild]


def test_the_13f_chain_is_ingest_then_recompute_then_refresh() -> None:
    chain = tasks.thirteen_f_poll(concurrency=3)

    assert [(str(link.task).rsplit(".", 1)[1], link.immutable) for link in chain.tasks] == [
        ("discover_13f_filings", True),
        # Not given discovery's result: the queue is worth draining whatever it found.
        ("ingest_pending_filings", True),
        ("recompute_positions", False),
        ("refresh_matviews", False),
    ]
    assert chain.tasks[1].kwargs == {"concurrency": 3}


def test_the_form4_chain_is_the_crawl_then_its_ingest() -> None:
    chain = tasks.form4_chain()

    assert [str(link.task).rsplit(".", 1)[1] for link in chain.tasks] == [
        "crawl_form4_daily",
        "ingest_form4",
    ]


# --- beat triggers -----------------------------------------------------------


class _Applied:
    """What a patched ``apply_async`` returns: a result nobody may wait on."""

    id = "last-link"

    def get(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("a trigger waited on the chain it enqueued")


def _capture_chains(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    applied: list[Any] = []

    def apply_async(self: Any, *_: Any, **__: Any) -> _Applied:
        applied.append(self)
        return _Applied()

    monkeypatch.setattr(type(tasks.form4_chain()), "apply_async", apply_async)
    return applied


def _links(chain: Any) -> list[str]:
    return [str(link.task).rsplit(".", 1)[1] for link in chain.tasks]


def test_the_daily_form4_trigger_enqueues_the_crawl_then_its_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = _capture_chains(monkeypatch)

    assert tasks.daily_form4() == {"chain": "form4", "task_id": "last-link"}
    assert [_links(chain) for chain in applied] == [["crawl_form4_daily", "ingest_form4"]]


def test_in_a_filing_window_the_poll_trigger_enqueues_the_four_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = _capture_chains(monkeypatch)
    monkeypatch.setattr(tasks, "_now", lambda: datetime(2026, 2, 10, 15, 30, tzinfo=UTC))

    assert tasks.poll_13f() == {"chain": "13f-poll", "task_id": "last-link"}
    assert [_links(chain) for chain in applied] == [
        [
            "discover_13f_filings",
            "ingest_pending_filings",
            "recompute_positions",
            "refresh_matviews",
        ]
    ]


def test_outside_a_window_the_poll_trigger_enqueues_only_at_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = _capture_chains(monkeypatch)
    # 24 October 2026, daylight saving: 06:00 New York is 10:00 UTC.
    monkeypatch.setattr(tasks, "_now", lambda: datetime(2026, 10, 24, 15, 30, tzinfo=UTC))

    assert tasks.poll_13f() == {"chain": "13f-poll", "skipped": "outside the filing window"}
    assert applied == []

    monkeypatch.setattr(tasks, "_now", lambda: datetime(2026, 10, 24, 10, 0, tzinfo=UTC))

    assert tasks.poll_13f()["task_id"] == "last-link"
    assert len(applied) == 1


def test_edgar_and_network_work_is_on_ingestion_and_postgres_work_on_compute() -> None:
    queues = {
        name: task.queue
        for name, task in vars(tasks).items()
        if hasattr(task, "queue") and hasattr(task, "delay")
    }

    assert queues == {
        "discover_13f_filings": INGESTION_QUEUE,
        "ingest_pending_filings": INGESTION_QUEUE,
        "crawl_form4_daily": INGESTION_QUEUE,
        "ingest_form4": INGESTION_QUEUE,
        "resolve_cusips": INGESTION_QUEUE,
        "fetch_prices": INGESTION_QUEUE,
        "recompute_positions": COMPUTE_QUEUE,
        "refresh_matviews": COMPUTE_QUEUE,
        "purge_accounts": COMPUTE_QUEUE,
        "run_billing": COMPUTE_QUEUE,
        "poll_13f": COMPUTE_QUEUE,
        "daily_form4": COMPUTE_QUEUE,
        "ping": COMPUTE_QUEUE,
    }
