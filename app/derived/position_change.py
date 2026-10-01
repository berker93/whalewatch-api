"""``position_change``: what each filer did to each position, and the one query that says.

``position_snapshot`` is what a filer holds. This is the difference between two
of its periods, which is the question the product exists to answer: what did
they add, what did they trim. ``recompute`` rebuilds it straight after the
snapshot, in the same transaction, from the rows just written. So every change
is between two portfolios that were published, and no reader sees one of the
two tables rebuilt and not the other.

The query, in three steps
-------------------------
1. ``held``: every position, with ``LAG`` over the filer's history of the
   security, ``PARTITION BY filer_id, security_id ORDER BY period_of_report``,
   for its previous shares, value and weight and the period they are from.
   The window is defined once, in :func:`_lag`. SQLAlchemy cannot write a
   ``WINDOW w AS (...)`` clause, so the SQL spells the window out once per
   ``LAG``. Postgres finds them identical and computes all four in one sort
   and one pass, and a test holds it to that.
2. ``filer_period``: each filer's published periods, each with the one before
   it.
3. ``compared``: each position against the filer's previous period. The final
   select classifies it and takes the deltas.

Previous means the filer's previous period
------------------------------------------
``LAG`` over a security's history returns the last period in which the filer
held it, however long ago that was. That is the filer's previous period only if
the filer held the security then. A stock held in 2023Q1, sold in Q2 and bought
back in Q3 lags to Q1, which makes Q3 an add or a hold against a position the
filer did not have in Q2. So ``compared`` keeps the lagged row only when it is
from the filer's previous period, and the position is ``new`` otherwise.

The filer's previous period is its previous *published* one. Across a quarter
with no 13F loaded, or one withheld for a suspect filing, it is the last period
before the gap, and ``prev_period_of_report`` says which. Counting back one
calendar quarter instead would make every position after a withheld quarter
``new``: a quarter later, that is the manager who bought everything, which
withholding the period exists to prevent.

Hold is a band, not equality
----------------------------
Managers' reported share counts drift by a handful of shares between quarters,
from dividend reinvestment and rounding, with nobody having traded. Compared
exactly, half of every portfolio would be an add or a trim, and the activity
view would be noise. A change within :data:`HOLD_BAND_PCT` of the previous
count, either way, is a ``hold``. Its delta is stored as it is, not zeroed, so
the deltas still add up to the position.

Not here yet
------------
**Exits** are DATA-3. A position held last period and gone this one has no
snapshot row to lag from, so it has no row here either.

**Splits** are not adjusted for. Share counts are compared as filed, so across
a 4-for-1 split every holder of the stock shows an ``add`` of 300%. Adjusting
needs the corporate-action feed, which does not exist yet. ``check-data``
flags the extreme splits, as position jumps.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CTE,
    ColumnElement,
    Select,
    SQLColumnExpression,
    case,
    delete,
    false,
    func,
    insert,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.position_change import ChangeAction, PositionChange
from app.db.models.position_snapshot import PositionSnapshot

#: The hold band: a change in shares within this percentage of the previous
#: count, either way, is a ``hold``. 0.01% of 50,000 shares is 5, which covers a
#: dividend reinvested, and a manager who cut a stake by a tenth of a percent is
#: still shown trimming it.
HOLD_BAND_PCT: Final = Decimal("0.01")

#: :func:`position_changes`'s columns, in order, as ``position_change`` names them.
CHANGE_COLUMNS: Final = (
    "filer_id",
    "period_of_report",
    "security_id",
    "action",
    "shares",
    "value_usd",
    "weight_pct",
    "prev_period_of_report",
    "prev_shares",
    "prev_value_usd",
    "prev_weight_pct",
    "shares_delta",
    "shares_delta_pct",
    "value_delta",
    "weight_delta",
    "suspect",
)


@dataclass(frozen=True, slots=True)
class ChangeRebuild:
    """What one :func:`recompute_position_change` wrote, counted by action."""

    new: int
    add: int
    trim: int
    hold: int


def position_changes(*, filer_id: int | None = None) -> Select[Any]:
    """The rows ``position_change`` holds: one per row of ``position_snapshot``.

    Reads the snapshot *table* rather than
    :func:`~app.derived.position_snapshot.snapshot_positions`, so the changes
    are between the portfolios as they were published, weights rounded as they
    were stored.

    :param filer_id: One filer's changes instead of every filer's. A filer's
        changes depend on its own periods alone, so they are the same either way.
    :returns: A select of :data:`CHANGE_COLUMNS`, unordered.
        ``shares_delta_pct`` is unrounded here, and rounded to six places when
        stored.
    """
    held = _held(filer_id).cte("held")
    periods = _filer_periods(filer_id).cte("filer_period")
    compared = _compared(held, periods).cte("compared")

    now = compared.c
    new = now.prev_shares.is_(None)

    def since(previous: ColumnElement[Any]) -> ColumnElement[Any]:
        """The previous figure, or nothing for a new position, which grew from none."""
        return case((new, 0), else_=previous)

    return select(
        now.filer_id,
        now.period_of_report,
        now.security_id,
        case(
            (new, ChangeAction.NEW.value),
            # Multiplied out rather than divided: exact in numeric, and a
            # previous count of zero has nothing to divide by.
            (
                func.abs(now.shares - now.prev_shares) * 100 <= now.prev_shares * HOLD_BAND_PCT,
                ChangeAction.HOLD.value,
            ),
            (now.shares > now.prev_shares, ChangeAction.ADD.value),
            else_=ChangeAction.TRIM.value,
        ).label("action"),
        now.shares,
        now.value_usd,
        now.weight_pct,
        now.prev_period_of_report,
        now.prev_shares,
        now.prev_value_usd,
        now.prev_weight_pct,
        (now.shares - since(now.prev_shares)).label("shares_delta"),
        ((now.shares - now.prev_shares) * 100 / func.nullif(now.prev_shares, 0)).label(
            "shares_delta_pct"
        ),
        (now.value_usd - since(now.prev_value_usd)).label("value_delta"),
        (now.weight_pct - since(now.prev_weight_pct)).label("weight_delta"),
        now.suspect,
    )


async def recompute_position_change(
    session: AsyncSession, *, filer_id: int | None = None
) -> ChangeRebuild:
    """Replace ``position_change``, all of it or one filer's rows, from ``position_snapshot``.

    Run after :func:`~app.derived.position_snapshot.recompute_position_snapshot`
    in the same transaction, as ``recompute`` does: this reads the rows that
    call just wrote. Delete and reinsert, for the snapshot's reasons. A reader
    sees the old changes until the commit and the new ones after, never a mix.

    :param filer_id: Rebuild this filer's rows and leave every other filer's
        alone, as they were last built.
    """
    scope = [] if filer_id is None else [PositionChange.filer_id == filer_id]

    await session.execute(delete(PositionChange).where(*scope))
    await session.execute(
        insert(PositionChange).from_select(CHANGE_COLUMNS, position_changes(filer_id=filer_id))
    )

    counts: dict[str, int] = dict(
        (
            await session.execute(
                select(PositionChange.action, func.count())
                .where(*scope)
                .group_by(PositionChange.action)
            )
        )
        .tuples()
        .all()
    )
    return ChangeRebuild(
        new=counts.get(ChangeAction.NEW, 0),
        add=counts.get(ChangeAction.ADD, 0),
        trim=counts.get(ChangeAction.TRIM, 0),
        hold=counts.get(ChangeAction.HOLD, 0),
    )


def _lag(column: SQLColumnExpression[Any]) -> ColumnElement[Any]:
    """``LAG(column) OVER w``: ``column`` from the filer's previous row for the security.

    ``w`` is one filer's history of one security, oldest period first. It is
    written down here and nowhere else, so every ``LAG`` in :func:`_held` is
    over the same window, and Postgres computes them together.
    """
    return func.lag(column).over(
        partition_by=(PositionSnapshot.filer_id, PositionSnapshot.security_id),
        order_by=PositionSnapshot.period_of_report,
    )


def _filer_periods(filer_id: int | None) -> Select[Any]:
    """Each filer's published periods, each with the one before it.

    Grouped from the snapshot, so a period counts as published exactly when it
    has rows there. ``prev_suspect`` is whether that previous period was
    published with a suspect filing. ``suspect`` is the same on every row of a
    period, so ``bool_or`` is just any one of them.
    """

    def previous(expression: SQLColumnExpression[Any]) -> ColumnElement[Any]:
        # The window runs after the GROUP BY, over one row per period.
        return func.lag(expression).over(
            partition_by=PositionSnapshot.filer_id, order_by=PositionSnapshot.period_of_report
        )

    statement = select(
        PositionSnapshot.filer_id,
        PositionSnapshot.period_of_report,
        previous(PositionSnapshot.period_of_report).label("prev_period_of_report"),
        previous(func.bool_or(PositionSnapshot.suspect)).label("prev_suspect"),
    ).group_by(PositionSnapshot.filer_id, PositionSnapshot.period_of_report)
    return statement if filer_id is None else statement.where(PositionSnapshot.filer_id == filer_id)


def _held(filer_id: int | None) -> Select[Any]:
    """Every published position, with the filer's previous row for the same security.

    ``last_period`` is the period that row is from: the last in which the filer
    held the security, which need not be the filer's previous period.
    """
    statement = select(
        PositionSnapshot.filer_id,
        PositionSnapshot.period_of_report,
        PositionSnapshot.security_id,
        PositionSnapshot.shares,
        PositionSnapshot.value_usd,
        PositionSnapshot.weight_pct,
        PositionSnapshot.suspect,
        _lag(PositionSnapshot.period_of_report).label("last_period"),
        _lag(PositionSnapshot.shares).label("last_shares"),
        _lag(PositionSnapshot.value_usd).label("last_value_usd"),
        _lag(PositionSnapshot.weight_pct).label("last_weight_pct"),
    )
    # A WHERE runs before the window, but it removes whole filers, and no
    # window reaches across filers, so no row's LAG changes.
    return statement if filer_id is None else statement.where(PositionSnapshot.filer_id == filer_id)


def _compared(held: CTE, periods: CTE) -> Select[Any]:
    """Each position against its filer's previous period.

    The lagged row stands as the previous figures only when it is from that
    period. Otherwise the filer did not hold the security then, however
    recently before it did, and the figures are null: the position is new.

    ``suspect`` when this period or the previous one was published with a
    suspect filing. A change is only as good as its two ends, and that includes
    a new position, which is a claim that the previous period did not list it.
    """
    held_then = held.c.last_period == periods.c.prev_period_of_report

    def then(column: ColumnElement[Any]) -> ColumnElement[Any]:
        # No ELSE: null when the comparison is false, and when it is null,
        # which is a first period or a first appearance.
        return case((held_then, column))

    return (
        select(
            held.c.filer_id,
            held.c.period_of_report,
            held.c.security_id,
            held.c.shares,
            held.c.value_usd,
            held.c.weight_pct,
            periods.c.prev_period_of_report,
            then(held.c.last_shares).label("prev_shares"),
            then(held.c.last_value_usd).label("prev_value_usd"),
            then(held.c.last_weight_pct).label("prev_weight_pct"),
            or_(held.c.suspect, func.coalesce(periods.c.prev_suspect, false())).label("suspect"),
        )
        .select_from(held)
        .join(
            periods,
            (periods.c.filer_id == held.c.filer_id)
            & (periods.c.period_of_report == held.c.period_of_report),
        )
    )
