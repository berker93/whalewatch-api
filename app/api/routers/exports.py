"""CSV downloads of the tables a page shows: each a ``.csv`` beside its listing.

``GET /v1/investors/{slug}/portfolio.csv``
    One period's holdings: :func:`~app.api.routers.portfolio.read_portfolio`'s rows.
``GET /v1/investors/{slug}/activity.csv``
    What the investor did: :func:`~app.api.routers.portfolio.read_activity`'s.
``GET /v1/stocks/{ticker}/owners.csv``
    Who holds the stock: :func:`~app.api.routers.stocks.read_owners`'s.
``GET /v1/flows.csv``
    The screener's results: :func:`~app.api.routers.market.read_flows`'s.
``GET /v1/insiders/transactions.csv``
    The insider feed: :func:`~app.api.routers.insiders.list_insider_transactions`'s.

Each takes its listing's parameters but the page's, runs its listing's query
with no page, so every row in the listing's order, and streams it through
:mod:`app.api.csv_export`. The checks run first, as the listing runs them:
an unknown investor or stock is a 404 and a period before the plan's window a
402, before any of the file is sent.

Who may
-------
``export_enabled`` (Pro and the API plans): without it, a 402 ``plan_limit``
before anything is read. Each export is counted on its UTC day for the
account that took it (``user_activity_day.data_exports``), which the admin
console shows. Not cached: a file is one person's and taken once, and it is
streamed, so there is no answer to keep.

Provenance
----------
The last columns of every file (:mod:`app.api.csv_export`):

``as_of_period``
    The quarter the row is of, ``2026Q3``; for an insider trade, its trade day.
``filed_at``
    When the filing the row is read from reached EDGAR: the investor's latest
    for the period (holdings, activity, owners), the newest of every
    investor's for the period (the screener's aggregates), the Form 4's own.
``confidence``, ``observation_count``
    The implied price's (:mod:`app.api.estimates`): how far the holders' 13Fs
    agree on the stock's price at the period end, and how many there are. Empty
    for an insider trade, whose price is reported, not implied.
``is_split_adjusted``, ``split_ratio``
    Holdings, activity and owners only: the change is against the previous
    period's shares times the ratio, as a split since then would have made them.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Row, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import activity
from app.accounts.api_keys import KeyHolder
from app.accounts.sessions import Viewer, now
from app.api.csv_export import CSV_FILE, Column, csv_response, filename, stream_rows
from app.api.deps import SessionDep, SettingsDep
from app.api.entitlements import PLAN_LIMITED, EntitlementContext, EntitlementContextDep
from app.api.errors import INVALID, invalid, not_found
from app.api.meta import period_meta
from app.api.routers import insiders, market, portfolio, stocks
from app.api.routers.investors import SlugParam
from app.api.schemas.envelope import Coverage
from app.api.schemas.error import Problem
from app.api.schemas.insider import CODE_LABELS, Role
from app.api.schemas.stock import StockNotFound
from app.core.config import Settings
from app.core.periods import quarter_label
from app.db.models import Filer, Filing
from app.db.models.implied_price import ImpliedPrice
from app.db.models.position_change import ChangeAction, PositionChange
from app.db.queries.amendments import period_concerns
from app.db.queries.effective import EFFECTIVE_FILING
from app.derived.views import CONSENSUS_HOLDINGS

router = APIRouter(tags=["exports"])

SOURCE_13F: Final = (
    "Source: SEC EDGAR Form 13F-HR filings as each investor filed them. Compiled by WhaleWatch."
)
SOURCE_FORM4: Final = (
    "Source: SEC EDGAR Form 4 filings as each insider filed them. Compiled by WhaleWatch."
)
DISCLAIMER_13F: Final = (
    "Disclaimer: a 13F states what was held on the last day of a quarter and is filed up "
    "to 45 days after it. Positions may have changed since. Not investment advice."
)
DISCLAIMER_FORM4: Final = (
    "Disclaimer: a Form 4 is due two business days after the trade and some are filed "
    "late. Not investment advice."
)
UNITS: Final = (
    "Units: dollars for *_usd and prices. Percent for *_pct (12.5 is 12.5%). "
    "Empty is unknown or not applicable."
)

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)


# --- shared ---------------------------------------------------------------------


def _require_export(entitled: EntitlementContext) -> None:
    """402 unless the caller's plan has ``export_enabled``."""
    entitled.require_flag("export_enabled", message="CSV export is not on your plan.")


