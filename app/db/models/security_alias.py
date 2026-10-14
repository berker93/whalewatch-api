"""Every ticker a security has traded under, and when: the ticker history.

The CUSIP is the stable key; a ticker is a display label, and labels change.
Facebook's CUSIP traded as ``FB`` until 2022-06-09 and as ``META`` from then.
A ticker is also recycled once its company is delisted, so the same letters
can name one security in 2019 and another in 2024. Each row here says "this
security traded as this ticker over this range", and that is what lets

* ``/v1/stocks/{ticker}`` find the security a ticker meant *in the period
  being viewed* (:func:`app.db.queries.tickers.lookup_query`), and
* every row that belongs to a period show the ticker as it was then, with the
  current one beside it (:func:`app.db.queries.tickers.ticker_at`).

``security.ticker`` is the current ticker, or the last one a delisted security
had. It has an open-ended row here. Nothing inside the database joins on a
ticker: every join is on ``security_id``, and tickers appear only where a
request names one and where a response shows one.

Who writes it: ``resolve-cusips`` and ``resolve-cusip``, when a security gets
its first ticker or a new one (:mod:`app.ingestion.enrichment.tickers`). Former
tickers from before we resolved anything come from ``ticker-alias``, since no
source we read has them. Delisting (``security.delisted_by``) closes the open
row.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class SecurityAlias(Base):
    """One security, one ticker, one range of dates."""

    __tablename__ = "security_alias"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    security_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )
    """Cascades: a ticker of a security that no longer exists names nothing."""

    ticker: Mapped[str] = mapped_column(Text)
    """As stored in ``security.ticker``: upper case, a share class after a dot."""

    valid_from: Mapped[date | None] = mapped_column(Date)
    """The first day it traded as :attr:`ticker`. Null: as far back as we know."""

    valid_to: Mapped[date | None] = mapped_column(Date)
    """The first day it no longer did, so the range is ``[valid_from, valid_to)``.
    Null while it still does."""

    source: Mapped[str | None] = mapped_column(Text)
    """Where the ticker came from, for :attr:`Security.resolution_source`'s reason:
    a wrong one is found again by asking which others came from the same place."""

    note: Mapped[str | None] = mapped_column(Text)
    """Why, for a row a person entered."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("ticker <> ''", name="ticker_is_not_empty"),
        CheckConstraint(
            "source IS NULL OR source IN ('openfigi', '13f_column', 'manual')",
            name="source_is_known",
        ),
        CheckConstraint("valid_from < valid_to", name="valid_from_before_valid_to"),
        # One ticker at a time per security. Not across securities: a ticker
        # two securities hold at once is a collision to report, not refuse,
        # since either may be the stale one.
        ExcludeConstraint(
            ("security_id", "="),
            (text("daterange(valid_from, valid_to)"), "&&"),
            using="gist",
            name="ex_security_alias_one_ticker_at_a_time",
        ),
        # The lookup, an equality on upper(ticker), and the 404's suggestions,
        # a prefix of it. text_pattern_ops serves both under any collation.
        Index(
            "ix_security_alias_upper_ticker",
            func.upper(ticker).label("upper_ticker"),
            postgresql_ops={"upper_ticker": "text_pattern_ops"},
        ),
    )
