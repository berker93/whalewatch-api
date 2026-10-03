"""The materialised views: what each one caches, as a live query, and the refresh.

"Most owned stocks across the universe for a quarter" aggregates every
position every filer published in it, hundreds of thousands of rows, and so do
the quarter's flows. Computing that per request is wasteful when the inputs
change four times a year. So three aggregates of the derived tables are
materialised (migrations ``0014`` and ``0019``):

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
``mv_year_flows``
    ``mv_quarter_flows`` over the four quarters ending at each period, with
    each filer counted once however many of them it traded in.
``mv_filing_feed``
    Per effective filing of a published period: when it was filed, the
    period's position count, and its largest trade. The recent-filings feed.

Each view's live query is here: the same aggregate, read from the tables as
they are now. The migration's SQL is history and is written out, so the two are
separate texts, and the ``check_*`` tests hold them to the same rows, as
``reconcile`` does on the real data. They also say what an endpoint would
compute without the view, and the :data:`MATERIALISED_VIEWS` handles are what
one reads with it.

Traded dollars, not value_delta
-------------------------------
A change's ``value_delta`` includes the price move on every share held
throughout. Summed as flows, a stock that doubled reads as bought by every
holder who did nothing, and a trim during a rally as negative selling. So both
the flows and the turnover count what each change *traded*: the shares bought
or sold, at the period-end price (:func:`traded_usd`). An exit has no price in
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

A year is two figures, not one
------------------------------
A position bought in Q1 and sold in Q3 nets to about nothing over the year.
That is right for the year's net flow and wrong for its top buys, which would
leave out its biggest accumulations whenever they were sold again. So the year
keeps ``bought_value_usd`` (gross: every quarter's buying) and
``net_value_usd`` apart, as the quarter does, and the API serves both. The
dollars are the sums of the four quarters' rows, each already rounded to
cents, so a year adds up to its quarters exactly. The counts cannot be sums: a
filer that bought in two of the quarters is one buyer of the year. They are
counted again from ``position_change``, ``DISTINCT`` per filer.

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
Refreshed by ``whalewatch refresh-views``. ``recompute`` runs it after every
rebuild. ``ingest-filing`` runs it after a load that publishes. ``backfill``
runs it once, at the end, if anything loaded, and never after each filing: a
refresh in the middle of a backfill publishes a quarter with a third of its
filers in it. None of them runs on a timer. Until the next refresh each view
is as of its last one, which is the point of it.

Concurrently, so reads go on through a refresh. ``REFRESH ... CONCURRENTLY``
builds the new rows beside the old ones and applies the difference, and
readers see the old rows until the commit. It needs a unique index on plain
columns, with no ``WHERE``, on each view, and the migration creates one. It
also does more work than a plain refresh, which empties the view and fills it
again under a lock that blocks every read until the commit. That cost is
worth paying to never block the API. A view that has never been populated
cannot be refreshed concurrently, and is refreshed plainly once.

:func:`refresh_views` holds the recompute lock while it runs. Every rebuild of
the derived tables takes the same lock, so none commits between the first
refresh and the last. All the views are refreshed from the same snapshot,
and agree with each other.

A view that reads another is refreshed after it. The order is read from the
catalog (:func:`view_reads`), not declared here, so a new view that reads an
old one is ordered correctly without anyone listing the dependency. None does
yet.

Each refresh records its time in ``matview_refresh``, in the same transaction,
for the API to report how old an aggregate is (:func:`last_refreshed`).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    Date,
    DateTime,
    Interval,
    Numeric,
    Select,
    TableClause,
    Text,
    and_,
    case,
    cast,
    column,
    false,
    func,
    literal_column,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filing import Filing
from app.db.models.matview_refresh import MatviewRefresh
from app.db.models.position_change import ChangeAction, PositionChange
from app.db.models.position_snapshot import PositionSnapshot
from app.db.queries.effective import EFFECTIVE_FILING
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

YEAR_FLOWS: Final = table(
    "mv_year_flows",
    *(column(c.name, c.type) for c in QUARTER_FLOWS.c),
)
""":data:`QUARTER_FLOWS`'s columns, over the four quarters ending at ``period_of_report``."""

