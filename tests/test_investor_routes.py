"""The investor router's arithmetic, which needs no database."""

from datetime import date
from decimal import Decimal

import pytest

from app.api.routers.investors import _sparkline, quarters_ending


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


def test_a_sparkline_places_each_value_in_its_quarter() -> None:
    values = _sparkline(
        date(2025, 3, 31),
        # Older than eight quarters: ignored, though the query never sends one.
        [date(2022, 12, 31), date(2024, 6, 30), date(2025, 3, 31)],
        [Decimal(1), Decimal(2), Decimal(3)],
    )

    assert values == [None, None, None, None, Decimal(2), None, None, Decimal(3)]


def test_a_sparkline_with_no_latest_period_is_empty() -> None:
    assert _sparkline(None, None, None) == []
