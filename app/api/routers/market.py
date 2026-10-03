"""Market endpoints: the landing page's lists, the recent-filings feed, and the screener.

``GET /v1/market/top-holdings``
    The most widely or most heavily held stocks in a quarter, from
    ``mv_consensus_holdings``.
``GET /v1/market/top-buys``, ``/top-sells``, ``/new-positions``
    The stocks most bought, most sold, and most often opened, from
    ``mv_quarter_flows``, or ``mv_year_flows`` with ``?period_type=year``.
``GET /v1/market/activity``
    Filings, newest first, each with its period's position count and largest
    trade, from ``mv_filing_feed``. Keyset-paged.
``GET /v1/flows``
    The screener: every stock the filers traded or held on through the period,
    filtered and sorted, from the same flows views. Keyset-paged.

Only the views are aggregated. What else a row shows, the stock's name and
the filer's, is joined to the rows returned, never to the rows they are picked
from, so each endpoint is one index probe or one sort of a period's rows of a
view: a few thousand. Everything is as of the views' last refresh, which
``meta.refreshed_at`` states.

Gross and net
-------------
A year's top buys ranked by net value leave out every position bought and then
sold within the year, and those are often the year's largest accumulations. So
a flow row carries both ``gross_bought_usd`` and ``net_value_usd``, and the top
lists rank by either: ``?metric=value`` is gross, the dollars bought (or sold),
and ``?metric=net_value`` is net. See :mod:`app.derived.views`.

A year
------
``?period_type=year`` is the four quarters ending at ``?period``: the calendar
year for a Q4, the trailing twelve months for any other. ``meta.quarters``
names them, and ``meta.coverage`` is the last one's, which is the one still
filling in.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import (
    BindParameter,
    ColumnElement,
    Numeric,
    Row,
    Select,
    Subquery,
    TableClause,
    UnaryExpression,
    and_,
    exists,
    func,
    literal,
    null,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.base import ReadOnlyColumnCollection

from app.api.deps import PageParamsDep, SessionDep
from app.api.meta import period_meta, refreshed_at, unscoped_meta
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.routers.portfolio import SortOrder
from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.market import FeedFiling, LargestChange, MarketHolding, StockFlow
from app.api.schemas.types import Period
from app.core.periods import quarter_label, quarters_ending
from app.db.models import Filer, Filing, Security
from app.derived.views import (
    CONSENSUS_HOLDINGS,
    FILER_SUMMARY,
    FILING_FEED,
    QUARTER_FLOWS,
    YEAR_FLOWS,
)

router = APIRouter(tags=["market"])

# What the query builders take: a value, or a bind parameter left for
# ``make explain`` to fill in.
PeriodEnd = date | BindParameter[date]
type Columns = ReadOnlyColumnCollection[str, Any]

#: The longest a top list can be asked to be.
MAX_TOP: Final = 100
DEFAULT_TOP: Final = 20
#: The quarters a year is.
YEAR_QUARTERS: Final = 4

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)
# Where nobody holds the stock any more: zero, at the scale the column has.
_NO_DOLLARS: Final = Decimal("0.00")


class PeriodType(StrEnum):
    QUARTER = "quarter"
    YEAR = "year"


#: The flows view each period type reads. They have the same columns.
FLOWS: Final = {PeriodType.QUARTER: QUARTER_FLOWS, PeriodType.YEAR: YEAR_FLOWS}


class HoldingMetric(StrEnum):
    HOLDERS = "holders"
    VALUE = "value"


class FlowMetric(StrEnum):
    VALUE = "value"
    NET_VALUE = "net_value"
    HOLDERS = "holders"


class FlowSort(StrEnum):
    NET_VALUE = "net_value"
    GROSS_BOUGHT = "gross_bought"
    GROSS_SOLD = "gross_sold"
    BUYERS = "buyers"
    SELLERS = "sellers"
    NEW_POSITIONS = "new_positions"
    EXITS = "exits"
    HOLDERS = "holders"
    VALUE_HELD = "value_held"


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


# --- the period -----------------------------------------------------------------


async def _period(
    session: AsyncSession, view: TableClause, period: date | None, *, what: str
) -> date:
    """``period``, or the latest one ``view`` has.

    :param what: What ``view`` holds, for the 404: "holdings", "flows".
    :raises HTTPException: 404 when the view is empty, or has nothing for
        ``period``. An empty list would read as a quarter nobody traded in.
    """
    # Both through the view's unique index, which leads with the period.
    latest = select(func.max(view.c.period_of_report)).scalar_subquery()
    published = exists().where(view.c.period_of_report == period) if period is not None else null()
    row = (
        await session.execute(select(latest.label("latest"), published.label("published")))
    ).one()
    if row.latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {what} are published yet.",
        )
    latest_period: date = row.latest
    if period is None:
        return latest_period
    if not row.published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No {what} are published for {quarter_label(period)}. The latest quarter "
                f"with them is {quarter_label(latest_period)}."
            ),
        )
    return period


async def _meta(
    session: AsyncSession,
    period: date,
    *views: TableClause,
    period_type: PeriodType = PeriodType.QUARTER,
) -> Meta:
    """``period_meta``, with the year's quarters and when ``views`` were refreshed.

    ``mv_filer_summary`` is always one of the views: the coverage is read from it.
    """
    meta = await period_meta(session, period)
    if period_type is PeriodType.YEAR:
        meta.quarters = [quarter_label(q) for q in quarters_ending(period, YEAR_QUARTERS)]
    meta.refreshed_at = await refreshed_at(session, *views, FILER_SUMMARY)
    return meta


# --- holdings -------------------------------------------------------------------


def _holdings_order(c: Columns, metric: HoldingMetric) -> list[UnaryExpression[Any]]:
    """Largest first. Holders tie often, so the larger holding leads a tie."""
    lead = [c.holder_count.desc()] if metric is HoldingMetric.HOLDERS else []
    return [*lead, c.total_value_usd.desc(), c.security_id.desc()]


def top_holdings_query(period: PeriodEnd, metric: HoldingMetric, limit: int) -> Select[Any]:
    """The ``limit`` stocks held by the most filers, or for the most dollars, named."""
    consensus = CONSENSUS_HOLDINGS
    top = (
        select(consensus)
        .where(consensus.c.period_of_report == period)
        .order_by(*_holdings_order(consensus.c, metric))
        .limit(limit)
        .cte("top")
    )
    return _named(top).order_by(*_holdings_order(top.c, metric))


# --- flows ----------------------------------------------------------------------


def _flow_rows(flows: TableClause, period: PeriodEnd) -> Subquery:
    """The period's rows of ``flows``, with who holds the stock at its end beside each.

    Every stock any filer changed, held on through, or exited. One with no
    holders left has no consensus row, and is held by none for nothing.
    """
    consensus = CONSENSUS_HOLDINGS
    return (
        select(
            flows.c.security_id,
            flows.c.bought_value_usd,
            flows.c.sold_value_usd,
            flows.c.net_value_usd,
            flows.c.net_shares,
            flows.c.buyer_count,
            flows.c.seller_count,
            flows.c.new_positions,
            flows.c.exits,
            func.coalesce(consensus.c.holder_count, 0).label("holder_count"),
            func.coalesce(consensus.c.total_value_usd, literal(_NO_DOLLARS, Numeric)).label(
                "total_value_usd"
            ),
        )
        .select_from(flows)
        .outerjoin(
            consensus,
            and_(
                consensus.c.period_of_report == flows.c.period_of_report,
                consensus.c.security_id == flows.c.security_id,
            ),
        )
        .where(flows.c.period_of_report == period)
        .subquery("flows")
    )


def ranked_flows_query(
    flows: TableClause,
    period: PeriodEnd,
    limit: int,
    *,
    rank_by: str,
    descending: bool = True,
    then: str,
) -> Select[Any]:
    """The ``limit`` stocks first by ``rank_by``, named, in that order.

    Only stocks on the right side of zero: above it ranked largest first,
    below it ranked most negative first. So the top buys by net value are the
    stocks bought on balance, and never a stock merely sold least.

    :param rank_by: A column of :func:`_flow_rows`.
    :param then: The column, largest first, that orders a tie.
    """
    rows = _flow_rows(flows, period)

    def order(c: Columns) -> list[UnaryExpression[Any]]:
        lead = c[rank_by].desc() if descending else c[rank_by].asc()
        return [lead, c[then].desc(), c.security_id.desc()]

    beyond_zero = rows.c[rank_by] > 0 if descending else rows.c[rank_by] < 0
    top = select(rows).where(beyond_zero).order_by(*order(rows.c)).limit(limit).cte("top")
    return _named(top).order_by(*order(top.c))


def top_buys_query(
    flows: TableClause, period: PeriodEnd, metric: FlowMetric, limit: int
) -> Select[Any]:
    rank_by = {
        FlowMetric.VALUE: "bought_value_usd",
        FlowMetric.NET_VALUE: "net_value_usd",
        FlowMetric.HOLDERS: "buyer_count",
    }[metric]
    return ranked_flows_query(flows, period, limit, rank_by=rank_by, then="bought_value_usd")


def top_sells_query(
    flows: TableClause, period: PeriodEnd, metric: FlowMetric, limit: int
) -> Select[Any]:
    rank_by, descending = {
        FlowMetric.VALUE: ("sold_value_usd", True),
        FlowMetric.NET_VALUE: ("net_value_usd", False),
        FlowMetric.HOLDERS: ("seller_count", True),
    }[metric]
    return ranked_flows_query(
        flows, period, limit, rank_by=rank_by, descending=descending, then="sold_value_usd"
    )


def new_positions_query(flows: TableClause, period: PeriodEnd, limit: int) -> Select[Any]:
    return ranked_flows_query(
        flows, period, limit, rank_by="new_positions", then="bought_value_usd"
    )


#: Each sort, as the column of :func:`_flow_rows` it orders by and that column's type.
_SORTED_BY: Final[dict[FlowSort, tuple[str, type[Decimal] | type[int]]]] = {
    FlowSort.NET_VALUE: ("net_value_usd", Decimal),
    FlowSort.GROSS_BOUGHT: ("bought_value_usd", Decimal),
    FlowSort.GROSS_SOLD: ("sold_value_usd", Decimal),
    FlowSort.BUYERS: ("buyer_count", int),
    FlowSort.SELLERS: ("seller_count", int),
    FlowSort.NEW_POSITIONS: ("new_positions", int),
    FlowSort.EXITS: ("exits", int),
    FlowSort.HOLDERS: ("holder_count", int),
    FlowSort.VALUE_HELD: ("total_value_usd", Decimal),
}


def flows_query(
    flows: TableClause,
    period: PeriodEnd,
    page: PageParams,
    *,
    sort: FlowSort = FlowSort.NET_VALUE,
    order: SortOrder = SortOrder.DESC,
    direction: Direction | None = None,
    min_investors: int | None = None,
    min_value: Decimal | None = None,
) -> tuple[Select[Any], Keyset]:
    """One page of the screener, named, and its keyset.

    Each sort, then the dollars held, so a tie shows the larger stock first,
    then the security. Every key runs the same way, which pages with one row
    comparison.
    """
    rows = _flow_rows(flows, period)
    c = rows.c
    descending = order is SortOrder.DESC

    names = [_SORTED_BY[sort], _SORTED_BY[FlowSort.VALUE_HELD], ("security_id", int)]
    keys = [
        SortKey(name, c[name], kind, descending=descending)
        for name, kind in dict(names).items()  # value_held once, when it is the sort
    ]
    keyset = Keyset(f"flows.{sort.value}.{order.value}", tuple(keys))

    conditions: list[ColumnElement[bool]] = []
    if direction is Direction.BUY:
        conditions.append(c.net_value_usd > 0)
    elif direction is Direction.SELL:
        conditions.append(c.net_value_usd < 0)
    if min_investors is not None:
        conditions.append(c.holder_count >= min_investors)
    if min_value is not None:
        conditions.append(c.total_value_usd >= min_value)

    page_rows = page_statement(select(rows).where(*conditions), keyset, page).cte("page")
    return _named(page_rows).order_by(*keyset.order_by(on=page_rows)), keyset


def _named(rows: Any) -> Select[Any]:
    """``rows``, each with its stock's CUSIP, ticker and name."""
    return (
        select(rows, Security.cusip, Security.ticker, Security.name.label("issuer_name"))
        .select_from(rows)
        .join(Security, Security.id == rows.c.security_id)
    )