FILING_FEED: Final = table(
    "mv_filing_feed",
    column("filing_id", BigInteger),
    column("filer_id", BigInteger),
    column("period_of_report", Date),
    column("filed_at", DateTime(timezone=True)),
    column("position_count", BigInteger),
    column("largest_security_id", BigInteger),
    column("largest_action", Text),
    column("largest_shares_delta", Numeric),
    column("largest_traded_value_usd", Numeric),
)
"""Each effective filing of a published period, with the period's size and largest trade."""


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
        return func.round(func.coalesce(func.sum(traded_usd()).filter(which), 0), 2, type_=Numeric)

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
    traded_total = func.sum(traded_usd()).filter(change.action != ChangeAction.HOLD.value)
    traded = (
        select(
            change.filer_id,
            change.period_of_report,
            func.coalesce(traded_total, 0).label("traded_usd"),
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


def year_flows() -> Select[Any]:
    """What ``mv_year_flows`` holds: :func:`quarter_flows` over each four quarters.

    Every period with flows ends a year, and the year is it and the three
    quarters before it. The dollars and ``net_shares`` are sums of the
    quarters' rows. The counts are distinct filers over the year, read from
    ``position_change`` again, holds and first periods left out as the
    quarter leaves them out.
    """
    change = PositionChange
    quarters = quarter_flows().cte("quarters")
    ends = select(quarters.c.period_of_report).distinct().cte("ends")

    def in_year(period: Any) -> ColumnElement[bool]:
        a_year_before = ends.c.period_of_report - literal_column("interval '1 year'", Interval)
        return and_(period <= ends.c.period_of_report, period > a_year_before)

    dollars = (
        select(
            ends.c.period_of_report,
            quarters.c.security_id,
            func.sum(quarters.c.bought_value_usd).label("bought_value_usd"),
            func.sum(quarters.c.sold_value_usd).label("sold_value_usd"),
            func.sum(quarters.c.net_shares).label("net_shares"),
            func.bool_or(quarters.c.suspect).label("suspect"),
        )
        .select_from(ends)
        .join(quarters, in_year(quarters.c.period_of_report))
        .group_by(ends.c.period_of_report, quarters.c.security_id)
        .cte("dollars")
    )

    def filers(*actions: ChangeAction) -> ColumnElement[int]:
        return func.count(change.filer_id.distinct()).filter(
            change.action.in_([action.value for action in actions])
        )

    traders = (
        select(
            ends.c.period_of_report,
            change.security_id,
            filers(ChangeAction.NEW).label("new_positions"),
            filers(ChangeAction.EXIT).label("exits"),
            filers(ChangeAction.NEW, ChangeAction.ADD).label("buyer_count"),
            filers(ChangeAction.TRIM, ChangeAction.EXIT).label("seller_count"),
        )
        .select_from(ends)
        .join(change, in_year(change.period_of_report))
        .where(
            change.prev_period_of_report.is_not(None),
            change.action != ChangeAction.HOLD.value,
        )
        .group_by(ends.c.period_of_report, change.security_id)
        .cte("traders")
    )

    def counted(name: str) -> ColumnElement[int]:
        # A security only held on through the year has no traders.
        return func.coalesce(traders.c[name], 0).label(name)

    return (
        select(
            dollars.c.period_of_report,
            dollars.c.security_id,
            dollars.c.bought_value_usd,
            dollars.c.sold_value_usd,
            (dollars.c.bought_value_usd - dollars.c.sold_value_usd).label("net_value_usd"),
            dollars.c.net_shares,
            counted("new_positions"),
            counted("exits"),
            counted("buyer_count"),
            counted("seller_count"),
            dollars.c.suspect,
        )
        .select_from(dollars)
        .outerjoin(
            traders,
            and_(
                traders.c.period_of_report == dollars.c.period_of_report,
                traders.c.security_id == dollars.c.security_id,
            ),
        )
    )


def filing_feed() -> Select[Any]:
    """What ``mv_filing_feed`` holds, read live from the filings and both derived tables.

    One row per filing in ``effective_filing`` whose period is in
    ``position_snapshot``: one a withheld suspect filing made is not news yet.
    Two filings counting toward one period, an original and its additions,
    share the period's figures. The largest trade is by :func:`traded_usd`,
    rounded as the activity endpoint rounds it, and null in the filer's first
    period, where every position is new only to us.
    """
    snapshot, change, effective = PositionSnapshot, PositionChange, EFFECTIVE_FILING
    traded = func.round(traded_usd(), 2)
    largest = (
        select(
            change.filer_id,
            change.period_of_report,
            change.security_id,
            change.action,
            change.shares_delta,
            traded.label("traded_value_usd"),
        )
        .distinct(change.filer_id, change.period_of_report)
        .where(
            change.action != ChangeAction.HOLD.value,
            change.prev_period_of_report.is_not(None),
        )
        .order_by(
            change.filer_id, change.period_of_report, traded.desc(), change.security_id.desc()
        )
        .cte("largest")
    )
    positions = (
        select(
            snapshot.filer_id,
            snapshot.period_of_report,
            func.count().label("position_count"),
        )
        .group_by(snapshot.filer_id, snapshot.period_of_report)
        .cte("positions")
    )
    return (
        select(
            effective.c.filing_id,
            effective.c.filer_id,
            effective.c.period_of_report,
            Filing.filed_at,
            positions.c.position_count,
            largest.c.security_id.label("largest_security_id"),
            largest.c.action.label("largest_action"),
            largest.c.shares_delta.label("largest_shares_delta"),
            largest.c.traded_value_usd.label("largest_traded_value_usd"),
        )
        .select_from(effective)
        .join(Filing, Filing.id == effective.c.filing_id)
        .join(
            positions,
            and_(
                positions.c.filer_id == effective.c.filer_id,
                positions.c.period_of_report == effective.c.period_of_report,
            ),
        )
        .outerjoin(
            largest,
            and_(
                largest.c.filer_id == effective.c.filer_id,
                largest.c.period_of_report == effective.c.period_of_report,
            ),
        )
    )


def traded_usd() -> ColumnElement[Decimal]:
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


#: Every materialised view. ``refresh-views`` refreshes them in this order,
#: except that a view that reads another always comes after it.
MATERIALISED_VIEWS: Final = (
    MaterialisedView(CONSENSUS_HOLDINGS, ("period_of_report", "security_id"), consensus_holdings),
    MaterialisedView(QUARTER_FLOWS, ("period_of_report", "security_id"), quarter_flows),
    MaterialisedView(FILER_SUMMARY, ("filer_id", "period_of_report"), filer_summary),
    MaterialisedView(YEAR_FLOWS, ("period_of_report", "security_id"), year_flows),
    MaterialisedView(FILING_FEED, ("filer_id", "period_of_report", "filing_id"), filing_feed),
)


# Every materialised view in this schema, and every relation its query names,
# directly or through plain views. A view's rewrite rule depends on each
# relation it reads, and on the view itself, which is left out. Matviews only:
# a plain view is followed, not returned.
_READS: Final = text("""
    WITH RECURSIVE reads (reader, source) AS (
        SELECT r.ev_class, d.refobjid
        FROM pg_rewrite r
        JOIN pg_class m ON m.oid = r.ev_class
        JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
        WHERE m.relkind = 'm'
          AND m.relnamespace = to_regnamespace(current_schema())
          AND d.refclassid = 'pg_class'::regclass
          AND d.refobjid <> r.ev_class
      UNION
        SELECT reads.reader, d.refobjid
        FROM reads
        JOIN pg_class v ON v.oid = reads.source AND v.relkind = 'v'
        JOIN pg_rewrite r ON r.ev_class = v.oid
        JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
        WHERE d.refclassid = 'pg_class'::regclass
          AND d.refobjid <> v.oid
    )
    SELECT DISTINCT reader.relname, source.relname
    FROM reads
    JOIN pg_class reader ON reader.oid = reads.reader
    JOIN pg_class source ON source.oid = reads.source
    WHERE source.relkind = 'm'
""")


async def view_reads(session: AsyncSession) -> dict[str, frozenset[str]]:
    """The materialised views each materialised view reads, by name, from the catalog.

    Including those it reads through a plain view. A view that reads no other
    is not a key.
    """
    reads: defaultdict[str, set[str]] = defaultdict(set)
    for reader, source in (await session.execute(_READS)).tuples():
        reads[reader].add(source)
    return {reader: frozenset(sources) for reader, sources in reads.items()}


def in_refresh_order(
    views: Sequence[MaterialisedView],
    reads: Mapping[str, frozenset[str]],
    *,
    only: str | None = None,
) -> list[MaterialisedView]:
    """``views``, each after every one of them it reads, and otherwise in their own order.

    With ``only``, that view and each view that reads it, directly or through
    another. Refreshing a view without the views that read it would leave them
    disagreeing with it. The views it reads are left as they are, as of their
    own last refresh, since only the one view was asked for.

    :param reads: What :func:`view_reads` returns.
    :raises ValueError: ``only`` is not one of ``views``.
    """
    names = {view.name for view in views}
    if only is None:
        wanted = names
    elif only not in names:
        raise ValueError(f"{only!r} is not a materialised view")
    else:
        wanted = {only}
        while more := {reader for reader in names - wanted if reads.get(reader, set()) & wanted}:
            wanted |= more

    pending = [view for view in views if view.name in wanted]
    ordered: list[MaterialisedView] = []
    while pending:
        # There is always one: Postgres cannot make a cycle of views. A view
        # can only read one that already exists, and a materialised view's
        # query cannot be replaced afterwards.
        done = {view.name for view in ordered}
        ready = next(view for view in pending if reads.get(view.name, set()) & wanted <= done)
        ordered.append(ready)
        pending.remove(ready)
    return ordered


async def refresh_order(
    session: AsyncSession, *, only: str | None = None
) -> list[MaterialisedView]:
    """What ``refresh-views`` refreshes, in the order it does: :func:`in_refresh_order`
    of every view, with the dependencies the catalog has now."""
    return in_refresh_order(MATERIALISED_VIEWS, await view_reads(session), only=only)


@dataclass(frozen=True, slots=True)
class Refreshed:
    """What :func:`refresh_views` did to one view."""

    name: str
    rows: int
    concurrently: bool
    """False for a view never populated before, which cannot be refreshed
    concurrently, and for every view in a refresh asked not to be."""
    seconds: float
    refreshed_at: datetime
    """As ``matview_refresh`` records it."""


async def refresh_views(
    session: AsyncSession,
    views: Sequence[MaterialisedView] | None = None,
    *,
    concurrently: bool = True,
    run_id: uuid.UUID | None = None,
) -> list[Refreshed]:
    """Refresh ``views`` in the caller's transaction, and record each in ``matview_refresh``.

    Takes the recompute lock first, and holds it until the caller commits, so
    no rebuild lands between one view's refresh and the next. Readers see each
    view as it was until the commit, and refreshed after it. The views'
    ``matview_refresh`` rows commit with them.

    Times are the database's ``clock_timestamp()``, for ``ingestion_run``'s
    reason: a duration never mixes two machines' clocks.

    :param views: In the order to refresh them, as :func:`refresh_order` gives
        them. By default every view, in that order.
    :param concurrently: Readers go on reading each view as it was. Without
        it, each view is refreshed faster, but locked against reads from its
        refresh until the caller commits, which is after the last view. A view
        never populated is refreshed plainly either way.
    :param run_id: The run doing this, for ``matview_refresh.run_id``.
    """
    await hold_recompute_lock(session)
    if views is None:
        views = await refresh_order(session)

    refreshed: list[Refreshed] = []
    for view in views:
        started, populated = (
            await session.execute(
                text(
                    "SELECT clock_timestamp(), (SELECT ispopulated FROM pg_matviews "
                    "WHERE schemaname = current_schema() AND matviewname = :name)"
                ),
                {"name": view.name},
            )
        ).one()
        concurrent = concurrently and bool(populated)
        # The name is one of ours, not input: there is nothing to quote.
        how = "CONCURRENTLY " if concurrent else ""
        await session.execute(text(f"REFRESH MATERIALIZED VIEW {how}{view.name}"))
        recorded = insert(MatviewRefresh).values(
            view_name=view.name, refreshed_at=func.clock_timestamp(), run_id=run_id
        )
        finished = await session.scalar(
            recorded.on_conflict_do_update(
                index_elements=[MatviewRefresh.view_name],
                set_={
                    "refreshed_at": recorded.excluded.refreshed_at,
                    "run_id": recorded.excluded.run_id,
                },
            ).returning(MatviewRefresh.refreshed_at)
        )
        assert finished is not None
        rows = await session.scalar(select(func.count()).select_from(view.handle))
        refreshed.append(
            Refreshed(
                name=view.name,
                rows=rows or 0,
                concurrently=concurrent,
                seconds=(finished - started).total_seconds(),
                refreshed_at=finished,
            )
        )
    return refreshed


async def last_refreshed(session: AsyncSession) -> dict[str, datetime | None]:
    """When each materialised view was last refreshed, by name. Every view is a key.

    ``None`` for a view not refreshed since ``matview_refresh`` was created,
    whose last refresh is unknown. This is what lets a response built from a
    view say how old it is: :func:`app.api.meta.refreshed_at` serves it.
    """
    rows = await session.execute(select(MatviewRefresh.view_name, MatviewRefresh.refreshed_at))
    recorded = {name: refreshed_at for name, refreshed_at in rows.tuples()}
    return {view.name: recorded.get(view.name) for view in MATERIALISED_VIEWS}
