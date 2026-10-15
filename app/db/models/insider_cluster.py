"""Cluster buys: several of one company's insiders buying on the open market at about the same time.

One insider buying says something about one person. Three officers and
directors of the same company buying within a month, each with their own money,
is the insider signal with the most research behind it. This table holds every
such episode, past and present, read off ``insider_transaction``
(:mod:`app.derived.insider_cluster`).

**An episode, not a window.** Detection slides a window of
:attr:`InsiderCluster.window_days` along each issuer's purchases, and every
window holding at least :attr:`InsiderCluster.min_insiders` distinct insiders
qualifies. Qualifying windows that share a purchase are one episode, so a
month of buying by five directors is one row, not one row per day of it.

**One row per parameter set.** The API's ``?window=`` and ``?min_insiders=``
choose from a fixed grid, and every point of it is stored, so a request is a
read, never a detection. Episodes for different parameters overlap and nest:
an episode of four insiders in 30 days is inside one of three.

Derived, like ``security_split``: rebuilt whole by ``detect-clusters``, which
``ingest-form4`` runs after every load, in one transaction.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY, QUANTITY


class InsiderCluster(Base):
    """One episode of cluster buying at one issuer, for one window and threshold."""

    __tablename__ = "insider_cluster"

    window_days: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    """How far apart, in days, the purchases of one qualifying window can be:
    30 means a purchase and every other purchase up to 30 days before it."""

    min_insiders: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    """How many distinct insiders a window needs to qualify."""

    issuer_cik: Mapped[str] = mapped_column(CHAR(10), primary_key=True)
    """The company, by the CIK the Form 4s were filed under. Clusters are found
    per issuer, not per security: insiders are insiders of a company, and a
    director buying the A shares and another buying the B shares are two of
    the same company's insiders buying."""

    window_start: Mapped[date] = mapped_column(Date, primary_key=True)
    """The first trade day of the episode's first qualifying window."""

    window_end: Mapped[date] = mapped_column(Date)
    """The last trade day of the episode's last qualifying window. Unique per
    issuer and parameter set, since an issuer's episodes do not overlap."""

    issuer_name: Mapped[str] = mapped_column(Text)
    """``issuerName`` as the latest-filed of the episode's lines spells it."""

    security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # SET NULL, as on insider_transaction: the link is our reading of it.
        ForeignKey("security.id", ondelete="SET NULL"),
    )
    """The security every line of the episode is linked to. Null when any is
    unlinked, or they are linked to more than one of the issuer's classes."""

    insider_count: Mapped[int] = mapped_column(Integer)
    """Distinct insiders who bought during the episode. An insider who bought
    on three days is one."""

    transaction_count: Mapped[int] = mapped_column(Integer)
    """The lines counted: one per purchase as filed."""

    total_shares: Mapped[Decimal | None] = mapped_column(QUANTITY)

    total_value: Mapped[Decimal | None] = mapped_column(MONEY)
    """Dollars paid, the sum of the lines' ``value_usd``. Null when no line has
    a price; lines without one add nothing to it."""

    transaction_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    """The ``insider_transaction.id`` of every line counted, oldest trade first.

    No foreign key can hold an array, and none is needed: the table is rebuilt
    whenever the lines change. Look them up with ``transaction_date`` between
    :attr:`window_start` and :attr:`window_end` as well as the id, so the
    lookup is pruned to the partitions of those years."""

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The listing: one parameter set's episodes, latest first, in the order
        # it pages, (window_end, issuer_cik) being unique within the set.
        Index(
            "ix_insider_cluster_window_end",
            "window_days",
            "min_insiders",
            desc("window_end"),
            desc("issuer_cik"),
        ),
        CheckConstraint("window_days > 0", name="window_days_is_positive"),
        CheckConstraint("min_insiders >= 2", name="min_insiders_is_a_cluster"),
        CheckConstraint("window_end >= window_start", name="window_is_ordered"),
        CheckConstraint(
            "insider_count >= min_insiders AND transaction_count >= insider_count",
            name="counts_are_consistent",
        ),
        CheckConstraint(
            "cardinality(transaction_ids) = transaction_count",
            name="transaction_ids_are_counted",
        ),
    )
