"""What a security was worth at a quarter end, as the managers who held it reported.

A 13F line carries a value and a share count, and the value is the position's
market value on the period's last day. So every line is an observation of one
price, the quarter-end close, by one filer. Forty managers holding a stock are
forty observations of the same number, and their agreement is how sure we can
be of it. This is the third table in the derived layer, rebuilt from
``position_snapshot`` by
:func:`~app.derived.implied_price.recompute_implied_price`, in the same
transaction as the snapshot. No market data vendor is involved, which is the
point: nothing here needs a licence to publish.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: A price per share. Eight places, because a value in whole dollars over a
#: large share count is a small number: the cheapest observation in the
#: five-year backfill is $0.009. Twelve integer digits clear BRK.A many times.
PRICE = Numeric(20, 8)


class PriceConfidence(StrEnum):
    """How far the observations behind an implied price agree with it.

    Text with a ``CHECK``, by the rule in :mod:`app.db.models.enums`. Judged on
    the priced observations, those worth more than nothing, and on how many of
    them are within :data:`~app.derived.implied_price.AGREEMENT_PCT` of the
    price.

    ``high``
        Three or more agree, and they are a majority.
    ``medium``
        Two agree, and they are a majority: two holders, or two of three.
    ``low``
        One holder, or no majority agreeing. The price is still the median,
        and still the best guess there is, but one filer reporting in
        thousands, or a stale share count across a split, could be all of it.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


CONFIDENCE_CHECK = "confidence IN ({})".format(
    ", ".join(f"'{level.value}'" for level in PriceConfidence)
)

#: A price with no confidence, or a confidence with no price, is a row whose two
#: halves disagree about whether there is an estimate at all.
PRICED_CHECK = "(price IS NULL) = (confidence IS NULL)"


class ImpliedPrice(Base):
    """One security's quarter-end price in one period, implied by its holders' filings.

    **Every** ``position_snapshot`` **row with shares is one observation**, of
    ``value_usd / shares``, and counts toward exactly one row here: its own
    security's in its own period. Rows with no shares imply nothing, since
    there is nothing to divide by.

    **The price is the median observation**, the middle one, or the lower of
    the middle two. A median rather than ``SUM(value) / SUM(shares)``, because
    the observations that disagree are not noise around the price but
    mistakes: a filer reporting in thousands after 2023 (a thousandth of the
    price), a share count from before a split (the price times the ratio), a
    strategic stake carried at book value. Any of them, if large, drags a
    weighted average wherever it likes. The median ignores a minority of them,
    however large. Always one of the observations, never an average of two.

    **An observation worth nothing is counted, not priced.** A position
    reported at $0 is a filer saying it could not or did not value it, not that
    the stock was free. :attr:`unpriced_count` says how many, and the median is
    over the rest. A security whose every holder reported $0 has a row here
    with no price.
    """

    __tablename__ = "implied_price"

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE, as a cache: the snapshot rows it was computed from already
        # hold the security with RESTRICT.
        ForeignKey("security.id", ondelete="CASCADE"),
        primary_key=True,
    )

    period_of_report: Mapped[date] = mapped_column(primary_key=True)
    """The quarter end the price is for."""

    price: Mapped[Decimal | None] = mapped_column(PRICE)
    """The median priced observation, rounded to eight places.

    Null when no observation is priced, and when the median rounds to nothing,
    which it would at under half a millionth of a cent. Never zero or
    negative: a price of nothing is no price."""

    observation_count: Mapped[int] = mapped_column()
    """``position_snapshot`` rows for the security in the period with shares:
    one per filer that held it."""

    unpriced_count: Mapped[int] = mapped_column()
    """Of :attr:`observation_count`, those worth $0, which the median leaves out."""

    agreeing_count: Mapped[int] = mapped_column()
    """Priced observations within
    :data:`~app.derived.implied_price.AGREEMENT_PCT` of the unrounded median.
    At least one when there is a price, since the median agrees with itself."""

    confidence: Mapped[str | None] = mapped_column(Text)
    """One :class:`PriceConfidence` value, null exactly when :attr:`price` is.
    ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    suspect: Mapped[bool] = mapped_column()
    """Whether any observation is from a period published with a suspect
    filing. Always false unless the snapshot was rebuilt with
    ``--include-suspect``."""

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    """When the rebuild that wrote this row ran: the same transaction as the
    snapshot it was computed from."""

    __table_args__ = (
        # The per-security series (a stock's prices over time) reads the
        # primary key, which leads with the security. Nothing reads one period
        # across every security yet, so there is no index for it.
        CheckConstraint("price > 0", name="price_is_positive"),
        CheckConstraint(PRICED_CHECK, name="priced_when_confident"),
        CheckConstraint(CONFIDENCE_CHECK, name="confidence_is_known"),
        CheckConstraint(
            "observation_count > 0"
            " AND unpriced_count BETWEEN 0 AND observation_count"
            " AND agreeing_count BETWEEN 0 AND observation_count - unpriced_count",
            name="counts_are_consistent",
        ),
    )