# --- the feed -------------------------------------------------------------------

#: Newest filing first. Both descending, so a page boundary is one row
#: comparison, which the (filed_at, filing_id) index answers backwards.
FEED_KEYSET: Final = Keyset(
    "market-activity",
    (
        SortKey("filed_at", FILING_FEED.c.filed_at, datetime, descending=True),
        SortKey("filing_id", FILING_FEED.c.filing_id, int, descending=True),
    ),
)


def feed_query(page: PageParams) -> Select[Any]:
    """One page of the feed, with each filing's form, filer and largest trade's stock."""
    page_rows = page_statement(select(FILING_FEED), FEED_KEYSET, page).cte("page")
    return (
        select(
            page_rows,
            Filing.accession_no,
            Filing.form_type,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            Security.cusip,
            Security.ticker,
            Security.name.label("issuer_name"),
        )
        .select_from(page_rows)
        .join(Filing, Filing.id == page_rows.c.filing_id)
        .join(Filer, Filer.id == page_rows.c.filer_id)
        .outerjoin(Security, Security.id == page_rows.c.largest_security_id)
        .order_by(*FEED_KEYSET.order_by(on=page_rows))
    )


# --- the routes -----------------------------------------------------------------

PeriodParam = Annotated[
    Period | None,
    Query(
        description=(
            "The quarter, as `2026Q1` or `2026-03-31`, or with `period_type=year` the "
            "last of the year's four. Defaults to the latest one published, which "
            "`meta.period` states."
        )
    ),
]
HoldingsPeriodParam = Annotated[
    Period | None,
    Query(
        description=(
            "The quarter, as `2026Q1` or `2026-03-31`. Defaults to the latest one "
            "published, which `meta.period` states."
        )
    ),
]
PeriodTypeParam = Annotated[
    PeriodType,
    Query(
        description=(
            "`quarter`, or `year`: the four quarters ending at `period`, which "
            "`meta.quarters` names. A filer is counted once however many of them it "
            "traded in, and the dollars are the four quarters' added up."
        )
    ),
]
LimitParam = Annotated[
    int,
    Query(ge=1, le=MAX_TOP, description=f"How many stocks. Above {MAX_TOP} is refused."),
]
FlowMetricParam = Annotated[
    FlowMetric,
    Query(
        description=(
            "`value`: gross dollars traded that way, so a position bought and sold "
            "again within a year still counts. `net_value`: bought less sold, so only "
            "stocks traded that way on balance. `holders`: filers trading that way."
        )
    ),
]
_NOT_PUBLISHED: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_404_NOT_FOUND: {"description": "Nothing published for the period."}
}