async def _count_export(session: AsyncSession, entitled: EntitlementContext) -> None:
    """Count the export for the account taking it, and commit, before the
    cursor opens its own transaction."""
    who = entitled.viewer
    if isinstance(who, Viewer | KeyHolder):
        await activity.count_export(session, who.user.id, now())
        await session.commit()


def _header(
    settings: Settings, title: str, path: str, *, form4: bool = False, more: list[str]
) -> list[str]:
    return [
        f"WhaleWatch export: {title}",
        SOURCE_FORM4 if form4 else SOURCE_13F,
        f"Generated: {_iso(now())}",
        DISCLAIMER_FORM4 if form4 else DISCLAIMER_13F,
        UNITS,
        f"Page: {settings.web_url}{path}",
        *more,
    ]


def _label(period: date | None) -> str | None:
    return None if period is None else quarter_label(period)


def _query(request: Request) -> str:
    """The request's query string, for the page's link: the web's pages take
    the listing's parameters by the same names."""
    return f"?{request.url.query}" if request.url.query else ""


#: The estimate's columns (:mod:`app.api.estimates`), as the listing's rows name them.
ESTIMATE_COLUMNS: Final = (
    Column("implied_price"),
    Column("estimated_return_pct"),
    Column(
        "estimated_return_as_of",
        lambda r: r.quote_date if r.estimated_return_pct is not None else None,
    ),
    Column("return_to_last_quarter_pct"),
    Column(
        "last_quarter_period",
        lambda r: (
            _label(r.last_quarter_period) if r.return_to_last_quarter_pct is not None else None
        ),
    ),
)


def _provenance(
    *,
    as_of: Callable[[Row[Any]], object],
    filed_at: Callable[[Row[Any]], object],
    price: bool = True,
    splits: bool = True,
) -> tuple[Column, ...]:
    """The provenance columns. ``as_of`` and ``filed_at`` read a row; ``price``
    is whether the row has the price estimate's confidence, and ``splits``
    whether it has the change's split. Without, the column is there and empty."""

    def nothing(row: Row[Any]) -> None:
        return None

    columns = [
        Column("as_of_period", as_of),
        Column("filed_at", filed_at),
        Column("confidence", (lambda r: r.price_confidence) if price else nothing),
        Column("observation_count", (lambda r: r.price_observation_count) if price else nothing),
    ]
    if splits:
        columns += [Column("is_split_adjusted", lambda r: r.is_split), Column("split_ratio")]
    return tuple(columns)


def _filed_at(filer_id: int | None = None, period: date | None = None) -> Any:
    """When each ``(filer, period)``'s latest effective filing reached EDGAR,
    grouped once rather than looked up per row: one filer's periods, or one
    period's filers."""
    effective = EFFECTIVE_FILING
    conditions = []
    if filer_id is not None:
        conditions.append(effective.c.filer_id == filer_id)
    if period is not None:
        conditions.append(effective.c.period_of_report == period)
    return (
        select(
            effective.c.filer_id,
            effective.c.period_of_report,
            func.max(Filing.filed_at).label("filed_at"),
        )
        .join(Filing, Filing.id == effective.c.filing_id)
        .where(*conditions)
        .group_by(effective.c.filer_id, effective.c.period_of_report)
        .cte("filed")
    )


# --- the investor ---------------------------------------------------------------


