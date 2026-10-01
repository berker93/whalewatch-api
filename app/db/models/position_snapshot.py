"""The published portfolio: one row per security, per filer, per period.

The first table in what the data model calls the derived layer — "a cache with
a schema". One function writes it,
:func:`~app.derived.position_snapshot.recompute_position_snapshot`, which
replaces the rows of a set of ``(filer, period)`` pairs from ``holding``
through ``effective_filing``; nothing else may depend on it for correctness,
because everything in it can be rebuilt from the layer above.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY, QUANTITY

# A position's share of its period's value, in percent. Six places so that
# rounding cannot move a period's sum by a hundredth of a percent until the
# period holds 20,000 positions, which no 13F in the universe comes near.
WEIGHT_PCT = Numeric(9, 6)


class PositionSnapshot(Base):
    """One security in one filer's portfolio for one period, as published.

    ``holding`` is one line of one filing; this is the position once the period
    has been resolved. Every filing that counts toward the ``(filer, period)``
    — the original or its latest restatement, the new-holdings amendments after
    it, each CIK's under a ``sum`` overlap policy — summed per security. The
    per-filer read path serves this, so it never has to know about amendments.

    **Common stock only.** Option lines and ``PRN`` principal amounts are not
    rows here. An option's value is the notional of the underlying and a
    principal amount is not a count of shares, so neither can be added to a
    share count or be a share of the portfolio. Leaving them out is also what
    makes ``(filer, period, security)`` a key. One filing can report a CUSIP as
    stock, calls, puts and principal, but only once as stock.

    **A period with a suspect filing is not here** unless the snapshot was
    rebuilt with ``--include-suspect``, and then every one of its rows says so
    in :attr:`suspect`. This is the one place in the schema that withholds data
    rather than flagging it. ``filing`` keeps everything, and
    ``GET /filings/{accession_no}`` still serves a suspect filing, but what gets
    *published* waits for someone to look. ``check-data`` lists every period
    that is waiting.
    """

    __tablename__ = "position_snapshot"

    filer_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE, unlike holding's RESTRICT: this is a cache of the filer's
        # holdings, and a cache has no business keeping its subject alive.
        ForeignKey("filer.id", ondelete="CASCADE"),
        primary_key=True,
    )

    period_of_report: Mapped[date] = mapped_column(primary_key=True)

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("security.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    shares: Mapped[Decimal] = mapped_column(QUANTITY)
    """Summed over every line of the period's filings that holds the security."""

    value_usd: Mapped[Decimal] = mapped_column(MONEY)
    """Whole dollars, summed like :attr:`shares`."""

    weight_pct: Mapped[Decimal | None] = mapped_column(WEIGHT_PCT)
    """:attr:`value_usd` as a percentage of the period's total.

    A period's weights sum to 100, give or take the rounding to six places.
    Null on every row of a period whose positions are all worth nothing, where
    there is no total to divide by.
    """

    source_filing_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE for filer_id's reason: deleting a filing changes what the
        # snapshot should say, and only a recompute can say it.
        ForeignKey("filing.id", ondelete="CASCADE"),
    )
    """The filing this position was read from.

    One filing for nearly every row. When more than one of the period's filings
    holds the security, as with an original and the new-holdings amendment
    that added to it, or two CIKs under a ``sum`` policy, this is the
    latest-filed of them. That filing is the one that last changed the number,
    so it is the one that explains a figure that no longer matches the
    original. The rest of the period is in ``effective_filing``.
    """

    suspect: Mapped[bool] = mapped_column()
    """Whether a filing counted toward this row's period is ``suspect``.

    Always false in a snapshot built the default way, which leaves those periods
    out. True only after ``recompute --include-suspect``, and on every row of
    such a period. The flag is per period rather than per filing, because the
    period is what was published on the strength of a filing nobody checked.
    """

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    """When the rebuild that wrote this row ran. One value per rebuild, since
    ``now()`` is the transaction's start."""

    __table_args__ = (
        CheckConstraint("weight_pct >= 0 AND weight_pct <= 100", name="weight_pct_is_a_percentage"),
        # The latest period (0017): max(period_of_report) reads one entry of
        # this instead of every row. Who holds a stock in a period uses both
        # columns. Period first for the max, which the other order cannot serve.
        Index(
            "ix_position_snapshot_period_of_report_security_id",
            "period_of_report",
            "security_id",
        ),
    )
