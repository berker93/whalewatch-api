"""The beat schedule: when the 13F poll polls, and what each entry sends when.

Without a scheduler: :func:`should_poll_now` is a function of a moment, and
the entries are data.
"""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from celery.schedules import crontab

from app.jobs import tasks
from app.jobs.schedule import (
    BEAT_SCHEDULE,
    POLL_EVERY_MINUTES,
    POLL_EXPIRES_SECONDS,
    TIMEZONE,
    should_poll_now,
)
from tests.test_celery_app import _celery

NEW_YORK = ZoneInfo("America/New_York")


def _ny(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK)


# --- should_poll_now ---------------------------------------------------------


@pytest.mark.parametrize("minute", range(0, 24 * 60, POLL_EVERY_MINUTES))
def test_every_send_in_a_filing_window_polls(minute: int) -> None:
    assert should_poll_now(_ny(2026, 5, 14) + timedelta(minutes=minute))


@pytest.mark.parametrize(
    ("moment", "polls"),
    [
        (_ny(2026, 10, 24, 5, 59), False),
        (_ny(2026, 10, 24, 6, 0), True),
        # A send that ran late, but inside the half hour it was sent in.
        (_ny(2026, 10, 24, 6, 24), True),
        (_ny(2026, 10, 24, 6, 30), False),
        (_ny(2026, 10, 24, 18, 0), False),
    ],
    ids=str,
)
def test_outside_a_window_only_the_six_oclock_send_polls(moment: datetime, polls: bool) -> None:
    assert should_poll_now(moment) is polls


@pytest.mark.parametrize(
    "moment",
    [
        # 06:00 New York is 10:00 UTC under daylight saving...
        datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        # ...and 11:00 UTC without it.
        datetime(2026, 12, 1, 11, 0, tzinfo=UTC),
        datetime(2026, 12, 1, 3, 0, tzinfo=timezone(timedelta(hours=-8))),
    ],
    ids=str,
)
def test_the_six_oclock_send_is_new_yorks_whatever_zone_the_moment_is_in(
    moment: datetime,
) -> None:
    assert should_poll_now(moment)
    assert not should_poll_now(moment + timedelta(hours=1))


def test_whose_day_it_is_is_new_yorks() -> None:
    # 22:00 on 20 February in New York, already the 21st in UTC: in the window.
    assert should_poll_now(datetime(2026, 2, 21, 3, 0, tzinfo=UTC))
    # 23:00 on 31 January in New York, already 1 February in UTC: not yet.
    assert not should_poll_now(datetime(2026, 2, 1, 4, 0, tzinfo=UTC))


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="no time zone"):
        should_poll_now(datetime(2026, 10, 24, 6, 0))


# --- the entries -------------------------------------------------------------


def test_the_schedule_is_this_module_and_beat_runs_the_default_scheduler() -> None:
    """In code, not a database: no django-celery-beat or RedBeat scheduler,
    whose entries live outside the repository and drift from it."""
    conf = _celery().conf

    assert conf.beat_schedule == BEAT_SCHEDULE
    assert conf.beat_scheduler == "celery.beat:PersistentScheduler"


def test_crontabs_are_new_york_times_and_celery_keeps_utc() -> None:
    conf = _celery().conf

    assert str(conf.timezone) == TIMEZONE == "America/New_York"
    assert conf.enable_utc is True


@pytest.mark.parametrize("name", sorted(BEAT_SCHEDULE))
def test_every_entry_names_a_task_that_exists(name: str) -> None:
    module, _, task = BEAT_SCHEDULE[name]["task"].rpartition(".")

    assert module == "app.jobs.tasks"
    assert hasattr(getattr(tasks, task), "delay")


@pytest.mark.parametrize(
    ("name", "task", "when"),
    [
        ("poll-13f", "poll_13f", crontab(minute="0,30")),
        ("daily-form4", "daily_form4", crontab(minute=0, hour=22, day_of_week="mon-fri")),
        ("fetch-prices", "fetch_prices", crontab(minute=30, hour=21, day_of_week="mon-fri")),
        ("resolve-cusips", "resolve_cusips", crontab(minute=0, hour=3)),
        ("refresh-matviews", "refresh_matviews", crontab(minute=0, hour=4)),
    ],
)
def test_each_entry_runs_when_the_ticket_says(name: str, task: str, when: crontab) -> None:
    assert BEAT_SCHEDULE[name]["task"] == f"app.jobs.tasks.{task}"
    assert BEAT_SCHEDULE[name]["schedule"] == when


#: The shortest gap between two sends of each entry.
_SECONDS_BETWEEN_SENDS = {
    "heartbeat": 5 * 60,
    "poll-13f": POLL_EVERY_MINUTES * 60,
    "daily-form4": 24 * 60 * 60,
    "fetch-prices": 24 * 60 * 60,
    "resolve-cusips": 24 * 60 * 60,
    "refresh-matviews": 24 * 60 * 60,
}


def test_every_entry_expires_by_the_time_the_next_is_sent() -> None:
    assert _SECONDS_BETWEEN_SENDS.keys() == BEAT_SCHEDULE.keys()
    for name, entry in BEAT_SCHEDULE.items():
        assert 0 < entry["options"]["expires"] <= _SECONDS_BETWEEN_SENDS[name], name


def test_a_poll_send_expires_inside_its_own_half_hour() -> None:
    """What lets should_poll_now tell the 06:00 send from the 06:30 one by the
    clock: a send that has not run by the next one never runs."""
    assert POLL_EXPIRES_SECONDS < POLL_EVERY_MINUTES * 60