@router.get(
    "/market/top-holdings",
    response_model=Envelope[MarketHolding],
    summary="The most widely or most heavily held stocks",
    responses=_NOT_PUBLISHED,
)
async def read_top_holdings(
    session: SessionDep,
    period: HoldingsPeriodParam = None,
    metric: Annotated[
        HoldingMetric,
        Query(description="`holders`: held by the most filers. `value`: the most dollars held."),
    ] = HoldingMetric.HOLDERS,
    limit: LimitParam = DEFAULT_TOP,
) -> Envelope[MarketHolding]:
    """The top stocks at a quarter end. Not paginated."""
    period = await _period(session, CONSENSUS_HOLDINGS, period, what="holdings")
    rows = await session.execute(top_holdings_query(period, metric, limit))
    return Envelope(
        data=[
            MarketHolding(
                cusip=row.cusip,
                ticker=row.ticker,
                issuer_name=row.issuer_name,
                holder_count=row.holder_count,
                total_shares=row.total_shares,
                total_value_usd=row.total_value_usd,
                avg_weight_pct=row.avg_weight_pct,
                median_weight_pct=row.median_weight_pct,
                value_rank=row.value_rank,
            )
            for row in rows
        ],
        meta=await _meta(session, period, CONSENSUS_HOLDINGS),
    )


