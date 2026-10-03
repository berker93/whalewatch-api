"""One investor's portfolio, what it did, and how its book has grown.

``GET /v1/investors/{slug}/portfolio``
    One period's positions, read live from ``position_snapshot`` with the
    period's ``position_change`` row beside each. Defaults to the latest
    period the filer has published, which ``meta`` then states. Not the
    universe's latest period, which a filer that has not filed for yet would
    answer with nothing. Keyset-paged: an index fund holds thousands of
    positions.
``GET /v1/investors/{slug}/activity``
    ``position_change`` over a range of periods: what was opened, added to,
    trimmed and exited. Holds are left out unless asked for, and so is the
    filer's first period, in which every position is ``new`` only because it
    is the first we have (:mod:`app.derived.views` leaves it out of the flows
    for the same reason).
``GET /v1/investors/{slug}/history``
    One row per quarter from ``mv_filer_summary``, as of the views' last
    refresh, like the investor detail it charts. Not paged: one filer's
    quarters are a few dozen.

First held period
-----------------
A position's ``first_period`` is ``MIN(period_of_report)`` over the filer and
the security. Per row, as a correlated subquery, that is one scan of the
filer's slice of the primary key per position: 120 ms for a 200-row page of
Two Sigma's latest period on the dev database's backfill (46,000 positions
over 14 periods), since the key leads with ``(filer_id, period_of_report)``
and the security is only a filter within it. Instead the filer's positions up
to the period are grouped by security once, in a CTE, and hash-joined to the
page: one index-only scan of the same slice, 6.5 ms for the same page.

Storing it as a column of ``position_snapshot`` would make it free to read,
but a period's ``first_period`` then depends on every earlier period, and a
scoped ``recompute`` of one ``(filer, period)`` would have to rewrite every
later period of the filer. At 6.5 ms for the largest filer that is not a
trade worth making yet.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import PlainValidator, WithJsonSchema
from sqlalchemy import (
    ARRAY,
    BindParameter,
    Numeric,
    Row,
    Select,
    Subquery,
    Text,
    and_,
    any_,
    case,
    cast,
    exists,
    func,
    literal,
    null,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import Lifetime, cached
from app.api.deps import PageParamsDep, SessionDep
from app.api.errors import INVALID, INVALID_CURSOR, invalid, not_found
from app.api.meta import period_meta, unscoped_meta
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.routers.investors import SlugParam
from app.api.schemas.envelope import Envelope
from app.api.schemas.portfolio import (
    Activity,
    ActivityEnvelope,
    HistoryPoint,
    HistoryPointEnvelope,
    PortfolioPosition,
    PortfolioPositionEnvelope,
)
from app.api.schemas.types import Period
from app.core.periods import quarter_label, quarters_between
from app.db.models import Filer, Holding, Security
from app.db.models.holding import QUANTITY
from app.db.models.position_change import CHANGE_PCT, ChangeAction, PositionChange
from app.db.models.position_snapshot import WEIGHT_PCT, PositionSnapshot
from app.db.queries.amendments import period_concerns
from app.db.queries.effective import EFFECTIVE_FILING
from app.derived.views import FILER_SUMMARY, traded_usd

router = APIRouter(tags=["investors"])

# What the query builders take: a value, or a bind parameter left for
# ``make explain`` to fill in, as it does for every query it reads from here.
FilerId = int | BindParameter[int]
PeriodEnd = date | BindParameter[date]


class PortfolioSort(StrEnum):
    """What a portfolio is ordered by."""

    WEIGHT = "weight"
    VALUE = "value"
    SHARES = "shares"
    CHANGE = "change"


class SortOrder(StrEnum):
    """Ascending or descending."""

    ASC = "asc"
    DESC = "desc"


#: Above every ``shares_delta_pct`` the column can hold (``numeric(28, 6)``):
#: where ``change`` sorts a position that grew from nothing, which has no
#: percentage. Largest first, a new position is the biggest increase there is.
_FROM_NOTHING: Final = Decimal(10) ** 22
#: Below every real one, falls included (they stop at -100): where ``change``
#: sorts an option line, which has no change at all.
_NO_CHANGE: Final = -_FROM_NOTHING
#: Below every real weight: where ``weight`` sorts a row without one, so an
#: option line comes after every stock position under ``desc``.
_NO_WEIGHT: Final = Decimal(-1)

#: What ``?action=`` means when it is left out: everything a filer did.
DEFAULT_ACTIONS: Final = frozenset(ChangeAction) - {ChangeAction.HOLD}


async def _filer_id(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(select(Filer.id).where(Filer.slug == slug))
    if filer_id is None:
        raise _unknown(slug)
    return filer_id


def _unknown(slug: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No investor {slug!r}. GET /v1/investors lists every one.",
    )


async def _portfolio_period(
    session: AsyncSession, slug: str, period: date | None
) -> tuple[int, date]:
    """The filer, and the period to serve: ``period``, or its latest published one.

    :raises HTTPException: 404 for an unknown slug, for a filer with nothing
        published, and for a period the filer has not published. An empty
        portfolio would read as a manager who sold everything.
    """
    snapshot = PositionSnapshot
    # max() over the primary key's (filer_id, period_of_report) prefix: one
    # entry read, backwards.
    latest = (
        select(func.max(snapshot.period_of_report))
        .where(snapshot.filer_id == Filer.id)
        .scalar_subquery()
    )
    published = (
        exists().where(snapshot.filer_id == Filer.id, snapshot.period_of_report == period)
        if period is not None
        else null()
    )
    row = (
        await session.execute(
            select(Filer.id, latest.label("latest"), published.label("published")).where(
                Filer.slug == slug
            )
        )
    ).one_or_none()
    if row is None:
        raise _unknown(slug)
    if row.latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nothing is published for {slug!r} yet.",
        )
    if period is None:
        return row.id, row.latest
    if not row.published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No portfolio is published for {slug!r} for {quarter_label(period)}: not "
                f"filed, not loaded, or withheld. Its latest is {quarter_label(row.latest)}, "
                f"and GET /v1/investors/{slug}/history lists every quarter."
            ),
        )
    return row.id, period


def _positions(filer_id: FilerId, period: PeriodEnd, *, include_options: bool) -> Subquery:
    """The period's positions, one row each, with every sort key as a column.

    Common stock from ``position_snapshot``, joined to its change. With
    ``include_options``, the option lines of the filings the period was
    published from as well, summed per security and kind as the snapshot sums
    stock. Option lines in shares only: a ``PRN`` line counts principal.
    """
    snapshot, change = PositionSnapshot, PositionChange
    rows: Any = (
        select(
            snapshot.security_id,
            null().cast(Text).label("put_call"),
            snapshot.shares,
            snapshot.value_usd,
            snapshot.weight_pct,
            change.action,
            change.shares_delta,
            change.shares_delta_pct,
            change.weight_delta,
        )
        .select_from(snapshot)
        # Outer, though every snapshot row has a change row (reconcile checks
        # it): a missing one should cost the row its deltas, not the row.
        .outerjoin(
            change,
            and_(
                change.filer_id == snapshot.filer_id,
                change.period_of_report == snapshot.period_of_report,
                change.security_id == snapshot.security_id,
            ),
        )
        .where(snapshot.filer_id == filer_id, snapshot.period_of_report == period)
    )
    if include_options:
        effective = EFFECTIVE_FILING
        options = (
            select(
                Holding.security_id,
                Holding.put_call,
                func.sum(Holding.shares).label("shares"),
                func.sum(Holding.value_usd).label("value_usd"),
                null().cast(WEIGHT_PCT),
                null().cast(Text),
                null().cast(QUANTITY),
                null().cast(CHANGE_PCT),
                null().cast(WEIGHT_PCT),
            )
            .select_from(effective)
            .join(Holding, Holding.filing_id == effective.c.filing_id)
            .where(
                effective.c.filer_id == filer_id,
                effective.c.period_of_report == period,
                Holding.put_call.is_not(None),
                Holding.sshprnamt_type == "SH",
            )
            .group_by(Holding.security_id, Holding.put_call)
        )
        rows = union_all(rows, options)
    held = rows.subquery("held")

    return select(
        held,
        func.coalesce(held.c.weight_pct, literal(_NO_WEIGHT, Numeric)).label("weight_key"),
        case(
            (held.c.action.is_(None), literal(_NO_CHANGE, Numeric)),
            (held.c.shares_delta_pct.is_(None), literal(_FROM_NOTHING, Numeric)),
            else_=held.c.shares_delta_pct,
        ).label("change_key"),
        # Unique with security_id: a security is one stock row, one call row
        # and one put row at most.
        func.coalesce(held.c.put_call, "").label("put_call_key"),
    ).subquery("positions")


def _portfolio_keyset(positions: Subquery, sort: PortfolioSort, order: SortOrder) -> Keyset:
    """Each sort, then the value, so ties show the larger position first, and
    then the row's identity. Every key runs the same way, which pages with one
    row comparison."""
    descending = order is SortOrder.DESC
    c = positions.c

    def key(name: str, kind: type[Decimal] | type[int] | type[str]) -> SortKey:
        return SortKey(name, c[name], kind, descending=descending)

    lead = {
        # Within a period weight orders as value does. weight_key is there
        # for the rows with no weight.
        PortfolioSort.WEIGHT: [key("weight_key", Decimal)],
        PortfolioSort.VALUE: [],
        PortfolioSort.SHARES: [key("shares", Decimal)],
        PortfolioSort.CHANGE: [key("change_key", Decimal)],
    }[sort]
    return Keyset(
        f"investor-portfolio.{sort.value}.{order.value}",
        (
            *lead,
            key("value_usd", Decimal),
            key("security_id", int),
            key("put_call_key", str),
        ),
    )


def portfolio_query(
    filer_id: FilerId,
    period: PeriodEnd,
    page: PageParams,
    *,
    sort: PortfolioSort = PortfolioSort.WEIGHT,
    order: SortOrder = SortOrder.DESC,
    include_options: bool = False,
) -> tuple[Select[Any], Keyset]:
    """One page of the portfolio, named and with ``first_period``, and its keyset."""
    positions = _positions(filer_id, period, include_options=include_options)
    keyset = _portfolio_keyset(positions, sort, order)
    page_rows = page_statement(select(positions), keyset, page).cte("page")

    # See the module docstring: grouped once, not looked up per row.
    snapshot = PositionSnapshot
    first_held = (
        select(snapshot.security_id, func.min(snapshot.period_of_report).label("first_period"))
        .where(snapshot.filer_id == filer_id, snapshot.period_of_report <= period)
        .group_by(snapshot.security_id)
        .cte("first_held")
    )

    statement = (
        select(
            page_rows,
            Security.cusip,
            Security.ticker,
            Security.name.label("issuer_name"),
            first_held.c.first_period,
        )
        .select_from(page_rows)
        .join(Security, Security.id == page_rows.c.security_id)
        .outerjoin(
            first_held,
            and_(
                first_held.c.security_id == page_rows.c.security_id,
                page_rows.c.put_call.is_(None),
            ),
        )
        .order_by(*keyset.order_by(on=page_rows))
    )
    return statement, keyset


#: Latest period first, then the largest trade, then the security.
ACTIVITY_KEYSET: Final = Keyset(
    "investor-activity",
    (
        SortKey(
            "period_of_report", PositionChange.period_of_report.expression, date, descending=True
        ),
        SortKey(
            "traded_value_usd",
            # A hold traded nothing, as the views count it: its few shares of
            # drift are not a trade.
            case(
                (
                    PositionChange.action == ChangeAction.HOLD.value,
                    literal(Decimal("0.00"), Numeric),
                ),
                else_=func.round(traded_usd(), 2),
            ),
            Decimal,
            descending=True,
        ),
        SortKey("security_id", PositionChange.security_id.expression, int, descending=True),
    ),
)


def activity_query(
    filer_id: FilerId,
    page: PageParams,
    *,
    start: date | None = None,
    end: date | None = None,
    actions: frozenset[ChangeAction] = DEFAULT_ACTIONS,
) -> Select[Any]:
    """One page of the filer's changes, named, in :data:`ACTIVITY_KEYSET` order."""
    change = PositionChange
    keys = {key.name: key.expression for key in ACTIVITY_KEYSET.keys}
    rows = select(
        change.period_of_report,
        change.prev_period_of_report,
        change.security_id,
        change.action,
        change.shares,
        change.shares_delta,
        change.shares_delta_pct,
        change.value_usd,
        keys["traded_value_usd"].label("traded_value_usd"),
        change.weight_pct,
        change.weight_delta,
    ).where(
        change.filer_id == filer_id,
        # One array parameter rather than IN's one per action: the same
        # prepared statement for every set of actions. CAST rather than the
        # dialect's ::text[], which make explain's text() would misread.
        change.action == any_(cast(literal(sorted(a.value for a in actions)), ARRAY(Text))),
        # The first period we have: every position "new", none of them bought.
        change.prev_period_of_report.is_not(None),
    )
    if start is not None:
        rows = rows.where(change.period_of_report >= start)
    if end is not None:
        rows = rows.where(change.period_of_report <= end)

    page_rows = page_statement(rows, ACTIVITY_KEYSET, page).cte("page")
    return (
        select(
            page_rows,
            Security.cusip,
            Security.ticker,
            Security.name.label("issuer_name"),
        )
        .select_from(page_rows)
        .join(Security, Security.id == page_rows.c.security_id)
        .order_by(*ACTIVITY_KEYSET.order_by(on=page_rows))
    )