@router.get(
    "/investors/{slug}/portfolio.csv",
    operation_id="exportInvestorPortfolio",
    response_class=StreamingResponse,
    summary="One investor's positions in one period, as a CSV file",
    responses={
        **CSV_FILE,
        **not_found("No investor with that slug, or nothing published for the period."),
        **PLAN_LIMITED,
        **INVALID,
    },
)
async def export_portfolio(
    slug: SlugParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    period: portfolio.PeriodParam = None,
    include_options: portfolio.IncludeOptions = False,
    sort: portfolio.SortParam = portfolio.PortfolioSort.WEIGHT,
    order: portfolio.OrderParam = portfolio.SortOrder.DESC,
) -> StreamingResponse:
    """Every position `GET /v1/investors/{slug}/portfolio` pages through, with
    the same parameters, in the same order, as
    `berkshire-hathaway_2026Q3_holdings.csv`. `filed_at` is the investor's
    latest filing for the period; the `#` lines name what `meta.caveats` would.
    Needs `export_enabled`."""
    _require_export(entitled)
    entitled.require_period(period)
    filer_id, period = await portfolio._portfolio_period(session, slug, period, entitled)
    meta = await period_meta(session, period, filer_id=filer_id)
    caveats = await period_concerns(session, slug=slug, period=period)
    name = await session.scalar(select(_DISPLAY_NAME).where(Filer.id == filer_id))
    statement, _ = portfolio.portfolio_query(
        filer_id,
        period,
        None,
        sort=sort,
        order=order,
        include_options=include_options,
        history_from=entitled.history_from,
    )
    await _count_export(session, entitled)

    label = quarter_label(period)
    columns = (
        Column("cusip"),
        Column("ticker"),
        Column("current_ticker"),
        Column("issuer_name"),
        Column("delisted_by"),
        Column("put_call"),
        Column("shares"),
        Column("value_usd"),
        Column("weight_pct"),
        Column("action"),
        Column("shares_delta"),
        Column("shares_delta_pct"),
        Column("weight_delta"),
        Column("first_period"),
        *ESTIMATE_COLUMNS,
        *_provenance(as_of=lambda r: label, filed_at=lambda r: meta.latest_filing_at),
    )
    return csv_response(
        filename(slug, label, "holdings"),
        _header(
            settings,
            f"{name} holdings in {label} (the quarter ended {period})",
            f"/investors/{quote(slug, safe='')}?{urlencode({'period': label})}",
            more=[f"Caveat: {caveat}" for caveat in caveats],
        ),
        await stream_rows(session, statement),
        columns,
    )


@router.get(
    "/investors/{slug}/activity.csv",
    operation_id="exportInvestorActivity",
    response_class=StreamingResponse,
    summary="What one investor opened, added to, trimmed and exited, as a CSV file",
    responses={**CSV_FILE, **not_found("No investor with that slug."), **PLAN_LIMITED, **INVALID},
)
async def export_activity(
    slug: SlugParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    start: portfolio.RangeStart = None,
    end: portfolio.RangeEnd = None,
    action: portfolio.ActionsParam = None,
) -> StreamingResponse:
    """Every change `GET /v1/investors/{slug}/activity` pages through, with the
    same parameters, in the same order, as
    `berkshire-hathaway_2025Q4-2026Q3_activity.csv`, the quarters the rows span.
    `filed_at` is the investor's latest filing for the row's period. Needs
    `export_enabled`."""
    _require_export(entitled)
    if start is not None and end is not None and start > end:
        raise invalid("from", f"from ({quarter_label(start)}) is after to ({quarter_label(end)}).")
    entitled.require_period(start)
    entitled.require_period(end)
    filer_id = await portfolio._filer_id(session, slug)
    actions = action or portfolio.DEFAULT_ACTIONS
    listed = portfolio.activity_query(
        filer_id,
        None,
        start=start,
        end=end,
        actions=actions,
        history_from=entitled.history_from,
    ).subquery("activity")
    filed = _filed_at(filer_id=filer_id)
    statement = (
        select(listed, filed.c.filed_at)
        .select_from(listed)
        .outerjoin(filed, filed.c.period_of_report == listed.c.period_of_report)
        .order_by(*portfolio.ACTIVITY_KEYSET.order_by(on=listed))
    )
    first, last = (
        await session.execute(_activity_span(filer_id, actions, start, end, entitled))
    ).one()
    name = await session.scalar(select(_DISPLAY_NAME).where(Filer.id == filer_id))
    await _count_export(session, entitled)

    span = (
        "no quarters"
        if first is None
        else quarter_label(first)
        if first == last
        else f"{quarter_label(first)}-{quarter_label(last)}"
    )
    columns = (
        Column("cusip"),
        Column("ticker"),
        Column("current_ticker"),
        Column("issuer_name"),
        Column("delisted_by"),
        Column("period", lambda r: r.period_of_report),
        Column("prev_period", lambda r: r.prev_period_of_report),
        Column("action"),
        Column("shares"),
        Column("shares_delta"),
        Column("shares_delta_pct"),
        Column("value_usd"),
        Column("traded_value_usd"),
        Column("weight_pct"),
        Column("weight_delta"),
        *ESTIMATE_COLUMNS,
        *_provenance(
            as_of=lambda r: quarter_label(r.period_of_report), filed_at=lambda r: r.filed_at
        ),
    )
    return csv_response(
        filename(slug, span if first is not None else "none", "activity"),
        _header(
            settings,
            f"{name} activity in {span}",
            f"/investors/{quote(slug, safe='')}",
            more=[f"Actions: {'; '.join(sorted(a.value for a in actions))}"],
        ),
        await stream_rows(session, statement),
        columns,
    )


