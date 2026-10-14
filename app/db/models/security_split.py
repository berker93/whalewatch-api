"""Stock splits, read off the holders' filings: a share count that jumped by a common ratio.

A 10-for-1 split multiplies every holder's share count by ten and divides the
price by ten, and nobody traded. ``position_change`` compares share counts as
filed, so without this every holder of the stock shows an ``add`` of +900%.
This table says which ``(security, period)`` had a split since the quarter
before, and by how much, so that ``position_change`` can compare against the
previous shares times the ratio instead.

No corporate-action feed is involved, as for ``implied_price``. A split is
detected from what the holders filed: their share counts agree on one ratio,
and the implied price moved the other way by about as much
(:mod:`app.derived.security_split`). The fourth table in the derived layer,
rebuilt by ``recompute`` from ``position_snapshot`` and ``implied_price``, in
the same transaction, and by ``detect-splits``.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: A split ratio, as a decimal: 20 for 20-for-1, 0.1 for 1-for-10. Ten places,
#: so a 2-for-3 is 0.6666666667. Only for reading. Arithmetic on shares uses the
#: exact :attr:`SecuritySplit.numerator` and :attr:`SecuritySplit.denominator`.
SPLIT_RATIO = Numeric(20, 10)

#: The generated :attr:`SecuritySplit.ratio`, in SQL.
RATIO_EXPRESSION = "numerator::numeric / denominator"


class SplitConfidence(StrEnum):
    """How much evidence a detected split has, beyond what detecting it requires.

    Text with a ``CHECK``, by the rule in :mod:`app.db.models.enums`. Every
    detected split has at least two holders agreeing on the ratio and the
    price corroborating it. The two things that make it surer are more holders
    agreeing, and a holder whose shares are the previous count times the ratio
    to the share, which is a holder that did not trade.

    ``high``
        Three or more holders agree, and one of them is exact to the share.
    ``medium``
        Three or more agree, or one is exact.
    ``low``
        Two agree, and neither is exact. Only ever a ratio of 4-for-1 or more,
        or 1-for-4 or less, which ordinary trading does not imitate.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SPLIT_CONFIDENCE_CHECK = "confidence IN ({})".format(
    ", ".join(f"'{level.value}'" for level in SplitConfidence)
)


class SecuritySplit(Base):
    """A split of one security, dated the first period whose share counts are after it.

    ``period_of_report`` is the quarter end by which the split had happened,
    and the quarter before it is from before. NVDA's 10-for-1 of June 2024 is
    dated 2024-06-30. A change in ``position_change`` crosses the split when
    its previous period is before that date and its own period is on or after
    it, which also covers a filer that skipped the quarter.

    **The ratio is a fraction, stored exactly**: :attr:`numerator` new shares
    for :attr:`denominator` old. 3-for-2 is 3 and 2, 1-for-10 is 1 and 10. One
    of the fractions in
    :data:`~app.derived.security_split.SPLIT_FRACTIONS`, never anything
    measured. :attr:`ratio` is the same as a decimal, for reading.
    """

    __tablename__ = "security_split"

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE, as a cache, for implied_price's reason.
        ForeignKey("security.id", ondelete="CASCADE"),
        primary_key=True,
    )

    period_of_report: Mapped[date] = mapped_column(primary_key=True)
    """The first quarter end after the split."""

    numerator: Mapped[int] = mapped_column(Integer)
    """New shares per :attr:`denominator` old: 20 in 20-for-1, 1 in 1-for-10."""

    denominator: Mapped[int] = mapped_column(Integer)
    """Old shares per :attr:`numerator` new: 1 in 20-for-1, 10 in 1-for-10."""

    ratio: Mapped[Decimal] = mapped_column(SPLIT_RATIO, Computed(RATIO_EXPRESSION, persisted=True))
    """:attr:`numerator` over :attr:`denominator`: above 1 for a split, below
    1 for a reverse split. Generated, so it cannot disagree with them."""

    confidence: Mapped[str] = mapped_column(Text)
    """One :class:`SplitConfidence` value. ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    holders: Mapped[int] = mapped_column(Integer)
    """Filers with shares of the security in this quarter and the one before:
    the holders whose share counts can be compared at all."""

    holders_agreeing: Mapped[int] = mapped_column(Integer)
    """Of :attr:`holders`, those whose share count moved by the ratio, within
    :data:`~app.derived.security_split.AGREEMENT_TOLERANCE` either way. Most
    holders trade in a quarter, so few move by the ratio exactly."""

    holders_exact: Mapped[int] = mapped_column(Integer)
    """Of :attr:`holders_agreeing`, those whose shares are the previous count
    times the ratio to within a share: the ones that did not trade."""

    shares_ratio: Mapped[Decimal] = mapped_column(Numeric(28, 6))
    """The median holder's shares now over its shares a quarter before. The
    ratio is this, snapped to the nearest common fraction."""

    price_ratio: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    """The implied price now over the implied price a quarter before. Times
    :attr:`ratio`, it is one plus the stock's return over the quarter, which
    is how the price corroborates the split."""

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    """When the detection that wrote this row ran."""

    __table_args__ = (
        CheckConstraint(
            "numerator > 0 AND denominator > 0 AND numerator <> denominator",
            name="ratio_is_a_split",
        ),
        CheckConstraint(SPLIT_CONFIDENCE_CHECK, name="confidence_is_known"),
        CheckConstraint(
            "holders_exact BETWEEN 0 AND holders_agreeing AND holders_agreeing <= holders",
            name="holders_are_consistent",
        ),
    )
