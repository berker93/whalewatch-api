"""When the Senate is in session, for how often eFD is polled.

The schedule polls eFD every two hours on a day the Senate sits and once a day
otherwise (:func:`app.jobs.schedule.should_poll_senate_now`). Which days it
sits is the Majority Leader's published calendar for the year — the
`2026 schedule <https://www.senate.gov/legislative/2026_schedule.htm>`_ — as
written out in :data:`CALENDARS`: when the session convenes, its target
adjournment, and the state work periods between, with the federal holidays.

It is a plan, not a record: the Senate stays in late, or leaves early, and
nothing here learns of it. Getting it wrong only changes how often eFD is
asked, never what is read, since every poll reads the whole window since the
watermark.

A year not written out is a year whose calendar is not published yet, or not
copied here: every weekday of it counts as a sitting day, polling more often
rather than less, and :func:`in_session` logs that once so someone adds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """One year's published plan."""

    convenes: date
    adjourns: date
    """The target adjournment: the last sitting day."""
    recesses: tuple[tuple[date, date], ...]
    """State work periods, first and last day each, both included."""
    holidays: frozenset[date]


def _days(first: str, last: str) -> tuple[date, date]:
    return date.fromisoformat(first), date.fromisoformat(last)


CALENDARS: Final[dict[int, SessionCalendar]] = {
    # The 2nd session of the 119th Congress, as released in November 2025.
    2026: SessionCalendar(
        convenes=date(2026, 1, 5),
        adjourns=date(2026, 12, 18),
        recesses=(
            _days("2026-01-19", "2026-01-23"),
            _days("2026-03-30", "2026-04-10"),
            _days("2026-05-04", "2026-05-08"),
            _days("2026-05-25", "2026-05-29"),
            _days("2026-06-19", "2026-06-19"),
            _days("2026-06-29", "2026-07-10"),
            _days("2026-08-10", "2026-09-11"),
            _days("2026-10-05", "2026-11-06"),
            _days("2026-11-23", "2026-11-27"),
            _days("2026-12-21", "2026-12-31"),
        ),
        holidays=frozenset(
            date.fromisoformat(day)
            for day in (
                "2026-01-01",
                "2026-01-19",
                "2026-02-16",
                "2026-05-25",
                "2026-06-19",
                "2026-07-03",
                "2026-09-07",
                "2026-10-12",
                "2026-11-11",
                "2026-11-26",
                "2026-12-25",
            )
        ),
    ),
}

_warned: set[int] = set()


def in_session(day: date) -> bool:
    """Whether the Senate plans to sit on ``day``. Weekends never; a year with
    no calendar here, every weekday."""
    if day.weekday() >= 5:
        return False
    calendar = CALENDARS.get(day.year)
    if calendar is None:
        if day.year not in _warned:
            _warned.add(day.year)
            logger.warning("senate_calendar.unknown_year", year=day.year)
        return True
    if not calendar.convenes <= day <= calendar.adjourns or day in calendar.holidays:
        return False
    return not any(first <= day <= last for first, last in calendar.recesses)
