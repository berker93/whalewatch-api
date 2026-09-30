"""The published portfolio: one row per position, per filer, per period.

The first table in what the data model calls the derived layer — "a cache with
a schema". One function writes it,
:func:`~app.derived.position_snapshot.recompute_position_snapshot`, which
replaces a filer's rows wholesale from ``holding`` through ``effective_filing``;
nothing else may depend on it for correctness, because everything in it can be
rebuilt from the layer above.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY, QUANTITY

# A position's share of its period's value. Six places is a ten-thousandth of a
# percent, which is finer than anything computed from it is ever shown at.
WEIGHT = Numeric(7, 6)


class PositionSnapshot(Base):
    """One position in one filer's portfolio for one period, as published.

    ``holding`` is one line of one filing; this is the position once the period
    has been resolved. Every filing that counts toward the ``(filer, period)``
    — the original or its latest restatement, the new-holdings amendments after
    it, each CIK's under a ``sum`` overlap policy — summed on the natural key.
    The per-filer read path serves this, so it never has to know about
    amendments.

    **A period with a suspect filing is not here** unless the snapshot was
    rebuilt with ``--include-suspect``, and then every one of its rows says so
    in :attr:`suspect`. This is the one place in the schema that withholds data
    rather than flagging it. ``filing`` keeps everything, and
    ``GET /filings/{accession_no}`` still serves a suspect filing, but what gets
    *published* waits for someone to look. ``check-data`` lists every period
    that is waiting.
    """

    __tablename__ = "position_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    filer_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE, unlike holding's RESTRICT: this is a cache of the filer's
        # holdings, and a cache has no business keeping its subject alive.
        ForeignKey("filer.id", ondelete="CASCADE"),
    )

    period_of_report: Mapped[date] = mapped_column()

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("security.id", ondelete="RESTRICT"),
    )

    cusip: Mapped[str] = mapped_column(CHAR(9))
    """As the filings wrote it. The natural key is on this, as ``holding``'s is."""

    put_call: Mapped[str | None] = mapped_column(Text)
    sshprnamt_type: Mapped[str] = mapped_column(Text)

    shares: Mapped[Decimal] = mapped_column(QUANTITY)
    """Summed over the period's filings. Never over ``PRN`` and ``SH`` together:
    they are different rows, because :attr:`sshprnamt_type` is in the key."""

    value_usd: Mapped[Decimal] = mapped_column(MONEY)
    """Whole dollars, summed like :attr:`shares`."""

    weight: Mapped[Decimal | None] = mapped_column(WEIGHT)
    """:attr:`value_usd` over the period's total, counting positions only.

    Null on an option line. Its value is the notional of the underlying, not a
    premium, so it cannot be a share of anything. For the same reason options
    are left out of the total, so the weights of a period's other rows sum to
    one. Null too for every row of a period whose positions are all worth
    nothing, where there is no total to divide by.
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
        # holding's natural key with the filing replaced by the period: one row
        # per position, however many filings the period resolved to. NULLS NOT
        # DISTINCT for the same reason as there — put_call is null on nearly
        # every row.
        UniqueConstraint(
            "filer_id",
            "period_of_report",
            "cusip",
            "put_call",
            "sshprnamt_type",
            name="uq_position_snapshot_filer_period_position",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("weight >= 0 AND weight <= 1", name="weight_is_a_fraction"),
        CheckConstraint("put_call IS NULL OR weight IS NULL", name="an_option_has_no_weight"),
    )
