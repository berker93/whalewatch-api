"""What changed: one row per security, per filer, per period, against the period before.

The second table in the derived layer, and the one the product is for: a
portfolio says what a manager holds, and this says what they did. One function
writes it, :func:`~app.derived.position_change.recompute_position_change`,
which rebuilds it from ``position_snapshot`` in the same transaction as the
snapshot itself. Like the snapshot, nothing may depend on it for correctness.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY, QUANTITY
from app.db.models.position_snapshot import WEIGHT_PCT


class ChangeAction(StrEnum):
    """What a filer did to a position between its previous period and this one.

    Text with a ``CHECK``, by the rule in :mod:`app.db.models.enums`: the set is
    ours, and it has grown once already, by ``exit``.

    ``new``
        Not held in the filer's previous period, or the filer has no previous
        period. A stock sold one quarter and bought back the next is new again.
    ``add``
        More shares than before, by more than the hold band.
    ``trim``
        Fewer shares than before, by more than the hold band. Not a sale: the
        filer may have been assigned, distributed in kind, or moved the position
        to an affiliate that files separately.
    ``hold``
        The same shares as before, within
        :data:`~app.derived.position_change.HOLD_BAND_PCT` either way. Judged
        on shares alone, so a stock that doubled in price is still a hold.
    ``exit``
        Held in the filer's previous period and not in this one. The row is a
        position of nothing: no shares, no value, no weight, and the deltas
        are the whole previous position, negative. Only in a period the filer
        published, so a quarter it did not file, or one withheld, holds no
        exits: a missing filing is not a sale. Nor is an exit, for ``trim``'s
        reasons.
    """

    NEW = "new"
    ADD = "add"
    TRIM = "trim"
    HOLD = "hold"
    EXIT = "exit"


ACTION_CHECK = "action IN ({})".format(", ".join(f"'{action.value}'" for action in ChangeAction))

#: ``new`` means "no previous figures", and the previous figures being absent
#: means ``new``. Either half failing is a row whose action and numbers
#: disagree, and every reader believes one of the two.
NEW_CHECK = "(action = 'new') = (prev_shares IS NULL)"

#: An exit is a position of nothing. One way only: a position the filer still
#: lists at zero shares is not an exit, it is a filer listing zero shares.
EXIT_CHECK = "action <> 'exit' OR (shares = 0 AND value_usd = 0 AND weight_pct = 0)"

# A change in shares, in percent of the previous count. Falls are bounded at
# -100, and growth is not: one share to a million is +99,999,900%. 22 integer
# digits hold any change between two QUANTITY values, so no position, however
# strange, can overflow the column and fail the rebuild for every filer.
CHANGE_PCT = Numeric(28, 6)


class PositionChange(Base):
    """One security in one filer's period, against the filer's previous period.

    One row for every row of ``position_snapshot``, and one ``exit`` for every
    position the filer's previous period had and this one does not. The
    snapshot has no row for those, which is why this is a table rather than a
    query over it: an exit is the absence of a row, and an absence cannot be
    indexed. The previous period is the
    filer's previous *published* one, which is usually the calendar quarter
    before. Across a quarter with no filing, or one withheld for a suspect
    filing, it is the last period published before the gap.
    :attr:`prev_period_of_report` says which period it was.

    **The previous figures are null exactly when the action is** ``new``. That
    covers a position the filer did not hold in its previous period, and every
    position in the filer's first period, which has no previous period at all.
    :attr:`prev_period_of_report` tells those two apart.

    **The deltas count a new position from zero, and an exit down to it**, so
    they sum: a filer's ``shares_delta`` over every period up to this one is
    what it holds now, and the market-wide flow into a stock is a ``SUM`` that
    includes those who bought in fresh and those who sold out.
    """

    __tablename__ = "position_change"

    filer_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE for position_snapshot's reason: a cache has no business
        # keeping its subject alive.
        ForeignKey("filer.id", ondelete="CASCADE"),
        primary_key=True,
    )

    period_of_report: Mapped[date] = mapped_column(primary_key=True)

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("security.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    action: Mapped[str] = mapped_column(Text)
    """One :class:`ChangeAction` value. ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    shares: Mapped[Decimal] = mapped_column(QUANTITY)
    """This period's shares, as ``position_snapshot`` has them. Zero for an exit."""

    value_usd: Mapped[Decimal] = mapped_column(MONEY)
    """This period's value in whole dollars, as ``position_snapshot`` has it.
    Zero for an exit."""

    weight_pct: Mapped[Decimal | None] = mapped_column(WEIGHT_PCT)
    """This period's weight, as ``position_snapshot`` has it: null only in a
    period whose positions are all worth nothing. Zero for an exit."""

    prev_period_of_report: Mapped[date | None] = mapped_column()
    """The filer's previous published period: what this row is compared against.

    Set on a ``new`` row too, where it is the period the security was not held
    in, and on an ``exit``, where it is the last period it was. Null only in
    the filer's first period, which "opened every position" would misdescribe.
    It is the first period we have, not necessarily the first the filer filed.
    """

    prev_shares: Mapped[Decimal | None] = mapped_column(QUANTITY)
    """Shares in :attr:`prev_period_of_report`. Null exactly when the action is ``new``."""

    prev_value_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    """Value in :attr:`prev_period_of_report`. Null exactly when the action is ``new``."""

    prev_weight_pct: Mapped[Decimal | None] = mapped_column(WEIGHT_PCT)
    """Weight in :attr:`prev_period_of_report`. Null when the action is ``new``,
    and when that period's positions were all worth nothing."""

    shares_delta: Mapped[Decimal] = mapped_column(QUANTITY)
    """:attr:`shares` less :attr:`prev_shares`, or less nothing when ``new``.

    The number of shares by which the reported position changed, and the field
    the read API returns. The whole previous position, negative, for an exit.
    Not "sold" when negative, for the reason given on ``trim``."""

    shares_delta_pct: Mapped[Decimal | None] = mapped_column(CHANGE_PCT)
    """:attr:`shares_delta` as a percentage of :attr:`prev_shares`.

    -100 for an exit. Null when the action is ``new``, since growth from
    nothing has no percentage, and when the previous count was zero for the
    same reason."""

    value_delta: Mapped[Decimal] = mapped_column(MONEY)
    """:attr:`value_usd` less :attr:`prev_value_usd`, or less nothing when ``new``.

    Price and quantity together. A hold whose stock rose has a positive one."""

    weight_delta: Mapped[Decimal | None] = mapped_column(WEIGHT_PCT)
    """:attr:`weight_pct` less :attr:`prev_weight_pct` in percentage points,
    or less nothing when ``new``. Null when either weight is."""

    suspect: Mapped[bool] = mapped_column()
    """Whether a suspect filing counts toward this period or the previous one.

    Always false unless the snapshot was rebuilt with ``--include-suspect``.
    The previous period counts because the change is only as good as both of
    its ends: a ``new`` against a suspect period is a claim that the suspect
    filing did not list the security. An ``exit`` is the same claim about
    this period's filing, and rests on the previous one's having listed it.
    """

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    """When the rebuild that wrote this row ran: the same transaction, and so
    the same value, as the snapshot it was computed from."""

    __table_args__ = (
        CheckConstraint(ACTION_CHECK, name="action_is_known"),
        CheckConstraint(NEW_CHECK, name="new_when_not_held_before"),
        CheckConstraint(EXIT_CHECK, name="an_exit_holds_nothing"),
        # Who added, trimmed, opened or exited a stock in a period (0017).
        # Partial because that question never includes a hold, so only a query
        # that says action <> 'hold' can use it.
        Index(
            "ix_position_change_period_of_report_security_id_not_hold",
            "period_of_report",
            "security_id",
            postgresql_where=text("action <> 'hold'"),
        ),
    )
