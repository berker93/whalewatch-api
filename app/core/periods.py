"""Quarters: how a period is spelled, parsed and counted. Written once, here.

A 13F period is a calendar quarter, and the database names it by its last day,
``2026-03-31``. People name it ``2026Q1``. Every endpoint that takes a period
accepts both and means the same day by them, so there is one parser, and
everything that turns a period into a label or walks from one quarter to the
next uses the functions beside it rather than its own arithmetic.

A day that is not a quarter end is not a period. ``2026-03-30`` is refused,
not rounded to the quarter it falls in: a client that sent it has a date from
somewhere else and has misunderstood what a period is, and an answer for
``2026-03-31`` would hide that.
"""

import re
from datetime import date, timedelta
from typing import Final

#: The last day of each quarter's last month, by month.
_QUARTER_END_DAY: Final = {3: 31, 6: 30, 9: 30, 12: 31}

#: A 13F is due this many calendar days after the quarter it reports on.
FILING_DEADLINE_DAYS: Final = 45

# Four-digit years, both spellings. Matched in full, so nothing either side of
# the period (whitespace included) is quietly ignored. The quarter's letter in
# either case: "2026q1" is unambiguous, and refusing it helps no one.
_LABEL: Final = re.compile(r"(\d{4})[Qq](\d)")
# date.fromisoformat also takes "20260331" and "2026-W13-2", which are not
# spellings anyone means a period by. The shape is checked first.
_ISO_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_quarter_end(day: date) -> bool:
    """Whether ``day`` is the last day of a calendar quarter."""
    return _QUARTER_END_DAY.get(day.month) == day.day


def quarter_end(year: int, quarter: int) -> date:
    """The last day of ``quarter`` (1 to 4) of ``year``.

    :raises ValueError: ``quarter`` is not 1 to 4, or ``year`` is outside what
        a ``date`` holds.
    """
    if not 1 <= quarter <= 4:
        raise ValueError(f"quarter {quarter} is not 1 to 4")
    month = quarter * 3
    return date(year, month, _QUARTER_END_DAY[month])


def quarter_label(period: date) -> str:
    """``'2024Q1'`` for 2024-03-31: the spelling ``filing.quarter`` generates.

    :raises ValueError: ``period`` is not a quarter end, so has no label.
    """
    if not is_quarter_end(period):
        raise ValueError(f"{period} is not a quarter end")
    return f"{period.year}Q{period.month // 3}"


def parse_period(text: str) -> date:
    """The quarter end ``text`` names, as ``2026Q1`` or ``2026-03-31``.

    :raises ValueError: Neither spelling, a quarter outside 1 to 4, a day
        that does not exist, or a real day that is not a quarter end. The
        message says which, and names both accepted spellings.
    """
    if label := _LABEL.fullmatch(text):
        quarter = int(label[2])
        if not 1 <= quarter <= 4:
            raise ValueError(f"{text!r} has no quarter {quarter}: quarters are 1 to 4")
        return quarter_end(int(label[1]), quarter)

    if _ISO_DAY.fullmatch(text):
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a day of the calendar") from None
        if not is_quarter_end(day):
            raise ValueError(
                f"{text!r} is not a quarter end: a period is the last day of "
                "March, June, September or December"
            )
        return day

    raise ValueError(f"{text!r} is not a period: send a quarter as 2026Q1 or as 2026-03-31")


def filing_deadline(period: date) -> date:
    """The day a 13F for ``period`` is due: 45 calendar days after it.

    An approximation, and knowingly so. The real deadline is rolled forward to
    the next business day when the 45th day is a weekend or a federal holiday,
    and this does not roll it. So it can be up to three days early: 2025Q4's
    45th day is Saturday 14 February 2026, and with Presidents' Day on the
    Monday the filings were due on Tuesday the 17th. Anything that decides a
    period is finished on this date decides it that much too soon.

    :raises ValueError: ``period`` is not a quarter end.
    """
    if not is_quarter_end(period):
        raise ValueError(f"{period} is not a quarter end")
    return period + timedelta(days=FILING_DEADLINE_DAYS)


def quarters_ending(period: date, count: int) -> list[date]:
    """The ``count`` quarter ends up to and including ``period``, oldest first.

    :raises ValueError: ``period`` is not a quarter end.
    """
    last = _index(period)
    return [_from_index(index) for index in range(last - count + 1, last + 1)]


def quarters_between(first: date, last: date) -> list[date]:
    """Every quarter end from ``first`` to ``last``, both included, oldest first.

    Empty when ``first`` is after ``last``.

    :raises ValueError: Either is not a quarter end.
    """
    return [_from_index(index) for index in range(_index(first), _index(last) + 1)]


def _index(period: date) -> int:
    """Quarters since year 0, so that consecutive quarters differ by one."""
    if not is_quarter_end(period):
        raise ValueError(f"{period} is not a quarter end")
    return period.year * 4 + period.month // 3 - 1


def _from_index(index: int) -> date:
    year, quarter = divmod(index, 4)
    return quarter_end(year, quarter + 1)