def history_query(slug: str | BindParameter[str]) -> Select[Any]:
    """The filer's id and each published period's summary, oldest first.

    No rows for an unknown slug, and one row with a null period for a filer
    with nothing published.
    """
    summary = FILER_SUMMARY
    return (
        select(
            Filer.id,
            summary.c.period_of_report,
            summary.c.portfolio_value_usd,
            summary.c.position_count,
            summary.c.top10_weight_pct,
        )
        .select_from(Filer)
        .outerjoin(summary, summary.c.filer_id == Filer.id)
        .where(Filer.slug == slug)
        .order_by(summary.c.period_of_report)
    )


def _actions(value: object) -> frozenset[ChangeAction]:
    """``?action=new,exit``, or ``?action=new&action=exit``, or both at once.

    FastAPI collects a set-typed query parameter as a list of its repeats,
    so this is handed a list, each item of which may be a comma-separated list.
    """
    if isinstance(value, frozenset):  # a default, never a query string
        return value
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("a comma-separated list of actions")
    names = [name.strip() for item in value for name in item.split(",")]
    known = {action.value for action in ChangeAction}
    if unknown := [name for name in names if name not in known]:
        raise ValueError(
            f"unknown action {', '.join(map(repr, unknown))}: "
            f"choose from {', '.join(action.value for action in ChangeAction)}"
        )
    return frozenset(ChangeAction(name) for name in names)


