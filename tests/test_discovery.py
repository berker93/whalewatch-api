"""The parts of :mod:`app.ingestion.discovery` that need no database.

The set difference and the queue are in tests/integration/test_cli_discover_filings.py.
"""

from datetime import date

from app.ingestion.discovery import default_since


def test_the_default_window_is_five_years_back() -> None:
    assert default_since(date(2026, 9, 30)) == date(2021, 9, 30)


def test_five_years_back_from_a_leap_day_is_the_28th() -> None:
    assert default_since(date(2024, 2, 29)) == date(2019, 2, 28)
