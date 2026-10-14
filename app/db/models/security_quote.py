"""The latest closing price of a security, from a quote provider: market data.

Everything else this codebase publishes is derived from SEC filings, which is
what makes it publishable. A quote is not. It comes from a vendor, under the
vendor's licence, and the only one wired up (yfinance) is a scraper of Yahoo
that nothing public may redistribute. So the provider is off by default
(``QUOTE_PROVIDER=null``) and production refuses the scraper, and this table
is empty wherever that holds. Anything that reads it must treat no row as the
normal case: the price a filer reported is ``implied_price``, and this only
says what the stock has done since.

Written by ``fetch-quotes``
(:func:`~app.ingestion.enrichment.quotes.fetch_quotes`), one row per security
per trading day it was run on. Not partitioned: a few thousand listed
securities, one row each per run.
"""

# ``date`` is a column here (06c's key is ``(security_id, date)``), and a class
# attribute named ``date`` would shadow the type in its own annotation.
import datetime as dt
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.implied_price import PRICE


class QuoteSource(StrEnum):
    """Values of :attr:`SecurityQuote.source`: the provider that gave the close.

    Text with a ``CHECK``, by the rule in :mod:`app.db.models.enums`. The null
    provider is not one: it gives nothing, so nothing is ever written under it.
    """

    YFINANCE = "yfinance"


QUOTE_SOURCE_CHECK = "source IN ({})".format(
    ", ".join(f"'{source.value}'" for source in QuoteSource)
)


class SecurityQuote(Base):
    """One security's closing price on one trading day."""

    __tablename__ = "security_quote"

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE: a quote for a security that no longer exists is for nothing.
        ForeignKey("security.id", ondelete="CASCADE"),
        primary_key=True,
    )

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    """The trading day the close is from, as the provider dated it: not the day
    it was fetched, which can be a weekend."""

    close: Mapped[Decimal] = mapped_column(PRICE)
    """The day's closing price per share, in dollars, as traded that day.
    Providers rewrite past closes for every later split, but only the latest
    close is ever fetched, and a split after :attr:`date` leaves this row as
    it was. Comparing it with an older price is SEC-7's job, through
    ``security_split``."""

    ticker: Mapped[str] = mapped_column(Text)
    """The ticker it was quoted under, which was ``security.ticker`` on the day
    it was fetched. Kept because tickers change, and a wrong close is found
    again by the ticker it was asked for."""

    source: Mapped[str] = mapped_column(Text)
    """One :class:`QuoteSource` value. ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    """When the close was last fetched. A re-run on the same trading day
    replaces the row, and this with it."""

    __table_args__ = (
        CheckConstraint("close > 0", name="close_is_positive"),
        CheckConstraint("ticker <> ''", name="ticker_is_not_empty"),
        CheckConstraint(QUOTE_SOURCE_CHECK, name="source_is_known"),
    )