@router.get(
    "/market/top-buys",
    response_model=Envelope[StockFlow],
    summary="The stocks the tracked investors bought most",
    responses=_NOT_PUBLISHED,
)
async def read_top_buys(
    session: SessionDep,
    period: PeriodParam = None,
    period_type: PeriodTypeParam = PeriodType.QUARTER,
    metric: FlowMetricParam = FlowMetric.VALUE,
    limit: LimitParam = DEFAULT_TOP,
) -> Envelope[StockFlow]:
    """Most bought first. Not paginated."""
    flows = FLOWS[period_type]
    period = await _period(session, flows, period, what="flows")
    rows = await session.execute(top_buys_query(flows, period, metric, limit))
    return await _flows_envelope(session, rows, period, period_type)


@router.get(
    "/market/top-sells",
    response_model=Envelope[StockFlow],
    summary="The stocks the tracked investors sold most",
    responses=_NOT_PUBLISHED,
)
async def read_top_sells(
    session: SessionDep,
    period: PeriodParam = None,
    period_type: PeriodTypeParam = PeriodType.QUARTER,
    metric: FlowMetricParam = FlowMetric.VALUE,
    limit: LimitParam = DEFAULT_TOP,
) -> Envelope[StockFlow]:
    """Most sold first. Not paginated."""
    flows = FLOWS[period_type]
    period = await _period(session, flows, period, what="flows")
    rows = await session.execute(top_sells_query(flows, period, metric, limit))
    return await _flows_envelope(session, rows, period, period_type)


