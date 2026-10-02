"""Quarters: the one parser every ``?period=`` goes through, and the arithmetic beside it."""

from datetime import date

import pytest

from app.core.periods import (
    is_quarter_end,
    parse_period,
    quarter_end,
    quarter_label,
    quarters_between,
    quarters_ending,
)

# --- parse_period ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("2026Q1", date(2026, 3, 31)),
        ("2026Q2", date(2026, 6, 30)),
        ("2026Q3", date(2026, 9, 30)),
        ("2026Q4", date(2026, 12, 31)),
        ("2026q1", date(2026, 3, 31)),
        ("1999Q4", date(1999, 12, 31)),
        ("0001Q1", date(1, 3, 31)),
        ("9999Q4", date(9999, 12, 31)),
    ],
)
def test_a_label_is_its_quarters_last_day(text: str, period: date) -> None:
    assert parse_period(text) == period


@pytest.mark.parametrize(
    "day", [date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30), date(2026, 12, 31)]
)
def test_a_quarter_end_is_itself(day: date) -> None:
    assert parse_period(day.isoformat()) == day


@pytest.mark.parametrize("label", ["2024Q1", "2024Q2", "2024Q3", "2024Q4"])
def test_both_spellings_of_a_quarter_are_the_same_day(label: str) -> None:
    day = parse_period(label)

    assert parse_period(day.isoformat()) == day
    assert quarter_label(day) == label


@pytest.mark.parametrize("text", ["2026Q0", "2026Q5", "2026Q9"])
def test_a_quarter_outside_one_to_four_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="quarters are 1 to 4"):
        parse_period(text)


@pytest.mark.parametrize(
    "text",
    [
        # The day before and after each quarter end.
        "2026-03-30",
        "2026-04-01",
        "2026-06-29",
        "2026-07-01",
        "2026-09-29",
        "2026-10-01",
        "2026-12-30",
        "2027-01-01",
        # The last day of a month that ends no quarter.
        "2026-02-28",
        "2024-02-29",
        "2026-04-30",
    ],
)
def test_a_real_day_that_is_not_a_quarter_end_is_refused_not_rounded(text: str) -> None:
    with pytest.raises(ValueError, match="not a quarter end"):
        parse_period(text)


@pytest.mark.parametrize("text", ["2026-06-31", "2026-09-31", "2026-02-30", "2026-13-31"])
def test_a_day_the_calendar_does_not_have_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a day of the calendar"):
        parse_period(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026",
        "Q1",
        "Q1 2026",
        "2026 Q1",
        "2026-Q1",
        "26Q1",
        "02026Q1",
        "2026Q11",
        "2026Q",
        " 2026Q1",
        "2026Q1 ",
        "2026-3-31",
        "20260331",
        "2026/03/31",
        "31-03-2026",
        "2026-03-31T00:00:00",
        "2026-W13-2",
        "2026H1",
        "latest",
    ],
)
def test_anything_else_is_refused_naming_both_spellings(text: str) -> None:
    with pytest.raises(ValueError, match=r"2026Q1 or as 2026-03-31"):
        parse_period(text)


# --- the rest --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "label"),
    [(date(2024, 3, 31), "2024Q1"), (date(2024, 6, 30), "2024Q2"), (date(2024, 12, 31), "2024Q4")],
)
def test_quarter_label(period: date, label: str) -> None:
    assert quarter_label(period) == label


def test_a_day_that_is_not_a_quarter_end_has_no_label() -> None:
    with pytest.raises(ValueError, match="not a quarter end"):
        quarter_label(date(2024, 3, 30))


def test_quarter_end_refuses_a_fifth_quarter() -> None:
    with pytest.raises(ValueError, match="1 to 4"):
        quarter_end(2026, 5)


def test_is_quarter_end() -> None:
    assert is_quarter_end(date(2026, 9, 30))
    assert not is_quarter_end(date(2026, 9, 29))
    assert not is_quarter_end(date(2026, 8, 31))


def test_quarters_ending_crosses_years_oldest_first() -> None:
    assert quarters_ending(date(2025, 3, 31), 4) == [
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
    ]


def test_quarters_ending_ends_on_the_period() -> None:
    quarters = quarters_ending(date(2026, 12, 31), 8)

    assert len(quarters) == 8
    assert quarters[0] == date(2025, 3, 31)
    assert quarters[-1] == date(2026, 12, 31)


@pytest.mark.parametrize("day", [date(2025, 3, 30), date(2025, 4, 30), date(2025, 6, 1)])
def test_quarters_ending_refuses_a_day_that_is_not_a_quarter_end(day: date) -> None:
    with pytest.raises(ValueError, match="not a quarter end"):
        quarters_ending(day, 8)


def test_quarters_between_includes_both_ends() -> None:
    assert quarters_between(date(2024, 9, 30), date(2025, 3, 31)) == [
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
    ]


def test_quarters_between_one_quarter_and_itself_is_that_quarter() -> None:
    assert quarters_between(date(2025, 3, 31), date(2025, 3, 31)) == [date(2025, 3, 31)]


def test_quarters_between_backwards_is_empty() -> None:
    assert quarters_between(date(2025, 3, 31), date(2024, 12, 31)) == []
