"""``position_change``: what each filer did to each position, and the one query that says.

``position_snapshot`` is what a filer holds. This is the difference between two
of its periods, which is the question the product exists to answer: what did
they add, what did they trim. ``recompute`` rebuilds it straight after the
snapshot, in the same transaction, from the rows just written. So every change
is between two portfolios that were published, and no reader sees one of the
two tables rebuilt and not the other.

The query, in four steps
------------------------
1. ``held``: every position, with ``LAG`` over the filer's history of the
   security, ``PARTITION BY filer_id, security_id ORDER BY period_of_report``,
   for its previous shares, value and weight and the period they are from.
   The window is defined once, in :func:`_lag`. SQLAlchemy cannot write a
   ``WINDOW w AS (...)`` clause, so the SQL spells the window out once per
   ``LAG``. Postgres finds them identical and computes all four in one sort
   and one pass, and a test holds it to that.
2. ``periods``: each filer's published periods, from the ``filer_period``
   view, each with the one before it and the one after.
3. ``compared``: each position against the filer's previous period, and each
   exit, as a position of nothing against the period it was last held in.
4. The final select classifies every row and takes the deltas, the same way
   for both, so an exit's deltas are the previous position, negative, by the
   formulas that work out every other row's.

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

Exits are found from the period before
--------------------------------------
A position the filer sold out of has no snapshot row in the period it is gone
from, so there is nothing there to ``LAG`` from. :func:`_exited` starts from
the last period it was held in instead. It takes each position and the
filer's next period, by ``LEAD`` over ``filer_period``. It left joins the
snapshot at that period for the same security, and where the join misses,
the position is gone. Postgres plans the left join as an anti-join.

``LEAD`` is null in the filer's latest period, and a left join to a null period
always misses, so that filter is load-bearing. Without it, every position in
every filer's latest quarter is an exit. A filer that has not filed its next
quarter yet has not sold anything: a missing filing is not an exit. A gap or a
withheld period is stepped over, as for ``LAG``, and the exit is dated the
period after it, with ``prev_period_of_report`` the last one before it.

``LEAD`` shares the window of the ``LAG`` over the same periods, so the next
period costs nothing more. A self-join, taking the smallest later period, gives
the same rows but pairs every period with every later one, which grows with the
square of the filer's history. ``CROSS JOIN LATERAL`` with ``LIMIT 1`` probes
the index once per period, which is cheap against a table. Against this view it
is not, because each probe reads the whole of the next period to aggregate it.

Across a split
--------------
Share counts as filed make a 4-for-1 split an ``add`` of 300% for every holder
of the stock. So a change whose previous period is before a split in
``security_split`` and whose own period is on or after it is taken against the
previous shares times the split's ratio, and every split's ratio multiplied
when the filer skipped a quarter that had another. A holder who held through
it is a ``hold``, one who held and bought is an ``add`` of what they bought,
and ``shares_delta`` is in today's shares. The previous figures stay as filed,
so they are still the previous period's snapshot.

The ratio comes from a ``LATERAL`` over ``security_split`` for each compared
row, a probe of its primary key: there are a few dozen splits, and the range
of periods a change spans is not something a hash join can match on. The
product is ``exp(sum(ln(...)))`` of each side of the fraction, in ``numeric``,
rounded back to the whole number it is. An exit is not adjusted: the position
is gone whatever the ratio, and its deltas are the position as it was filed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CTE,
    ColumnElement,
    FunctionElement,
    Numeric,
    Select,
    SQLColumnExpression,
    and_,
    case,
    cast,
    delete,
    false,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.position_change import ChangeAction, PositionChange
from app.db.models.position_snapshot import PositionSnapshot
from app.db.models.security_split import SecuritySplit
from app.db.queries.periods import FILER_PERIOD
from app.derived.scope import EVERYTHING, Scope

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
    "is_split",
    "split_ratio",
    "suspect",
)


@dataclass(frozen=True, slots=True)
class ChangeRebuild:
    """What one :func:`recompute_position_change` wrote, counted by action."""

    new: int
    add: int
    trim: int
    hold: int
    exit: int
    split: int = 0
    """Of the others, the changes taken across a split (``is_split``)."""


def position_changes(*, scope: Scope = EVERYTHING) -> Select[Any]:
    """The rows ``position_change`` holds: one per row of ``position_snapshot``, and one per exit.

    Reads the snapshot *table* rather than
    :func:`~app.derived.position_snapshot.snapshot_positions`, so the changes
    are between the portfolios as they were published, weights rounded as they
    were stored.

    :param scope: Only the changes dated these ``(filer, period)`` pairs. A
        filer's changes depend on its own periods alone, so they are the same
        either way. The windows still read every period of the filers in
        scope, since a period's change needs the one before it, and the pairs
        are picked out of what they return.
    :returns: A select of :data:`CHANGE_COLUMNS`, unordered.
        ``shares_delta_pct`` is unrounded here, and rounded to six places when
        stored.
    """
    held = _held(scope).cte("held")
    periods = _periods(scope).cte("periods")
    compared = union_all(_compared(held, periods), _exited(periods, scope)).cte("compared")

    now = compared.c
    new = now.prev_shares.is_(None)
    is_split = and_(~new, now.split_numerator.is_not(None))
    # The previous shares in today's shares: times the split's ratio, rounded
    # to the column's places, or as filed.
    previous = case(
        (
            is_split,
            func.round(now.prev_shares * now.split_numerator / now.split_denominator, 4),
        ),
        else_=now.prev_shares,
    )

    def since(previous: ColumnElement[Any]) -> ColumnElement[Any]:
        """The previous figure, or nothing for a new position, which grew from none."""
        return case((new, 0), else_=previous)

    return select(
        now.filer_id,
        now.period_of_report,
        now.security_id,
        case(
            # First: judged on shares like the rest, an exit is a trim to
            # zero, or a hold when it was a position of zero shares.
            (now.exited, ChangeAction.EXIT.value),
            (new, ChangeAction.NEW.value),
            # Multiplied out rather than divided: exact in numeric, and a
            # previous count of zero has nothing to divide by.
            (
                func.abs(now.shares - previous) * 100 <= previous * HOLD_BAND_PCT,
                ChangeAction.HOLD.value,
            ),
            (now.shares > previous, ChangeAction.ADD.value),
            else_=ChangeAction.TRIM.value,
        ).label("action"),
        now.shares,
        now.value_usd,
        now.weight_pct,
        now.prev_period_of_report,
        now.prev_shares,
        now.prev_value_usd,
        now.prev_weight_pct,
        (now.shares - since(previous)).label("shares_delta"),
        ((now.shares - previous) * 100 / func.nullif(previous, 0)).label("shares_delta_pct"),
        (now.value_usd - since(now.prev_value_usd)).label("value_delta"),
        (now.weight_pct - since(now.prev_weight_pct)).label("weight_delta"),
        is_split.label("is_split"),
        case((is_split, now.split_numerator / now.split_denominator)).label("split_ratio"),
        now.suspect,
    ).where(*scope.covers(now.filer_id, now.period_of_report))


async def recompute_position_change(
    session: AsyncSession, scope: Scope = EVERYTHING
) -> ChangeRebuild:
    """Replace ``position_change``, all of it or the rows in ``scope``, from ``position_snapshot``.

    Run after :func:`~app.derived.position_snapshot.recompute_position_snapshot`
    in the same transaction, as ``recompute`` does: this reads the rows that
    call just wrote. Delete and reinsert, for the snapshot's reasons. A reader
    sees the old changes until the commit and the new ones after, never a mix.

    :param scope: Rebuild the changes dated these ``(filer, period)`` pairs and
        leave every other row alone, as it was last built. An exit is dated the
        period it is gone from, so it is in the scope of that period.
        :func:`~app.derived.recompute.recompute` adds the period after each
        rebuilt snapshot period, whose changes start from it.
    """
    in_scope = scope.covers(PositionChange.filer_id, PositionChange.period_of_report)

    await session.execute(delete(PositionChange).where(*in_scope))
    await session.execute(
        insert(PositionChange).from_select(CHANGE_COLUMNS, position_changes(scope=scope))
    )

    rows = await session.execute(
        select(
            PositionChange.action,
            func.count(),
            func.count().filter(PositionChange.is_split),
        )
        .where(*in_scope)
        .group_by(PositionChange.action)
    )
    counts: dict[str, int] = {}
    split = 0
    for action, rebuilt, adjusted in rows.tuples():
        counts[action] = rebuilt
        split += adjusted
    return ChangeRebuild(
        new=counts.get(ChangeAction.NEW, 0),
        add=counts.get(ChangeAction.ADD, 0),
        trim=counts.get(ChangeAction.TRIM, 0),
        hold=counts.get(ChangeAction.HOLD, 0),
        exit=counts.get(ChangeAction.EXIT, 0),
        split=split,
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


def _periods(scope: Scope) -> Select[Any]:
    """Each filer's published periods, each with the one before it and the one after.

    Read from ``filer_period``, so a period counts as published exactly when it
    has rows in the snapshot. ``prev_suspect`` and ``next_suspect`` are whether
    those periods were published with a suspect filing. All four are over one
    window, so Postgres computes them in one pass, as for :func:`_lag`.
    """
    view = FILER_PERIOD

    def over_periods(adjacent: FunctionElement[Any]) -> ColumnElement[Any]:
        return adjacent.over(partition_by=view.c.filer_id, order_by=view.c.period_of_report)

    statement = select(
        view.c.filer_id,
        view.c.period_of_report,
        over_periods(func.lag(view.c.period_of_report)).label("prev_period_of_report"),
        over_periods(func.lag(view.c.suspect)).label("prev_suspect"),
        over_periods(func.lead(view.c.period_of_report)).label("next_period_of_report"),
        over_periods(func.lead(view.c.suspect)).label("next_suspect"),
    )
    # Pushed down into the view, before its GROUP BY: whole filers, so no
    # window loses a neighbour.
    return statement.where(*scope.of_filers(view.c.filer_id))


def _held(scope: Scope) -> Select[Any]:
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
    return statement.where(*scope.of_filers(PositionSnapshot.filer_id))


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

    split = split_between(
        held.c.security_id, periods.c.prev_period_of_report, held.c.period_of_report
    )

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
            false().label("exited"),
            split.c.numerator.label("split_numerator"),
            split.c.denominator.label("split_denominator"),
        )
        .select_from(held)
        .join(
            periods,
            (periods.c.filer_id == held.c.filer_id)
            & (periods.c.period_of_report == held.c.period_of_report),
        )
        .outerjoin(split, true())
    )


def split_between(
    security_id: SQLColumnExpression[Any],
    after: SQLColumnExpression[Any],
    until: SQLColumnExpression[Any],
    *,
    name: str = "split",
) -> Any:
    """``LATERAL``: every split of the security after one period and until another, multiplied.

    One row always, an aggregate's: the product of the numerators and of the
    denominators, or two nulls when there is no split between them, or no
    previous period at all.
    """

    def product(side: SQLColumnExpression[int]) -> ColumnElement[Any]:
        # Postgres has no product aggregate. In numeric, ln and exp of whole
        # numbers this small round back to exactly the product.
        return func.round(func.exp(func.sum(func.ln(cast(side, Numeric)))))

    split = SecuritySplit
    return (
        select(
            product(split.numerator).label("numerator"),
            product(split.denominator).label("denominator"),
        )
        .where(
            split.security_id == security_id,
            split.period_of_report > after,
            split.period_of_report <= until,
        )
        .lateral(name)
    )


def _exited(periods: CTE, scope: Scope) -> Select[Any]:
    """Every position gone from the filer's next period, as a position of nothing in it.

    Found from the period it was last held in, since the period it is gone from
    has no row for it. Each position is left joined to the snapshot at the
    filer's next period, for the same security, and a miss is an exit. The
    columns are :func:`_compared`'s, so the two are one ``UNION ALL``. The
    position is the previous figures, and this period's are zero.

    ``suspect`` for :func:`_compared`'s reason: when either period was
    published with a suspect filing.
    """
    then = PositionSnapshot
    later = aliased(PositionSnapshot, name="later")
    nothing = literal(0)

    return (
        select(
            then.filer_id,
            periods.c.next_period_of_report.label("period_of_report"),
            then.security_id,
            nothing.label("shares"),
            nothing.label("value_usd"),
            nothing.label("weight_pct"),
            then.period_of_report.label("prev_period_of_report"),
            then.shares.label("prev_shares"),
            then.value_usd.label("prev_value_usd"),
            then.weight_pct.label("prev_weight_pct"),
            or_(then.suspect, periods.c.next_suspect).label("suspect"),
            true().label("exited"),
            # Typed, for the UNION ALL: an exit is not adjusted.
            cast(null(), Numeric).label("split_numerator"),
            cast(null(), Numeric).label("split_denominator"),
        )
        .select_from(then)
        .join(
            periods,
            (periods.c.filer_id == then.filer_id)
            & (periods.c.period_of_report == then.period_of_report),
        )
        .outerjoin(
            later,
            (later.filer_id == then.filer_id)
            & (later.period_of_report == periods.c.next_period_of_report)
            & (later.security_id == then.security_id),
        )
        .where(
            # The filer's latest period has no next one, and the join to a
            # null period always misses: without this, everything the filer
            # holds now is an exit. A missing filing is not an exit.
            periods.c.next_period_of_report.is_not(None),
            later.security_id.is_(None),
            *scope.of_filers(then.filer_id),
        )
    )
