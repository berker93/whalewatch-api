"""The materialised views: what each one caches, as a live query, and the refresh.

"Most owned stocks across the universe for a quarter" aggregates every
position every filer published in it, hundreds of thousands of rows, and so do
the quarter's flows. Computing that per request is wasteful when the inputs
change four times a year. So three aggregates of the derived tables are
materialised (migration ``0014``):

``mv_consensus_holdings``
    Per ``(period, security)``, from ``position_snapshot``: how many filers
    hold it, their dollars and shares, the average and median weight it has in
    their portfolios, and its rank by dollars held.
``mv_quarter_flows``
    Per ``(period, security)``, from ``position_change``: the dollars bought
    and sold, the net, the net shares, and how many filers opened, exited,
    bought and sold.
``mv_filer_summary``
    Per ``(filer, period)``, from both: the portfolio's value, its number of
    positions, the weight of its ten largest, and its turnover.

Each view's live query is here: the same aggregate, read from the tables as
they are now. The migration's SQL is history and is written out, so the two are
separate texts, and the ``check_*`` tests hold them to the same rows. They also
say what an endpoint would compute without the view, and the
:data:`MATERIALISED_VIEWS` handles are what one reads with it.

Traded dollars, not value_delta
-------------------------------
A change's ``value_delta`` includes the price move on every share held
throughout. Summed as flows, a stock that doubled reads as bought by every
holder who did nothing, and a trim during a rally as negative selling. So both
the flows and the turnover count what each change *traded*: the shares bought
or sold, at the period-end price (:func:`_traded_usd`). An exit has no price in
its own period, so it trades at the price it was last held at, which makes it
its whole previous value. A new position trades its whole value. A ``hold``
trades nothing, its few shares of drift included.

Period-end prices are the only ones a 13F has. A position bought at $50 and
worth $80 by the end of the quarter is counted as $80 of buying. That is the
usual estimate made from 13Fs, and it is an estimate.

A filer's first period is not a flow
------------------------------------
In a filer's first period every position is ``new``, and its
``prev_period_of_report`` is null. That is the first period we have, not a
quarter in which the filer bought everything, so the flows leave those rows
out. Otherwise every filer added to the universe would be a market-wide buying
spree in the quarter it starts. Its turnover is null, for want of a previous
portfolio.

Average and median weight
-------------------------
Both are over the filers that hold the stock. They differ most where it
matters most. A stock held at 20% by one manager and 0.5% by thirty others has
an average weight of 1.13% and a median of 0.5%: the average says a typical
holder has a real position in it, and the median says thirty-one holders and
one of them means it. The median is ``PERCENTILE_CONT(0.5)``, which is double
precision only. It is converted back exactly, since a weight has six decimal
places and the midpoint of two has seven, and rounded to six as the average is.

Refreshing
----------
Refreshed by ``whalewatch refresh-views``, when a period's ingestion is
complete, not on a timer. A timer refreshes mid-backfill and publishes a
quarter with a third of its filers in it. Until then each view is as of its
last refresh, which is the point of it.

Concurrently, so reads go on through a refresh. That needs a unique index on
plain columns, with no ``WHERE``, on each view, and the migration creates one.
A view that has never been populated cannot be refreshed concurrently, and is
refreshed plainly once.

:func:`refresh_views` holds the recompute lock while it runs. Every rebuild of
the derived tables takes the same lock, so none commits between the first
refresh and the last. All three views are refreshed from the same snapshot,
and agree with each other.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    Date,
    Numeric,
    Select,
    TableClause,
    and_,
    case,
    cast,
    column,
    false,
    func,
    select,
    table,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.position_change import ChangeAction, PositionChange
from app.db.models.position_snapshot import PositionSnapshot
from app.derived.recompute import hold_recompute_lock

#: How many of a portfolio's largest positions ``top10_weight_pct`` adds up.
TOP_POSITIONS: Final = 10

# The handles: table() constructs, deliberately not on Base.metadata, for
# FILER_PERIOD's reason. Autogenerate would take a view declared there for a
# table and draft a CREATE TABLE.

CONSENSUS_HOLDINGS: Final = table(
    "mv_consensus_holdings",
    column("period_of_report", Date),
    column("security_id", BigInteger),
    column("holder_count", BigInteger),
    column("total_value_usd", Numeric),
    column("total_shares", Numeric),
    column("avg_weight_pct", Numeric),
    column("median_weight_pct", Numeric),
    column("value_rank", BigInteger),
    column("suspect", Boolean),
)
"""Each security held in each period, and by how many filers, how much, and how heavily."""

QUARTER_FLOWS: Final = table(
    "mv_quarter_flows",
    column("period_of_report", Date),
    column("security_id", BigInteger),
    column("bought_value_usd", Numeric),
    column("sold_value_usd", Numeric),
    column("net_value_usd", Numeric),
    column("net_shares", Numeric),
    column("new_positions", BigInteger),
    column("exits", BigInteger),
    column("buyer_count", BigInteger),
    column("seller_count", BigInteger),
    column("suspect", Boolean),
)
"""Each security changed in each period: what was bought and sold, and by how many filers."""

FILER_SUMMARY: Final = table(
    "mv_filer_summary",
    column("filer_id", BigInteger),
    column("period_of_report", Date),
    column("portfolio_value_usd", Numeric),
    column("position_count", BigInteger),
    column("top10_weight_pct", Numeric),
    column("turnover_pct", Numeric),
    column("suspect", Boolean),
)
"""Each filer's each published period: its size, its concentration and its turnover."""