def _activity_span(
    filer_id: int,
    actions: frozenset[ChangeAction],
    start: date | None,
    end: date | None,
    entitled: EntitlementContext,
) -> Select[Any]:
    """The first and last period of the activity's rows, for the file's name:
    :func:`~app.api.routers.portfolio.activity_query`'s conditions on
    ``position_change`` alone, without the names and the estimates."""
    change = PositionChange
    conditions = [
        change.filer_id == filer_id,
        change.action.in_(sorted(a.value for a in actions)),
        change.prev_period_of_report.is_not(None),
    ]
    for first in (entitled.history_from, start):
        if first is not None:
            conditions.append(change.period_of_report >= first)
    if end is not None:
        conditions.append(change.period_of_report <= end)
    return select(func.min(change.period_of_report), func.max(change.period_of_report)).where(
        *conditions
    )


# --- the stock ------------------------------------------------------------------


@router.get(
    "/stocks/{ticker}/owners.csv",
    operation_id="exportStockOwners",
    response_class=StreamingResponse,
    summary="Every tracked investor holding one stock in one period, as a CSV file",
    responses={
        **CSV_FILE,
        **not_found(
            "Nothing by that ticker, alias or CUSIP, with suggestions; or nothing "
            "published for the period, without.",
            StockNotFound | Problem,
        ),
        **PLAN_LIMITED,
        **INVALID,
    },
)
async def export_owners(
    ticker: stocks.TickerParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    period: stocks.PeriodParam = None,
) -> StreamingResponse:
    """Every holder `GET /v1/stocks/{ticker}/owners` pages through, in the same
    order, as `aapl_2026Q3_holders.csv`. `filed_at` is each holder's latest
    filing for the period. Needs `export_enabled`."""
    _require_export(entitled)
    entitled.require_period(period)
    security = await stocks._resolve(session, ticker, on=period)
    period = await stocks._owners_period(session, period, entitled)
    listed = stocks.owners_query(security.id, period, None).subquery("owners")
    filed = _filed_at(period=period)
    statement = (
        select(listed, filed.c.filed_at)
        .select_from(listed)
        .outerjoin(filed, filed.c.filer_id == listed.c.filer_id)
        .order_by(*stocks.OWNERS_KEYSET.order_by(on=listed))
    )
    await _count_export(session, entitled)

    label = quarter_label(period)
    shown = security.ticker or security.cusip
    columns = (
        Column("slug"),
        Column("display_name"),
        Column("shares"),
        Column("value_usd"),
        Column("weight_pct"),
        Column("action"),
        Column("shares_delta"),
        Column("shares_delta_pct"),
        Column("weight_delta"),
        *ESTIMATE_COLUMNS,
        *_provenance(as_of=lambda r: label, filed_at=lambda r: r.filed_at),
    )
    return csv_response(
        filename(shown, label, "holders"),
        _header(
            settings,
            f"holders of {security.name} ({shown}; CUSIP {security.cusip}) in {label} "
            f"(the quarter ended {period})",
            f"/stocks/{quote(shown, safe='')}?{urlencode({'period': label})}",
            more=[],
        ),
        await stream_rows(session, statement),
        columns,
    )