PeriodParam = Annotated[
    Period | None,
    Query(
        description=(
            "The quarter, as `2026Q1` or `2026-03-31`. Defaults to the latest one "
            "published for this investor, which `meta.period` states."
        )
    ),
]
IncludeOptions = Annotated[
    bool,
    Query(
        description=(
            "Add the period's option lines, one row per security and `put_call`. "
            "Their value is the notional of the underlying, so they have no weight, "
            "and weights stay shares of the common-stock portfolio."
        )
    ),
]
SortParam = Annotated[
    PortfolioSort,
    Query(
        description=(
            "`weight` (the same order as `value`, with option lines after stock), "
            "`value`, `shares`, or `change`: `shares_delta_pct`, with new positions "
            "as the largest increase and option lines last."
        )
    ),
]
OrderParam = Annotated[SortOrder, Query(description="Largest first by default.")]
RangeStart = Annotated[
    Period | None,
    Query(alias="from", description="The first quarter to include, as `2026Q1` or `2026-03-31`."),
]
RangeEnd = Annotated[
    Period | None,
    Query(alias="to", description="The last quarter to include, as `2026Q1` or `2026-03-31`."),
]
ActionsParam = Annotated[
    frozenset[ChangeAction] | None,
    PlainValidator(_actions),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["new,exit"]]}),
    Query(
        description=(
            "Any of `new`, `add`, `trim`, `hold`, `exit`, comma-separated or "
            "repeated. Every action but `hold` by default."
        )
    ),
]