def consensus_holdings() -> Select[Any]:
    """What ``mv_consensus_holdings`` holds, read live from ``position_snapshot``.

    ``suspect`` when any holder's period was published with a suspect filing.
    """
    snapshot = PositionSnapshot
    total_value = func.sum(snapshot.value_usd)
    median = func.percentile_cont(0.5).within_group(snapshot.weight_pct)
    return select(
        snapshot.period_of_report,
        snapshot.security_id,
        func.count().label("holder_count"),
        total_value.label("total_value_usd"),
        func.sum(snapshot.shares).label("total_shares"),
        func.round(func.avg(snapshot.weight_pct), 6).label("avg_weight_pct"),
        func.round(cast(median, Numeric), 6).label("median_weight_pct"),
        func.rank()
        .over(partition_by=snapshot.period_of_report, order_by=total_value.desc())
        .label("value_rank"),
        func.bool_or(snapshot.suspect).label("suspect"),
    ).group_by(snapshot.period_of_report, snapshot.security_id)


def quarter_flows() -> Select[Any]:
    """What ``mv_quarter_flows`` holds, read live from ``position_change``.

    Buyers are ``new`` and ``add``, sellers ``trim`` and ``exit``, and a
    ``hold`` is neither. ``net_shares`` counts every change, holds included,
    so it is how far the shares these filers hold moved. A filer's first
    period is left out, as the module docstring says.
    """
    change = PositionChange
    buying = change.action.in_([ChangeAction.NEW.value, ChangeAction.ADD.value])
    selling = change.action.in_([ChangeAction.TRIM.value, ChangeAction.EXIT.value])

    def traded(which: ColumnElement[bool]) -> ColumnElement[Decimal]:
        return func.round(func.coalesce(func.sum(_traded_usd()).filter(which), 0), 2, type_=Numeric)

    def filers(which: ColumnElement[bool]) -> ColumnElement[int]:
        # One row per filer per security per period: a count of rows is a
        # count of filers.
        return func.count().filter(which)

    bought, sold = traded(buying), traded(selling)
    return (
        select(
            change.period_of_report,
            change.security_id,
            bought.label("bought_value_usd"),
            sold.label("sold_value_usd"),
            (bought - sold).label("net_value_usd"),
            func.sum(change.shares_delta).label("net_shares"),
            filers(change.action == ChangeAction.NEW.value).label("new_positions"),
            filers(change.action == ChangeAction.EXIT.value).label("exits"),
            filers(buying).label("buyer_count"),
            filers(selling).label("seller_count"),
            func.bool_or(change.suspect).label("suspect"),
        )
        .where(change.prev_period_of_report.is_not(None))
        .group_by(change.period_of_report, change.security_id)
    )


