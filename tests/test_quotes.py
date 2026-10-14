"""The quote providers, without a database: which one is chosen, where yfinance
is refused, and how a yfinance frame is read.

The frame tests need pandas, which comes with yfinance in the optional
``quotes`` group, and are skipped without it. What is under test there is
:func:`~app.ingestion.enrichment.quotes.last_close` on a frame shaped as
``yfinance.download(..., group_by="ticker")`` returns one, not Yahoo.
"""

import os
import subprocess
import sys
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.ingestion.enrichment.quotes import (
    CLOSE_SETTLED,
    MARKET_TIMEZONE,
    NullQuoteProvider,
    Quote,
    QuoteProviderUnavailableError,
    YFinanceQuoteProvider,
    last_close,
    quote_provider,
    yahoo_symbol,
)
from tests.conftest import make_settings

MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 13)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=MARKET_TIMEZONE)


# --- choosing one --------------------------------------------------------------


async def test_the_default_provider_is_null_and_knows_no_quotes() -> None:
    settings = make_settings()

    provider = quote_provider(settings)

    assert settings.quote_provider == "null"
    assert isinstance(provider, NullQuoteProvider)
    assert await provider.latest_close(["AAPL", "NVDA"]) == {}


def test_production_refuses_yfinance_at_boot() -> None:
    with pytest.raises(ValidationError, match="QUOTE_PROVIDER=yfinance is for development"):
        make_settings(environment="production", quote_provider="yfinance")


def test_staging_may_try_yfinance() -> None:
    """Production only (06d D3): staging is not public."""
    assert make_settings(environment="staging", quote_provider="yfinance").quote_provider == (
        "yfinance"
    )


def test_the_provider_refuses_production_itself_before_importing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the settings say: constructed directly, in production, it
    refuses. yfinance being absent (None in sys.modules makes the import
    fail) shows that it got no further than the check."""
    monkeypatch.setitem(sys.modules, "yfinance", None)

    with pytest.raises(QuoteProviderUnavailableError, match="for development"):
        YFinanceQuoteProvider(make_settings(environment="production"))


def test_without_the_quotes_group_yfinance_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", None)

    with pytest.raises(QuoteProviderUnavailableError, match="uv sync --group quotes"):
        quote_provider(make_settings(quote_provider="yfinance"))


def test_nothing_on_the_production_path_imports_yfinance() -> None:
    """The app, the CLI and the providers' module, imported in a fresh
    interpreter, leave yfinance unimported: installed or not, nothing that
    serves or operates production loads it."""
    script = (
        "import sys, app.main, app.cli, app.ingestion.enrichment.quotes; "
        "sys.exit('yfinance' in sys.modules)"
    )
    env = {**os.environ, "POSTGRES_PASSWORD": "localdev", "SEC_CONTACT_EMAIL": "t@whalewatch.io"}

    result = subprocess.run([sys.executable, "-c", script], env=env, check=False)

    assert result.returncode == 0


def test_a_share_class_is_spelled_with_a_hyphen_at_yahoo() -> None:
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert yahoo_symbol("NVDA") == "NVDA"


# --- reading a frame -----------------------------------------------------------


def _frame(closes: dict[str, list[float]], days: list[date]) -> Any:
    """A ``yfinance.download(..., group_by="ticker")`` frame of daily bars."""
    pandas = pytest.importorskip("pandas")
    columns = pandas.MultiIndex.from_tuples(
        [(symbol, field) for symbol in closes for field in ("Open", "Close", "Volume")],
        names=["Ticker", "Price"],
    )
    rows = [
        [value for symbol in closes for value in (closes[symbol][i], closes[symbol][i], 1.0)]
        for i in range(len(days))
    ]
    return pandas.DataFrame(rows, index=pandas.DatetimeIndex(days, name="Date"), columns=columns)


def test_the_latest_close_is_the_last_bar_rounded_to_a_tick() -> None:
    frame = _frame({"NVDA": [180.0, 516.570007]}, [MONDAY, TUESDAY])

    assert last_close(frame, "NVDA", now=_at(date(2026, 10, 14), 9)) == (
        TUESDAY,
        Decimal("516.5700"),
    )


def test_todays_bar_is_not_a_close_until_after_the_close() -> None:
    frame = _frame({"NVDA": [180.0, 181.0]}, [MONDAY, TUESDAY])
    settled = CLOSE_SETTLED.hour, CLOSE_SETTLED.minute

    assert last_close(frame, "NVDA", now=_at(TUESDAY, 11)) == (MONDAY, Decimal("180.0000"))
    assert last_close(frame, "NVDA", now=_at(TUESDAY, *settled)) == (TUESDAY, Decimal("181.0000"))


def test_a_symbol_yahoo_had_nothing_for_has_no_close() -> None:
    nan = float("nan")
    frame = _frame({"NVDA": [180.0, 181.0], "GONE": [nan, nan]}, [MONDAY, TUESDAY])
    now = _at(date(2026, 10, 14), 9)

    assert last_close(frame, "GONE", now=now) is None
    assert last_close(frame, "NEVER", now=now) is None
    assert last_close(None, "NVDA", now=now) is None


def test_a_missing_last_bar_falls_back_to_the_one_before() -> None:
    frame = _frame({"NVDA": [180.0, float("nan")]}, [MONDAY, TUESDAY])

    assert last_close(frame, "NVDA", now=_at(date(2026, 10, 14), 9)) == (
        MONDAY,
        Decimal("180.0000"),
    )


async def test_yfinance_is_asked_in_its_spelling_and_answers_in_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Long settled, whatever the clock says: the provider reads the real one.
    monday, tuesday = date(2020, 1, 6), date(2020, 1, 7)
    frame = _frame({"BRK-B": [500.0, 510.25], "NVDA": [180.0, 181.5]}, [monday, tuesday])
    asked: list[list[str]] = []

    def download(symbols: list[str], **options: Any) -> Any:
        asked.append(symbols)
        assert options["auto_adjust"] is False
        assert options["group_by"] == "ticker"
        return frame

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    provider = YFinanceQuoteProvider(make_settings(quote_provider="yfinance"))

    quotes = await provider.latest_close(["BRK.B", "NVDA", "GONE"])

    assert asked == [["BRK-B", "NVDA", "GONE"]]
    assert quotes == {
        "BRK.B": Quote("BRK.B", tuesday, Decimal("510.2500")),
        "NVDA": Quote("NVDA", tuesday, Decimal("181.5000")),
    }
