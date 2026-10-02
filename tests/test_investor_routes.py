"""The investor router's arithmetic, which needs no database."""

from datetime import date
from decimal import Decimal

from app.api.routers.investors import _sparkline


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