@router.get(
    "/market/new-positions",
    response_model=Envelope[StockFlow],
    summary="The stocks the most tracked investors opened a position in",
    responses=_NOT_PUBLISHED,
)
async def read_new_positions(
    session: SessionDep,
    period: PeriodParam = None,
    period_type: PeriodTypeParam = PeriodType.QUARTER,
    limit: LimitParam = DEFAULT_TOP,
) -> Envelope[StockFlow]:
    """Most often opened first, then most bought. A filer's first period is
    not counted: every position is new to us there, not bought. Not paginated."""
    flows = FLOWS[period_type]
    period = await _period(session, flows, period, what="flows")
    rows = await session.execute(new_positions_query(flows, period, limit))
    return await _flows_envelope(session, rows, period, period_type)


@router.get(
    "/market/activity",
    response_model=Envelope[FeedFiling],
    summary="Recent filings, with what each period holds and its largest trade",
)
async def read_market_activity(session: SessionDep, page: PageParamsDep) -> Envelope[FeedFiling]:
    """Filings newest first: those a published period was built from. One
    withheld as suspect is left out until its period is published. Each row
    names its period, so ``meta`` names none."""
    statement = feed_query(page)
    rows, page_info = page_of(list(await session.execute(statement)), FEED_KEYSET, page)
    meta = unscoped_meta()
    meta.refreshed_at = await refreshed_at(session, FILING_FEED)
    return Envelope(data=[_feed_filing(row) for row in rows], meta=meta, page=page_info)