@router.get(
    "/investors/{slug}/portfolio",
    operation_id="getInvestorPortfolio",
    response_model=PortfolioPositionEnvelope,
    summary="One investor's positions in one period",
    responses={
        **not_found("No investor with that slug, or nothing published for the period."),
        **INVALID_CURSOR,
        **INVALID,
    },
)
@cached(period="period")
async def read_portfolio(
    slug: SlugParam,
    session: SessionDep,
    page: PageParamsDep,
    period: PeriodParam = None,
    include_options: IncludeOptions = False,
    sort: SortParam = PortfolioSort.WEIGHT,
    order: OrderParam = SortOrder.DESC,
) -> Envelope[PortfolioPosition]:
    """The portfolio, paginated, with each position's change since the filer's
    previous published period.

    ``meta.caveats`` lists what ``audit-amendments`` would flag about the
    period, so a client sees it on every page.
    """
    filer_id, period = await _portfolio_period(session, slug, period)
    statement, keyset = portfolio_query(
        filer_id, period, page, sort=sort, order=order, include_options=include_options
    )
    rows, page_info = page_of(list(await session.execute(statement)), keyset, page)

    meta = await period_meta(session, period, filer_id=filer_id)
    meta.caveats = list(await period_concerns(session, slug=slug, period=period))

    return Envelope(data=[_position(row) for row in rows], meta=meta, page=page_info)


