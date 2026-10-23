"""``fetch-quotes``: what is asked, what is written, and what one bad ticker costs.

Against a real Postgres, with a fake in place of the provider: yfinance's
reading of Yahoo has its own tests (tests/test_quotes.py), and what is under
test here is which securities are asked about, how a failure is contained, and
what is written from the answers. The job commits through its own sessions, so
tables are truncated around each test rather than rolled back, as in
test_cli_resolve_cusips.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any, Final

import pytest
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.api.cache import KEY_PREFIX
from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import IngestionRun, Security, SecurityQuote
from app.db.session import create_session_factory
from app.ingestion.enrichment import quotes as quotes_module
from app.ingestion.enrichment.quotes import (
    MAX_CONSECUTIVE_FAILURES,
    NullQuoteProvider,
    Quote,
    QuoteFetchError,
    fetch_quotes,
)
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

MONDAY: Final = date(2026, 10, 12)
TUESDAY: Final = date(2026, 10, 13)


class FakeProvider:
    """Stands in for yfinance: a close for every ticker in :attr:`closes`,
    nothing for the rest, and an exception for any call that asks about one
    in :attr:`breaking`, alone or in a batch."""

    name = "yfinance"

    def __init__(self, closes: dict[str, str], *, day: date = TUESDAY) -> None:
        self.closes = closes
        self.day = day
        self.breaking: set[str] = set()
        self.calls: list[list[str]] = []

    async def latest_close(self, tickers: list[str]) -> dict[str, Quote]:
        self.calls.append(tickers)
        if broken := self.breaking & set(tickers):
            raise RuntimeError(f"Yahoo choked on {sorted(broken)}")
        return {
            ticker: Quote(ticker, self.day, Decimal(self.closes[ticker]))
            for ticker in tickers
            if ticker in self.closes
        }

    @property
    def asked(self) -> list[str]:
        return [ticker for call in self.calls for ticker in call]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _execute(migrated_engine, _TRUNCATE)
    yield
    _execute(migrated_engine, _TRUNCATE)


_TRUNCATE: Final = text("TRUNCATE ingestion_run, security RESTART IDENTITY CASCADE")


def _execute(engine: AsyncEngine, *statements: Executable) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(statement)).all()]

    return asyncio.run(run())


def _securities(engine: AsyncEngine, *rows: dict[str, Any]) -> None:
    """``security`` rows; a ticker gets the source a ticker needs."""
    _execute(
        engine,
        *(
            insert(Security).values(
                cusip=f"{n:09d}",
                **({"resolution_source": "manual"} if row.get("ticker") else {}),
                **row,
            )
            for n, row in enumerate(rows, start=1)
        ),
    )


def _listed(engine: AsyncEngine, *tickers: str) -> None:
    _securities(engine, *({"ticker": ticker} for ticker in tickers))


def _quotes(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            SecurityQuote.ticker, SecurityQuote.date, SecurityQuote.close, SecurityQuote.source
        ).order_by(SecurityQuote.ticker, SecurityQuote.date),
    )


def _run(engine: AsyncEngine, provider: Any, **options: Any) -> quotes_module.QuoteReport:
    return asyncio.run(fetch_quotes(create_session_factory(engine), provider, **options))


# --- what is asked ---------------------------------------------------------------


def test_the_null_provider_is_asked_nothing_and_nothing_is_written(
    migrated_engine: AsyncEngine,
) -> None:
    _listed(migrated_engine, "AAPL", "NVDA")

    report = _run(migrated_engine, NullQuoteProvider())

    assert (report.provider, report.listed, report.asked, report.written) == ("null", 2, 0, 0)
    assert report.missing == []
    assert _quotes(migrated_engine) == []


def test_only_listed_securities_with_a_ticker_of_their_own_are_asked(
    migrated_engine: AsyncEngine,
) -> None:
    """Not one with no ticker, not one delisted (its last ticker may be
    someone else's by now), and not a ticker two listed securities claim:
    the close would be written to both, and one of them is wrong."""
    _securities(
        migrated_engine,
        {"ticker": "NVDA"},
        {"ticker": "BRK.B"},
        {"ticker": None},
        {"ticker": "GONE", "delisted_by": date(2025, 3, 31)},
        {"ticker": "APG"},
        {"ticker": "APG"},
    )
    provider = FakeProvider({"NVDA": "181.5", "BRK.B": "510.25", "GONE": "1", "APG": "30"})

    report = _run(migrated_engine, provider)

    assert sorted(provider.asked) == ["BRK.B", "NVDA"]
    assert (report.listed, report.asked, report.written) == (4, 2, 2)
    assert report.ambiguous == ["APG"]
    assert _quotes(migrated_engine) == [
        ("BRK.B", TUESDAY, Decimal("510.25000000"), "yfinance"),
        ("NVDA", TUESDAY, Decimal("181.50000000"), "yfinance"),
    ]


def test_named_tickers_are_the_only_ones_asked_however_they_are_spelled(
    migrated_engine: AsyncEngine,
) -> None:
    _listed(migrated_engine, "NVDA", "BRK.B", "AAPL")
    provider = FakeProvider({"NVDA": "181.5", "BRK.B": "510.25", "AAPL": "250"})

    report = _run(migrated_engine, provider, tickers=["brk-b", "nvda", "NOPE"])

    assert sorted(provider.asked) == ["BRK.B", "NVDA"]
    assert report.unknown == ["NOPE"]
    assert report.written == 2


# --- what is written ---------------------------------------------------------------


def test_a_rerun_on_the_same_trading_day_replaces_the_close_and_a_new_day_adds_one(
    migrated_engine: AsyncEngine,
) -> None:
    _listed(migrated_engine, "NVDA")

    _run(migrated_engine, FakeProvider({"NVDA": "180"}, day=MONDAY))
    _run(migrated_engine, FakeProvider({"NVDA": "181"}, day=MONDAY))
    _run(migrated_engine, FakeProvider({"NVDA": "185"}, day=TUESDAY))

    assert _quotes(migrated_engine) == [
        ("NVDA", MONDAY, Decimal("181.00000000"), "yfinance"),
        ("NVDA", TUESDAY, Decimal("185.00000000"), "yfinance"),
    ]


def test_a_ticker_with_no_close_is_reported_missing_not_failed(
    migrated_engine: AsyncEngine,
) -> None:
    _listed(migrated_engine, "NVDA", "SPAC")

    report = _run(migrated_engine, FakeProvider({"NVDA": "181.5"}))

    assert report.missing == ["SPAC"]
    assert report.failed == {}
    assert report.latest == TUESDAY


# --- containing failure ------------------------------------------------------------


def test_a_ticker_that_breaks_its_batch_fails_alone(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch is asked again a ticker at a time, so its other tickers are
    written, and so is every other batch."""
    monkeypatch.setattr(quotes_module, "BATCH", 2)
    _listed(migrated_engine, "AAPL", "BAD", "MSFT", "NVDA")
    provider = FakeProvider({"AAPL": "250", "BAD": "1", "MSFT": "500", "NVDA": "181.5"})
    provider.breaking = {"BAD"}

    report = _run(migrated_engine, provider)

    assert provider.calls == [["AAPL", "BAD"], ["AAPL"], ["BAD"], ["MSFT", "NVDA"]]
    assert list(report.failed) == ["BAD"]
    assert "Yahoo choked" in report.failed["BAD"]
    assert report.missing == []
    assert [ticker for ticker, *_ in _quotes(migrated_engine)] == ["AAPL", "MSFT", "NVDA"]


def test_a_provider_that_keeps_failing_stops_the_run_and_keeps_what_it_wrote(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quotes_module, "BATCH", 2)
    failing = [f"F{n:02d}" for n in range(MAX_CONSECUTIVE_FAILURES + 2)]
    _listed(migrated_engine, "AAA", "AAB", *failing)
    provider = FakeProvider({"AAA": "1", "AAB": "2"})
    provider.breaking = set(failing)

    with pytest.raises(QuoteFetchError, match=f"{MAX_CONSECUTIVE_FAILURES} tickers failed"):
        _run(migrated_engine, provider)

    assert [ticker for ticker, *_ in _quotes(migrated_engine)] == ["AAA", "AAB"]
    assert "F11" not in provider.asked


def test_no_close_for_any_ticker_is_the_provider_failing(migrated_engine: AsyncEngine) -> None:
    """What a scraper looks like the day the page it scrapes changes."""
    _listed(migrated_engine, "AAPL", "NVDA")

    with pytest.raises(QuoteFetchError, match="no close for any of the 2 tickers"):
        _run(migrated_engine, FakeProvider({}))


# --- the command -----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def yfinance_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Settings]:
    """The container's DSN with QUOTE_PROVIDER=yfinance; see test_cli_ingest_filing."""
    configured = settings.model_copy(update={"quote_provider": "yfinance"})
    monkeypatch.setattr("app.cli.get_settings", lambda: configured)
    yield configured
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def test_the_command_reports_each_outcome_records_a_partial_run_and_drops_the_cache(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    yfinance_settings: Settings,
) -> None:
    _securities(
        migrated_engine,
        {"ticker": "NVDA"},
        {"ticker": "BAD"},
        {"ticker": "SPAC"},
        {"ticker": "APG"},
        {"ticker": "APG"},
    )
    provider = FakeProvider({"NVDA": "181.5", "BAD": "1"})
    provider.breaking = {"BAD"}
    monkeypatch.setattr("app.jobs.enrichment.quote_provider", lambda settings: provider)
    redis = FakeRedis()
    redis.values = {f"{KEY_PREFIX}stocks.get_stock:0f0f": "{}"}
    monkeypatch.setattr("app.jobs.cache.create_redis", lambda settings: redis)

    result = runner.invoke(app, ["fetch-quotes"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("fetch-quotes  yfinance: 3 asked of 5 listed securities")
    assert "written     1 close, latest 2026-10-13" in result.stdout
    assert "no close    1, delisted or unknown to yfinance: SPAC" in result.stdout
    assert "failed      1: BAD" in result.stdout
    assert "ambiguous   1 ticker two listed securities claim, not asked: APG" in result.stdout
    assert redis.values == {}
    [(status, seen, written, error, metrics)] = _fetch(
        migrated_engine,
        select(
            IngestionRun.status,
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.error,
            IngestionRun.metrics,
        ).where(IngestionRun.job_name == "fetch-quotes"),
    )
    assert (status, seen, written) == ("partial", 3, 1)
    assert error.startswith("BAD: RuntimeError: Yahoo choked")
    assert metrics["cache_invalidated"] == 1
    assert metrics["latest"] == "2026-10-13"


def test_a_provider_that_fails_is_exit_1_and_a_failed_run(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    yfinance_settings: Settings,
) -> None:
    _listed(migrated_engine, "NVDA")
    monkeypatch.setattr("app.jobs.enrichment.quote_provider", lambda settings: FakeProvider({}))

    result = runner.invoke(app, ["fetch-quotes"])

    assert result.exit_code == 1
    assert "error: yfinance had no close for any of the 1 tickers" in result.stderr
    [(status,)] = _fetch(
        migrated_engine,
        select(IngestionRun.status).where(IngestionRun.job_name == "fetch-quotes"),
    )
    assert status == "failed"


def test_yfinance_not_installed_is_exit_1_naming_the_group(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    yfinance_settings: Settings,
) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", None)

    result = runner.invoke(app, ["fetch-quotes"])

    assert result.exit_code == 1
    assert "uv sync --group quotes" in result.stderr
