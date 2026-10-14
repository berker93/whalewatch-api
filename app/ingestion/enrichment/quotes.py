"""Latest closing prices, from a quote provider that is off by default.

The prices this codebase publishes are implied ones (:mod:`app.derived.implied_price`):
what the managers' own filings say a security was worth at a quarter end. They
need no licence, because the SEC published them. A quote is different. It is a
vendor's data, under the vendor's terms, and it is the only market data here.
It answers one question the filings cannot: what has the stock done since?

::

    provider = quote_provider(settings)
    report = await fetch_quotes(sessions, provider)

The providers
-------------
:class:`QuoteProvider` is the whole interface: the latest close for some
tickers. ``QUOTE_PROVIDER`` picks one (:func:`quote_provider`).

``null`` (:class:`NullQuoteProvider`)
    The default, and production's. Knows no quotes, so ``security_quote`` stays
    empty and anything built on it shows nothing rather than something
    unlicensed. Every screen works without it.
``yfinance`` (:class:`YFinanceQuoteProvider`)
    Scrapes Yahoo, for development. Yahoo's data is not licensed for
    redistribution, so it must never reach production. Three things keep it
    out: ``Settings`` refuses it there, the provider refuses to be constructed
    there, and the package is in the ``quotes`` dependency group, which the
    image does not install (``uv sync --group quotes`` on a laptop). A paid
    vendor, when there is one, is another class with the same method.

Never a ``PriceProvider`` with daily bars: a daily history existed to price
cost bases, and the implied quarter-end price does that from the filings.

What gets quoted
----------------
Securities with a ticker that are still listed: ``security.delisted_by`` unset,
since a delisted security's last ticker may already belong to someone else. A
ticker two listed securities claim at once is not asked about at all: the close
would be written to both, and one of them is wrong. ``cusip-coverage`` lists
those collisions.

One bad ticker does not stop the rest
-------------------------------------
Tickers are asked in batches of :data:`BATCH`. A ticker the provider has no
close for is missing from its answer, which is a normal outcome (a recent
delisting the 13(f) list has not caught up with, say) and reported as such. A
batch that raises is asked again one ticker at a time, so a ticker that breaks
the provider fails alone. When :data:`MAX_CONSECUTIVE_FAILURES` single tickers
fail in a row, it is the provider that is broken, not the tickers, and the run
stops with :class:`QuoteFetchError`. So does a run that asked about tickers and
got no close for any of them, which is what a scraper looks like the day the
page it scrapes changes. Each batch's closes are committed as they arrive, so a
run that stops keeps what it had.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from itertools import batched
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.tickers import normalise_ticker
from app.db.models.security import Security
from app.db.models.security_quote import QuoteSource, SecurityQuote

logger = get_logger(__name__)

#: Tickers per call to the provider. yfinance fetches a batch on its own
#: threads; two hundred keeps one call to seconds, and a failed batch to two
#: hundred single retries.
BATCH: Final = 200

#: Single tickers failing in a row before the run concludes that the provider,
#: not the tickers, is what is broken.
MAX_CONSECUTIVE_FAILURES: Final = 10

#: US equities close at 16:00 New York time. A daily bar dated today is the
#: day's close only some time after that; before it, it is the price so far.
MARKET_TIMEZONE: Final = ZoneInfo("America/New_York")
CLOSE_SETTLED: Final = time(16, 30)

#: Places a close is kept to. Yahoo's numbers are floats that went through a
#: float32 somewhere (516.570007); four places is a sub-penny stock's tick.
_CLOSE_PLACES: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Quote:
    """A ticker's latest close: the price, and the trading day it is from."""

    ticker: str
    """As asked: this codebase's spelling (``BRK.B``), whatever the provider's."""

    date: date
    close: Decimal


class QuoteProvider(Protocol):
    """Somewhere latest closes come from."""

    @property
    def name(self) -> str:
        """What ``security_quote.source`` records: a
        :class:`~app.db.models.security_quote.QuoteSource` value."""
        ...

    async def latest_close(self, tickers: list[str]) -> dict[str, Quote]:
        """The latest close of each ticker the provider has one for, keyed as asked.

        A ticker it has none for is absent, not an error. Raises only when the
        request as a whole failed.
        """
        ...


class QuoteProviderUnavailableError(Exception):
    """The configured provider cannot be used here: refused, or not installed."""


class QuoteFetchError(Exception):
    """The provider, not a ticker, failed: the run stopped."""


class NullQuoteProvider:
    """The default: knows no quotes. Production runs this."""

    name = "null"

    async def latest_close(self, tickers: list[str]) -> dict[str, Quote]:
        return {}