@router.get(
    "/investors/{slug}/activity",
    operation_id="getInvestorActivity",
    response_model=ActivityEnvelope,
    summary="What one investor opened, added to, trimmed and exited",
    responses={**not_found("No investor with that slug."), **INVALID_CURSOR, **INVALID},
)
@cached(period="end")
async def read_activity(
    slug: SlugParam,
    session: SessionDep,
    page: PageParamsDep,
    start: RangeStart = None,
    end: RangeEnd = None,
    action: ActionsParam = None,
) -> Envelope[Activity]:
    """Changes, newest period first and largest trade first within one.

    Each row names its period, so ``meta`` names none.
    """
    if start is not None and end is not None and start > end:
        raise invalid("from", f"from ({quarter_label(start)}) is after to ({quarter_label(end)}).")
    filer_id = await _filer_id(session, slug)
    statement = activity_query(
        filer_id, page, start=start, end=end, actions=action or DEFAULT_ACTIONS
    )
    rows, page_info = page_of(list(await session.execute(statement)), ACTIVITY_KEYSET, page)

    return Envelope(
        data=[
            Activity(
                cusip=row.cusip,
                ticker=row.ticker,
                issuer_name=row.issuer_name,
                period=row.period_of_report,
                prev_period=row.prev_period_of_report,
                action=row.action,
                shares=row.shares,
                shares_delta=row.shares_delta,
                shares_delta_pct=row.shares_delta_pct,
                value_usd=row.value_usd,
                traded_value_usd=row.traded_value_usd,
                weight_pct=row.weight_pct,
                weight_delta=row.weight_delta,
            )
            for row in rows
        ],
        meta=unscoped_meta(),
        page=page_info,
    )


@router.get(
    "/investors/{slug}/history",
    operation_id="getInvestorHistory",
    response_model=HistoryPointEnvelope,
    summary="One investor's portfolio value, positions and concentration, quarter by quarter",
    responses={**not_found("No investor with that slug."), **INVALID},
)
@cached(Lifetime.CURRENT_PERIOD)
async def read_history(slug: SlugParam, session: SessionDep) -> Envelope[HistoryPoint]:
    """Every quarter from the first published to the latest, oldest first.

    A quarter in between with nothing published is a row of nulls, so a chart
    of it shows the gap rather than drawing across it. Empty for a filer with
    nothing published. As of the views' last refresh. Not paginated.
    """
    rows = list(await session.execute(history_query(slug)))
    if not rows:
        raise _unknown(slug)

    published = {row.period_of_report: row for row in rows if row.period_of_report is not None}
    quarters = quarters_between(min(published), max(published)) if published else []
    data = []
    for quarter in quarters:
        row = published.get(quarter)
        data.append(
            HistoryPoint(period=quarter)
            if row is None
            else HistoryPoint(
                period=quarter,
                portfolio_value_usd=row.portfolio_value_usd,
                position_count=row.position_count,
                top10_weight_pct=row.top10_weight_pct,
            )
        )
    return Envelope(data=data, meta=unscoped_meta(), page=None)


def _position(row: Row[Any]) -> PortfolioPosition:
    return PortfolioPosition(
        cusip=row.cusip,
        ticker=row.ticker,
        issuer_name=row.issuer_name,
        put_call=row.put_call,
        shares=row.shares,
        value_usd=row.value_usd,
        weight_pct=row.weight_pct,
        shares_delta=row.shares_delta,
        shares_delta_pct=row.shares_delta_pct,
        weight_delta=row.weight_delta,
        action=row.action,
        first_period=row.first_period,
    )