# --- the screener ---------------------------------------------------------------


@router.get(
    "/flows.csv",
    operation_id="exportFlows",
    response_class=StreamingResponse,
    summary="The screener's stocks, as a CSV file",
    responses={
        **CSV_FILE,
        **not_found("Nothing published for the period."),
        **PLAN_LIMITED,
        **INVALID,
    },
)
async def export_flows(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    period: market.PeriodParam = None,
    period_type: market.PeriodTypeParam = market.PeriodType.QUARTER,
    sector: market.SectorParam = None,
    min_investors: market.MinInvestorsParam = None,
    min_value: market.MinValueParam = None,
    direction: market.DirectionParam = None,
    sort: market.FlowSortParam = market.FlowSort.NET_VALUE,
    order: market.FlowOrderParam = portfolio.SortOrder.DESC,
) -> StreamingResponse:
    """Every stock `GET /v1/flows` pages through, with the same filters, in the
    same order, as `screener_2026Q3_flows.csv` (`screener_2025Q4-2026Q3_flows.csv`
    for a year). `filed_at` is the newest filing the period's figures include,
    and the `#` lines say when the aggregates were refreshed. Needs
    `export_enabled`."""
    _require_export(entitled)
    market.refuse_sector(sector)
    flows = market.FLOWS[period_type]
    period = await market._period(
        session,
        flows,
        period,
        what="flows",
        entitled=entitled,
        quarters=market._quarters(period_type),
    )
    meta = await market._meta(session, period, flows, CONSENSUS_HOLDINGS, period_type=period_type)
    screened, keyset = market.flows_query(
        flows,
        period,
        None,
        sort=sort,
        order=order,
        direction=direction,
        min_investors=min_investors,
        min_value=min_value,
    )
    listed = screened.subquery("flows")
    price = ImpliedPrice
    statement = (
        select(
            listed,
            price.confidence.label("price_confidence"),
            price.observation_count.label("price_observation_count"),
        )
        .select_from(listed)
        .outerjoin(
            price,
            and_(price.security_id == listed.c.security_id, price.period_of_report == period),
        )
        .order_by(*keyset.order_by(on=listed))
    )
    await _count_export(session, entitled)

    label = quarter_label(period)
    span = label if meta.quarters is None else f"{meta.quarters[0]}-{meta.quarters[-1]}"
    refreshed = meta.refreshed_at
    columns = (
        Column("cusip"),
        Column("ticker"),
        Column("current_ticker"),
        Column("issuer_name"),
        Column("delisted_by"),
        Column("gross_bought_usd", lambda r: r.bought_value_usd),
        Column("gross_sold_usd", lambda r: r.sold_value_usd),
        Column("net_value_usd"),
        Column("net_shares"),
        Column("buyer_count"),
        Column("seller_count"),
        Column("new_positions"),
        Column("exits"),
        Column("holder_count"),
        Column("total_value_usd"),
        *_provenance(as_of=lambda r: label, filed_at=lambda r: meta.latest_filing_at, splits=False),
    )
    coverage = meta.coverage
    return csv_response(
        filename("screener", span, "flows"),
        _header(
            settings,
            f"screener flows in {span}",
            f"/flows{_query(request)}",
            more=[
                *([] if coverage is None else [_coverage(coverage, label)]),
                f"Aggregates as of: {_iso(refreshed) if refreshed else 'unrecorded'}",
            ],
        ),
        await stream_rows(session, statement),
        columns,
    )


def _coverage(coverage: Coverage, label: str) -> str:
    return (
        f"Coverage: {coverage.filers_reported} of {coverage.filers_tracked} "
        f"investors have filed for {label}."
    )


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


# --- insiders -------------------------------------------------------------------


def _roles(row: Row[Any]) -> str:
    """The roles the insider checked, as the feed's ``roles``, ``;`` between them."""
    return ";".join(
        role.value
        for role, checked in (
            (Role.DIRECTOR, row.is_director),
            (Role.OFFICER, row.is_officer),
            (Role.TEN_PERCENT_OWNER, row.is_ten_percent_owner),
            (Role.OTHER, row.is_other),
        )
        if checked
    )