class YFinanceQuoteProvider:
    """Yahoo's latest closes, through yfinance. Development only.

    :raises QuoteProviderUnavailableError: In production, before anything is
        imported, and anywhere yfinance is not installed.
    """

    name = QuoteSource.YFINANCE.value

    def __init__(self, settings: Settings) -> None:
        if settings.environment == "production":
            raise QuoteProviderUnavailableError(
                "yfinance is for development: Yahoo's data is not licensed for "
                "redistribution, and production runs QUOTE_PROVIDER=null"
            )
        try:
            # Imported here and nowhere else, so that nothing on the production
            # path can import it, and the package can be absent from the image.
            import yfinance
        except ImportError as missing:
            raise QuoteProviderUnavailableError(
                "QUOTE_PROVIDER=yfinance, but yfinance is not installed. It is in the "
                "'quotes' dependency group: uv sync --group quotes"
            ) from missing
        # A ticker with no data is an answer, and fetch_quotes reports it. Left
        # alone, yfinance also logs one ERROR line per such ticker.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        self._yfinance: Any = yfinance

    async def latest_close(self, tickers: list[str]) -> dict[str, Quote]:
        symbols = {yahoo_symbol(ticker): ticker for ticker in tickers}
        # yfinance is synchronous: a thread, not the event loop.
        frame = await asyncio.to_thread(self._download, list(symbols))
        now = datetime.now(MARKET_TIMEZONE)
        quotes: dict[str, Quote] = {}
        for symbol, ticker in symbols.items():
            close = last_close(frame, symbol, now=now)
            if close is not None:
                quotes[ticker] = Quote(ticker, *close)
        return quotes

    def _download(self, symbols: list[str]) -> Any:
        return self._yfinance.download(
            symbols,
            # The last week's daily bars: a long weekend with a holiday in it
            # still has a close in it, and a security that has not traded for
            # longer has no latest close worth the name.
            period="5d",
            interval="1d",
            # Closes as traded, not adjusted for dividends.
            auto_adjust=False,
            group_by="ticker",
            multi_level_index=True,
            threads=True,
            progress=False,
        )


def yahoo_symbol(ticker: str) -> str:
    """Yahoo writes a share class after a hyphen: ``BRK.B`` is ``BRK-B``."""
    return ticker.replace(".", "-")


def last_close(frame: Any, symbol: str, *, now: datetime) -> tuple[date, Decimal] | None:
    """The latest settled close of ``symbol`` in a ``yfinance.download`` frame.

    The frame's columns are ``(symbol, field)``, and a symbol Yahoo had nothing
    for is all NaN, or not there at all. A bar dated today is skipped until
    :data:`CLOSE_SETTLED`, since until then its close is the price so far.
    """
    if frame is None or symbol not in frame.columns.get_level_values(0):
        return None
    closes = frame[symbol]["Close"].dropna()
    for stamp, value in reversed(list(closes.items())):
        day = stamp.date()
        if day >= now.date() and now.time() < CLOSE_SETTLED:
            continue
        price = float(value)
        if not math.isfinite(price) or price <= 0:
            return None
        return day, Decimal(repr(price)).quantize(_CLOSE_PLACES)
    return None


def quote_provider(settings: Settings) -> QuoteProvider:
    """The provider ``QUOTE_PROVIDER`` names. ``null`` unless set."""
    match settings.quote_provider:
        case "null":
            return NullQuoteProvider()
        case "yfinance":
            return YFinanceQuoteProvider(settings)


# --- fetching --------------------------------------------------------------------


@dataclass(slots=True)
class QuoteReport:
    """What one ``fetch-quotes`` run asked about, and what came back."""

    provider: str
    listed: int = 0
    """Securities with a ticker that are still listed: the candidates."""

    asked: int = 0
    """Tickers asked about. The listed ones, less the ambiguous ones, or the
    ones named with ``--ticker``. None under the null provider."""

    written: int = 0
    """Closes written: one per ticker that had one."""

    latest: date | None = None
    """The latest trading day a close was for."""

    missing: list[str] = field(default_factory=list)
    """Tickers the provider had no close for."""

    failed: dict[str, str] = field(default_factory=dict)
    """Tickers that broke the provider when asked alone, with why."""

    ambiguous: list[str] = field(default_factory=list)
    """Tickers two listed securities claim, not asked about."""

    unknown: list[str] = field(default_factory=list)
    """Tickers named with ``--ticker`` that no listed security has."""


