"""The Celery app: its delivery guarantees, its queues, and the ping round trip.

The round trip here runs a real worker, in a thread, against kombu's in-memory
transport: the message is serialised, routed to a queue, consumed, run and its
result stored and read back, with no Redis. That Redis is the broker in
compose is checked by ``make ping``, against the running stack.
"""

import os
import subprocess
import sys
import textwrap

from celery import Celery
from celery.contrib.testing.worker import start_worker

from app.jobs.celery_app import (
    COMPUTE_QUEUE,
    HARD_TIME_LIMIT_SECONDS,
    INGESTION_QUEUE,
    SOFT_TIME_LIMIT_SECONDS,
    create_celery,
)
from tests.conftest import make_settings


def _celery() -> Celery:
    return create_celery(make_settings(celery_redis_url="redis://broker.test:6379/1"))


def test_redis_is_the_broker_and_the_result_backend() -> None:
    conf = _celery().conf

    assert conf.broker_url == "redis://broker.test:6379/1"
    assert conf.result_backend == "redis://broker.test:6379/1"


def test_a_task_killed_mid_flight_is_redelivered_not_lost() -> None:
    conf = _celery().conf

    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1


def test_time_limits_are_thirty_minutes_soft_and_thirty_five_hard() -> None:
    conf = _celery().conf

    assert conf.task_soft_time_limit == SOFT_TIME_LIMIT_SECONDS == 30 * 60
    assert conf.task_time_limit == HARD_TIME_LIMIT_SECONDS == 35 * 60


def test_a_running_task_is_never_redelivered_to_a_second_worker() -> None:
    """Redis hands an unacknowledged message out again after the visibility
    timeout, whether or not its task is still running. Longer than the hard
    limit, no task can still be running when that happens."""
    conf = _celery().conf

    assert conf.broker_transport_options["visibility_timeout"] > conf.task_time_limit


def test_ingestion_and_compute_are_separate_queues() -> None:
    conf = _celery().conf

    assert {queue.name for queue in conf.task_queues} == {INGESTION_QUEUE, COMPUTE_QUEUE}
    assert conf.task_default_queue == COMPUTE_QUEUE
    assert conf.task_create_missing_queues is False


def test_each_queue_has_a_binding_of_its_own() -> None:
    """Two queues behind one exchange and key would each get a copy of every
    message published to that key, and a task would run once on each worker."""
    conf = _celery().conf

    bindings = {(queue.exchange.name, queue.routing_key) for queue in conf.task_queues}
    assert bindings == {(INGESTION_QUEUE, INGESTION_QUEUE), (COMPUTE_QUEUE, COMPUTE_QUEUE)}


def test_tasks_are_found_by_autodiscovery_not_imported() -> None:
    """In a fresh interpreter, because a task module any earlier test imported
    would register with every app built after it and prove nothing."""
    script = textwrap.dedent(
        """
        import sys
        from app.jobs.celery_app import celery_app

        assert "app.jobs.tasks" not in sys.modules, "imported by hand"
        celery_app.loader.import_default_modules()
        expected = {
            f"app.jobs.tasks.{name}"
            for name in (
                "discover_13f_filings",
                "ingest_pending_filings",
                "crawl_form4_daily",
                "ingest_form4",
                "resolve_cusips",
                "fetch_prices",
                "recompute_positions",
                "refresh_matviews",
                "ping",
            )
        }
        assert expected <= set(celery_app.tasks), sorted(celery_app.tasks)
        """
    )
    # conftest has set the two required variables in this process already.
    env = dict(os.environ)
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False
    )

    assert completed.returncode == 0, completed.stderr


def test_ping_makes_the_round_trip_through_a_worker() -> None:
    celery = _celery()
    celery.conf.update(
        broker_url="memory://",
        result_backend="cache+memory://",
        broker_transport_options={},
    )

    with start_worker(celery, pool="solo", queues=[COMPUTE_QUEUE], perform_ping_check=False):
        pong = celery.send_task("app.jobs.tasks.ping").get(timeout=10)

    assert pong["pong"] is True
    assert pong["worker"]