@router.get(
    "/flows",
    response_model=Envelope[StockFlow],
    summary="Screen stocks by what the tracked investors bought and sold",
    responses={
        **_NOT_PUBLISHED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "A parameter is malformed, or `sector` was asked for."
        },
    },
)
async def read_flows(
    session: SessionDep,
    page: PageParamsDep,
    period: PeriodParam = None,
    period_type: PeriodTypeParam = PeriodType.QUARTER,
    sector: Annotated[
        str | None,
        Query(
            description=(
                "Not available yet: no source of sectors is loaded, so every stock's "
                "`sector` is null, and asking for one is a 422 rather than an empty page."
            )
        ),
    ] = None,
    min_investors: Annotated[
        int | None,
        Query(ge=0, description="Only stocks held by at least this many filers at the period end."),
    ] = None,
    min_value: Annotated[
        Decimal | None,
        Query(
            ge=0,
            description=(
                "Only stocks the filers hold at least this many dollars of at the period end."
            ),
        ),
    ] = None,
    direction: Annotated[
        Direction | None,
        Query(description="`buy`: net bought (`net_value_usd` above zero). `sell`: net sold."),
    ] = None,
    sort: Annotated[
        FlowSort,
        Query(
            description=(
                "`net_value`, `gross_bought`, `gross_sold`, `buyers`, `sellers`, "
                "`new_positions`, `exits`, `holders`, or `value_held`. Ties go to the "
                "larger holding."
            )
        ),
    ] = FlowSort.NET_VALUE,
    order: Annotated[SortOrder, Query(description="Largest first by default.")] = SortOrder.DESC,
) -> Envelope[StockFlow]:
    """Every stock traded or held through the period that passes the filters,
    paginated. Exits included: a stock every holder sold has no holders left."""
    if sector is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Screening by sector is not available yet: no source of sectors is "
                "loaded, so no stock has one. Leave sector out."
            ),
        )
    flows = FLOWS[period_type]
    period = await _period(session, flows, period, what="flows")
    statement, keyset = flows_query(
        flows,
        period,
        page,
        sort=sort,
        order=order,
        direction=direction,
        min_investors=min_investors,
        min_value=min_value,
    )
    rows, page_info = page_of(list(await session.execute(statement)), keyset, page)
    envelope = await _flows_envelope(session, rows, period, period_type)
    envelope.page = page_info
    return envelope


async def _flows_envelope(
    session: AsyncSession, rows: Any, period: date, period_type: PeriodType
) -> Envelope[StockFlow]:
    meta = await _meta(
        session, period, FLOWS[period_type], CONSENSUS_HOLDINGS, period_type=period_type
    )
    return Envelope(data=[_flow(row) for row in rows], meta=meta)


def _flow(row: Row[Any]) -> StockFlow:
    return StockFlow(
        cusip=row.cusip,
        ticker=row.ticker,
        issuer_name=row.issuer_name,
        gross_bought_usd=row.bought_value_usd,
        gross_sold_usd=row.sold_value_usd,
        net_value_usd=row.net_value_usd,
        net_shares=row.net_shares,
        buyer_count=row.buyer_count,
        seller_count=row.seller_count,
        new_positions=row.new_positions,
        exits=row.exits,
        holder_count=row.holder_count,
        total_value_usd=row.total_value_usd,
    )


def _feed_filing(row: Row[Any]) -> FeedFiling:
    largest = None
    if row.largest_security_id is not None:
        largest = LargestChange(
            cusip=row.cusip,
            ticker=row.ticker,
            issuer_name=row.issuer_name,
            action=row.largest_action,
            shares_delta=row.largest_shares_delta,
            traded_value_usd=row.largest_traded_value_usd,
        )
    return FeedFiling(
        accession_no=row.accession_no,
        form_type=row.form_type,
        period=row.period_of_report,
        filed_at=row.filed_at,
        slug=row.slug,
        display_name=row.display_name,
        position_count=row.position_count,
        largest_change=largest,
    )