async def fetch_quotes(
    sessions: async_sessionmaker[AsyncSession],
    provider: QuoteProvider,
    *,
    tickers: Sequence[str] | None = None,
) -> QuoteReport:
    """Fetch and write the latest close of every listed security, or of ``tickers``.

    Each batch's closes are committed as they arrive. Idempotent: a re-run
    replaces the closes of the trading days it fetches again.

    :raises QuoteFetchError: When the provider, not a ticker, failed. What was
        written before it did is kept.
    """
    report = QuoteReport(provider=provider.name)
    async with sessions() as session:
        listed = await listed_tickers(session)
    report.listed = sum(len(ids) for ids in listed.values())
    report.ambiguous = sorted(ticker for ticker, ids in listed.items() if len(ids) > 1)
    securities = {ticker: ids[0] for ticker, ids in listed.items() if len(ids) == 1}

    if tickers is not None:
        wanted = list(dict.fromkeys(normalise_ticker(ticker) for ticker in tickers))
        report.unknown = [ticker for ticker in wanted if ticker not in listed]
        report.ambiguous = [ticker for ticker in report.ambiguous if ticker in wanted]
        securities = {ticker: securities[ticker] for ticker in wanted if ticker in securities}

    # Nothing to ask a provider that knows nothing, and every ticker reported
    # as missing would read as a provider that failed.
    if isinstance(provider, NullQuoteProvider):
        return report

    asked = sorted(securities)
    report.asked = len(asked)
    failing = _FailureStreak()
    for batch in batched(asked, BATCH):
        quotes = await _ask(provider, list(batch), report, failing)
        report.missing += [
            ticker for ticker in batch if ticker not in quotes and ticker not in report.failed
        ]
        if quotes:
            async with sessions() as session, session.begin():
                await _write(session, provider, quotes, securities)
            report.written += len(quotes)
            latest = max(quote.date for quote in quotes.values())
            report.latest = max(latest, report.latest or latest)
        logger.info(
            "quotes.batch_fetched",
            asked=len(batch),
            written=len(quotes),
            failed=len(report.failed),
        )

    if report.asked and not report.written:
        raise QuoteFetchError(
            f"{provider.name} had no close for any of the {report.asked:,} tickers asked about"
        )
    return report


async def listed_tickers(session: AsyncSession) -> dict[str, list[int]]:
    """Every listed security's ticker, with the securities that have it: one,
    unless two collide."""
    rows = await session.execute(
        select(func.upper(Security.ticker), Security.id)
        .where(Security.ticker.is_not(None), Security.delisted_by.is_(None))
        .order_by(Security.id)
    )
    listed: dict[str, list[int]] = {}
    for ticker, security_id in rows:
        listed.setdefault(ticker, []).append(security_id)
    return listed


@dataclass(slots=True)
class _FailureStreak:
    count: int = 0

    def failed(self, failure: Exception) -> None:
        self.count += 1
        if self.count >= MAX_CONSECUTIVE_FAILURES:
            raise QuoteFetchError(
                f"{self.count} tickers failed in a row; the last with "
                f"{type(failure).__name__}: {failure}"
            ) from failure

    def answered(self) -> None:
        self.count = 0


async def _ask(
    provider: QuoteProvider, batch: list[str], report: QuoteReport, failing: _FailureStreak
) -> dict[str, Quote]:
    """The batch's closes, asking one ticker at a time if the batch fails."""
    try:
        answer = await provider.latest_close(batch)
    except Exception as failure:  # one ticker may break a batch: isolate it
        logger.warning(
            "quotes.batch_failed",
            tickers=len(batch),
            error=f"{type(failure).__name__}: {failure}",
        )
    else:
        failing.answered()
        return _valid(answer, batch, report)
    quotes: dict[str, Quote] = {}
    for ticker in batch:
        try:
            answer = await provider.latest_close([ticker])
        except Exception as failure:
            report.failed[ticker] = f"{type(failure).__name__}: {failure}"
            logger.warning("quotes.ticker_failed", ticker=ticker, error=report.failed[ticker])
            failing.failed(failure)
            continue
        failing.answered()
        quotes |= _valid(answer, [ticker], report)
    return quotes


def _valid(answer: Mapping[str, Quote], asked: list[str], report: QuoteReport) -> dict[str, Quote]:
    """The closes that answer the tickers asked, and are prices at all."""
    quotes: dict[str, Quote] = {}
    for ticker in asked:
        quote = answer.get(ticker)
        if quote is None:
            continue
        if quote.close > 0:
            quotes[ticker] = quote
        else:
            report.failed[ticker] = f"a close of {quote.close} on {quote.date}"
    return quotes


async def _write(
    session: AsyncSession,
    provider: QuoteProvider,
    quotes: Mapping[str, Quote],
    securities: Mapping[str, int],
) -> None:
    statement = insert(SecurityQuote).values(
        [
            {
                "security_id": securities[ticker],
                "date": quote.date,
                "close": quote.close,
                "ticker": ticker,
                "source": provider.name,
            }
            for ticker, quote in quotes.items()
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SecurityQuote.security_id, SecurityQuote.date],
            set_={
                "close": statement.excluded.close,
                "ticker": statement.excluded.ticker,
                "source": statement.excluded.source,
                "fetched_at": func.now(),
            },
        )
    )
