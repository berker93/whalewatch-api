"""Celery beat schedule.

What runs when nobody is watching. Defined here, in code, and nowhere else:
beat runs its default scheduler, whose file (``--schedule`` in
docker-compose.yml) holds only when each entry last ran, never what the
entries are. Changing the schedule is changing this module.

Times are New York's, and stored times are UTC
-----------------------------------------------
Crontabs here are New York times: the app's ``timezone`` is :data:`TIMEZONE`,
EDGAR's, so ``hour=22`` is 22:00 Eastern, daylight saving or
not. Celery itself keeps UTC (``enable_utc``): the times in its messages and
results are UTC, as every ``timestamptz`` in Postgres is. The one decision made
from a clock outside a crontab, :func:`should_poll_now`, is given an aware
moment and turns it into New York's itself.

The 13F poll
------------
Most of a year's 13Fs arrive in its four filing windows
(:func:`~app.core.periods.in_filing_window`), so the poll runs every half hour
inside them and once a day outside. One entry, every half hour, with the
decision in the task (:func:`should_poll_now`), not two entries: a daily and a
half-hourly entry would both fire at 06:00 on every window day, and the
decision would be in a crontab, where no test reaches it.

Beat sends a task, not a chain, so each chain has a small task that applies it
and returns at once (``poll_13f``, ``daily_form4`` in app/jobs/tasks.py).
Each entry names its task by its registered name, a string, so this module
imports no task and the schedule cannot be the reason a task module was loaded.

Every entry expires before it is next sent: one nobody consumed, behind a
stopped worker, is dropped rather than piling up to fire all at once later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from celery.schedules import crontab

from app.core.periods import in_filing_window

#: The zone every crontab below is read in: the app's ``timezone``, and EDGAR's.
TIMEZONE: Final = "America/New_York"
_ZONE: Final = ZoneInfo(TIMEZONE)

#: How often the 13F poll entry fires, in and out of a filing window.
POLL_EVERY_MINUTES: Final = 30

#: Outside a filing window, the hour (New York) whose first send polls. Early,
#: so a late filer from yesterday is on the site before anyone looks.
DAILY_POLL_HOUR: Final = 6

#: Under :data:`POLL_EVERY_MINUTES`, and more than a schedule nicety: a send
#: is dropped before the next one is due, so a trigger runs inside the half
#: hour it was sent in, and :func:`should_poll_now` can tell the 06:00 send
#: from the 06:30 one by the clock it reads.
POLL_EXPIRES_SECONDS: Final = 25 * 60

#: For entries sent once a day: half a day late is still worth running, and the
#: next one is never more than a day behind it.
DAILY_EXPIRES_SECONDS: Final = 12 * 60 * 60


def should_poll_now(now: datetime) -> bool:
    """Whether the 13F poll sent at ``now`` should poll EDGAR.

    Always, inside a filing window; outside one, only for the send in the
    first half hour of :data:`DAILY_POLL_HOUR`. Both in New York time, whatever
    zone ``now`` is in.

    :raises ValueError: ``now`` is naive, so names no moment.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"{now.isoformat()} has no time zone: say which moment it is")
    local = now.astimezone(_ZONE)
    if in_filing_window(local.date()):
        return True
    return local.hour == DAILY_POLL_HOUR and local.minute < POLL_EVERY_MINUTES


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
    # Discover, ingest, recompute, refresh: every half hour in a filing window,
    # at 06:00 otherwise. The task decides which (should_poll_now).
    "poll-13f": {
        "task": "app.jobs.tasks.poll_13f",
        "schedule": crontab(minute=f"*/{POLL_EVERY_MINUTES}"),
        "options": {"expires": POLL_EXPIRES_SECONDS},
    },
    # EDGAR publishes the day's form index at about 22:00, so this often runs
    # before it is there. The crawl stops at an unpublished day without
    # failing and its watermark never passes a day it did not read in full, so
    # that day is picked up on the next run: Friday's on Monday.
    "daily-form4": {
        "task": "app.jobs.tasks.daily_form4",
        "schedule": crontab(minute=0, hour=22, day_of_week="mon-fri"),
        "options": {"expires": DAILY_EXPIRES_SECONDS},
    },
    # After the close, with the day's last price settled. Weekdays: there is
    # no new close on a weekend.
    "fetch-prices": {
        "task": "app.jobs.tasks.fetch_prices",
        "schedule": crontab(minute=30, hour=21, day_of_week="mon-fri"),
        "options": {"expires": DAILY_EXPIRES_SECONDS},
    },
    # When nothing else wants the ingestion worker or OpenFIGI.
    "resolve-cusips": {
        "task": "app.jobs.tasks.resolve_cusips",
        "schedule": crontab(minute=0, hour=3),
        "options": {"expires": DAILY_EXPIRES_SECONDS},
    },
    # The views are refreshed after every ingest that published, by the 13F
    # chain. This catches the rest: a recompute or a load run by hand, and the
    # securities resolve-cusips named an hour before.
    "refresh-matviews": {
        "task": "app.jobs.tasks.refresh_matviews",
        "schedule": crontab(minute=0, hour=4),
        "options": {"expires": DAILY_EXPIRES_SECONDS},
    },
    # Deleted accounts past their thirty days, erased. Daily, so an account is
    # gone within a day of its date; a missed day is caught by the next, since
    # the purge takes everything due, not the day's.
    "purge-accounts": {
        "task": "app.jobs.tasks.purge_accounts",
        "schedule": crontab(minute=0, hour=5),
        "options": {"expires": DAILY_EXPIRES_SECONDS},
    },
}