def filer_summary() -> Select[Any]:
    """What ``mv_filer_summary`` holds, read live from both derived tables.

    ``turnover_pct`` is ``sum(traded) / 2 / previous portfolio value``, in
    percent, and null in a filer's first period. Half of bought plus sold, so a
    manager who sold half the book and bought the same again turned over half
    of it. The previous portfolio's value is the sum of the period's
    ``prev_value_usd``: every position the previous period held is a change
    row here, held on or exited.

    ``suspect`` when the period was published with a suspect filing, or the
    period its changes are from was: turnover is only as good as both.
    """
    snapshot, change = PositionSnapshot, PositionChange
    ranked = select(
        snapshot.filer_id,
        snapshot.period_of_report,
        snapshot.value_usd,
        snapshot.suspect,
        # A tie at tenth place is between equal values, so which of them is in
        # the ten does not change the sum.
        func.row_number()
        .over(
            partition_by=(snapshot.filer_id, snapshot.period_of_report),
            order_by=(snapshot.value_usd.desc(), snapshot.security_id),
        )
        .label("place"),
    ).cte("ranked")
    held = (
        select(
            ranked.c.filer_id,
            ranked.c.period_of_report,
            func.sum(ranked.c.value_usd).label("portfolio_value_usd"),
            func.count().label("position_count"),
            func.sum(ranked.c.value_usd)
            .filter(ranked.c.place <= TOP_POSITIONS)
            .label("top10_value_usd"),
            func.bool_or(ranked.c.suspect).label("suspect"),
        )
        .group_by(ranked.c.filer_id, ranked.c.period_of_report)
        .cte("held")
    )
    # A hold traded nothing: its few shares of drift are not a trade.
    traded_usd = func.sum(_traded_usd()).filter(change.action != ChangeAction.HOLD.value)
    traded = (
        select(
            change.filer_id,
            change.period_of_report,
            func.coalesce(traded_usd, 0).label("traded_usd"),
            func.sum(change.prev_value_usd).label("prev_portfolio_value_usd"),
            func.bool_or(change.suspect).label("suspect"),
        )
        .group_by(change.filer_id, change.period_of_report)
        .cte("traded")
    )
    return (
        select(
            held.c.filer_id,
            held.c.period_of_report,
            held.c.portfolio_value_usd,
            held.c.position_count,
            func.round(
                held.c.top10_value_usd * 100 / func.nullif(held.c.portfolio_value_usd, 0), 6
            ).label("top10_weight_pct"),
            func.round(
                traded.c.traded_usd * 100 / 2 / func.nullif(traded.c.prev_portfolio_value_usd, 0),
                6,
            ).label("turnover_pct"),
            (held.c.suspect | func.coalesce(traded.c.suspect, false())).label("suspect"),
        )
        .select_from(held)
        .outerjoin(
            traded,
            and_(
                traded.c.filer_id == held.c.filer_id,
                traded.c.period_of_report == held.c.period_of_report,
            ),
        )
    )


def _traded_usd() -> ColumnElement[Decimal]:
    """The dollars one ``position_change`` row traded: its shares, at the period-end price.

    This period's price, or, for an exit, which has none, the price the position
    was last held at. Multiplied before dividing, so a new position trades
    exactly its value and an exit exactly the value it had.
    """
    change = PositionChange
    shares_traded = func.abs(change.shares_delta, type_=Numeric)
    return case(
        (change.shares > 0, shares_traded * change.value_usd / change.shares),
        (change.prev_shares > 0, shares_traded * change.prev_value_usd / change.prev_shares),
        else_=0,
    )


@dataclass(frozen=True, slots=True)
class MaterialisedView:
    """One materialised view: its handle, its key, and the live query it caches."""

    handle: TableClause
    key: tuple[str, ...]
    """The columns of its unique index, which a concurrent refresh matches rows on."""
    live: Callable[[], Select[Any]]
    """The rows it holds, read from the tables as they are now. Its columns are
    the handle's, in the same order."""

    @property
    def name(self) -> str:
        return self.handle.name


#: Every materialised view, in the order ``refresh-views`` refreshes them.
MATERIALISED_VIEWS: Final = (
    MaterialisedView(CONSENSUS_HOLDINGS, ("period_of_report", "security_id"), consensus_holdings),
    MaterialisedView(QUARTER_FLOWS, ("period_of_report", "security_id"), quarter_flows),
    MaterialisedView(FILER_SUMMARY, ("filer_id", "period_of_report"), filer_summary),
)


@dataclass(frozen=True, slots=True)
class Refreshed:
    """What :func:`refresh_views` did to one view."""

    name: str
    rows: int
    concurrently: bool
    """False only for a view never populated before, which cannot be refreshed
    concurrently."""
    seconds: float


async def refresh_views(session: AsyncSession) -> list[Refreshed]:
    """Refresh every materialised view from the derived tables, in the caller's transaction.

    Takes the recompute lock first, and holds it until the caller commits, so
    no rebuild lands between one view's refresh and the next. Readers see each
    view as it was until the commit, and refreshed after it.
    """
    await hold_recompute_lock(session)
    refreshed: list[Refreshed] = []
    for view in MATERIALISED_VIEWS:
        populated = await session.scalar(
            text(
                "SELECT ispopulated FROM pg_matviews "
                "WHERE schemaname = current_schema() AND matviewname = :name"
            ),
            {"name": view.name},
        )
        concurrently = "CONCURRENTLY " if populated else ""
        started = time.perf_counter()
        # The name is one of ours, not input: there is nothing to quote.
        await session.execute(text(f"REFRESH MATERIALIZED VIEW {concurrently}{view.name}"))
        seconds = time.perf_counter() - started
        rows = await session.scalar(select(func.count()).select_from(view.handle))
        refreshed.append(
            Refreshed(name=view.name, rows=rows or 0, concurrently=bool(populated), seconds=seconds)
        )
    return refreshed