INSIDER_COLUMNS: Final = (
    Column("accession_no"),
    Column("line_number"),
    Column("transaction_date"),
    Column("issuer_cik"),
    Column("issuer_name"),
    Column("issuer_ticker"),
    Column("cusip"),
    Column("ticker"),
    Column("current_ticker"),
    Column("delisted_by"),
    Column("insider_cik"),
    Column("insider_name"),
    Column("roles", _roles),
    Column("officer_title"),
    Column("other_role", lambda r: r.other_text),
    Column("code", lambda r: r.transaction_code),
    Column("code_label", lambda r: CODE_LABELS.get(r.transaction_code)),
    Column("acquired_disposed"),
    Column("is_derivative"),
    Column("security_title"),
    Column("shares"),
    Column("price_per_share"),
    Column("value_usd"),
    Column("shares_owned_after"),
    Column("is_10b5_1"),
    Column("footnotes"),
    *_provenance(
        as_of=lambda r: r.transaction_date, filed_at=lambda r: r.filed_at, price=False, splits=False
    ),
)


@router.get(
    "/insiders/transactions.csv",
    operation_id="exportInsiderTransactions",
    response_class=StreamingResponse,
    summary="Insiders' Form 4 trades, as a CSV file",
    responses={
        **CSV_FILE,
        **not_found(
            "Nothing by that ticker, alias or CUSIP. Suggests what may have been meant.",
            StockNotFound,
        ),
        **PLAN_LIMITED,
        **INVALID,
    },
)
async def export_insider_transactions(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    ticker: insiders.FeedTickerParam = None,
    code: insiders.CodesParam = None,
    role: insiders.RolesParam = None,
    min_value: insiders.MinValueParam = None,
    start: insiders.StartParam = None,
    end: insiders.EndParam = None,
    exclude_10b5_1: insiders.Exclude10b51Param = False,
    insider_cik: insiders.InsiderCiksParam = None,
    filed_from: insiders.FiledFromParam = None,
    filed_to: insiders.FiledToParam = None,
    sort: insiders.SortParam = insiders.InsiderSort.DATE,
    order: insiders.OrderParam = portfolio.SortOrder.DESC,
) -> StreamingResponse:
    """Every line `GET /v1/insiders/transactions` pages through, with the same
    filters, in the same order: open-market purchases and sales unless `code`
    says otherwise, which the `#` lines state. As
    `insiders_2026-05-01-to-2026-11-01_trades.csv`, or `nvda_...` for one stock.
    `as_of_period` is the trade day and `filed_at` the Form 4's; a reported
    price has no `confidence`. Needs `export_enabled`."""
    _require_export(entitled)
    filters = insiders._filters(
        code,
        role,
        min_value,
        start,
        end,
        exclude_10b5_1,
        entitled=entitled,
        insider_ciks=insider_cik,
        filed_start=filed_from,
        filed_end=filed_to,
    )
    security = await stocks._resolve(session, ticker) if ticker is not None else None
    statement, _ = insiders.transactions_query(
        None,
        filters,
        security_id=None if security is None else security.id,
        sort=sort,
        order=order,
    )
    await _count_export(session, entitled)

    first = filters.start or filters.trades_from
    last = filters.end or entitled.today
    span = f"{first}-to-{last}" if first is not None else f"to-{last}"
    subject = "insiders" if security is None else (security.ticker or security.cusip)
    codes = (
        "every code"
        if filters.codes is None
        else "; ".join(f"{c} ({CODE_LABELS.get(c, 'unlabelled')})" for c in sorted(filters.codes))
    )
    return csv_response(
        filename(subject, span, "trades"),
        _header(
            settings,
            f"insider trades{'' if security is None else f' in {security.name}'} "
            f"from {first or 'the first loaded'} to {last}",
            f"/insiders{_query(request)}",
            form4=True,
            more=[f"Codes: {codes}{' (the default)' if code is None else ''}"],
        ),
        await stream_rows(session, statement),
        INSIDER_COLUMNS,
    )
