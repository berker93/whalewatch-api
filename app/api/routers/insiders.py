"""Insider (Form 4) endpoints: what officers, directors and 10% owners traded.

``GET /v1/insiders/transactions``
    Every insider's Form 4 lines, newest trade first, filtered and
    keyset-paged. Read live from ``insider_transaction``.
``GET /v1/stocks/{ticker}/insiders``
    The same, for one stock: the stock page's table.
``GET /v1/insiders/clusters``
    Episodes of cluster buying: several of one company's officers and
    directors buying on the open market within a window. Read from
    ``insider_cluster``, which ``detect-clusters`` rebuilds after every Form 4
    load; see :mod:`app.derived.insider_cluster` for what counts.

The default filter
------------------
Open-market purchases and sales only, codes ``P`` and ``S``. Most Form 4
lines are grants, option exercises and shares withheld for tax, and a feed
that shows them beside the trades makes "insider selling" of a vesting
schedule. That is an editorial decision, so it is stated rather than hidden:
``meta.codes`` names the codes the rows are limited to, with what each means,
and ``meta.codes_default`` says the client did not choose them. ``?code=all``
lifts it.

The default is written into the SQL as literals, not bound: the feed's index
is partial on exactly ``transaction_code IN ('P', 'S')``, and Postgres uses a
partial index only for a query it can prove is inside it when it plans. A
generic plan of a prepared statement, with the codes as a parameter, could not.

A stock's lines
---------------
``?ticker=`` and the stock page are the lines linked to the security, and the
issuer's lines linked to no security at all. The loader leaves a line unlinked
when the issuer's CIK names several securities and the filing's symbol does
not choose one (Lennar's filers write ``LEN, LEN.B``). The issuer is certain
there and the class is not, so such a line is under each of the issuer's
stocks, with ``cusip`` null to say so. The issuer is the CIK SEC lists the
stock's current ticker under.

The plan's window
-----------------
Trades are inside the caller's ``insiders_from`` (:mod:`app.api.entitlements`),
months back from today, in SQL: no line traded before it, and no cluster begun
before it, so an episode's totals never include a purchase outside it. A
``from``, ``to``, ``filed_from`` or ``filed_to`` before it is a 402.

Sorting
-------
By ``transaction_date`` (the default) or ``value_usd``, then the newest
trade, then the row's id, so the order is total. A line with no value, a grant
or a gift, sorts by value as below zero: last, largest first. ``?min_value=``
leaves those lines out altogether, because a line with no value is not worth
at least anything.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from pydantic import PlainValidator, WithJsonSchema
from sqlalchemy import (
    BindParameter,
    ColumnElement,
    Select,
    and_,
    false,
    func,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import Lifetime, cached
from app.api.deps import PageParamsDep, SessionDep
from app.api.entitlements import PLAN_LIMITED, EntitlementContext, EntitlementContextDep
from app.api.errors import INVALID, INVALID_CURSOR, invalid, not_found
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.routers.portfolio import SortOrder
from app.api.routers.stocks import TickerParam, _resolve
from app.api.schemas.insider import (
    CODE_LABELS,
    ClusterInsider,
    InsiderClusterEnvelope,
    InsiderClusterMeta,
    InsiderFeedMeta,
    InsiderTransaction,
    InsiderTransactionEnvelope,
    Role,
    TransactionCode,
)
from app.api.schemas.insider import InsiderCluster as ClusterRow
from app.api.schemas.stock import StockNotFound
from app.db.models import (
    OPEN_MARKET_CODES,
    Filing,
    Insider,
    InsiderCluster,
    ReportingOwner,
    SecCompanyTicker,
    Security,
)
from app.db.models import InsiderTransaction as Line
from app.db.queries.tickers import ticker_labels
from app.derived.insider_cluster import (
    DEFAULT_MIN_INSIDERS,
    DEFAULT_WINDOW,
    MIN_INSIDERS,
    RULES,
    WINDOWS,
)

router = APIRouter(tags=["insiders"])

SecurityId = int | BindParameter[int]

#: What ``?code=`` means when it is left out.
DEFAULT_CODES: Final = frozenset(OPEN_MARKET_CODES)
#: ``?code=all``: every code, the default lifted.
ALL_CODES: Final = "all"

# Sort keys must be NOT NULL, and a grant has no value. -1 is below every
# real one, so those lines come last, largest first.
_VALUE_KEY: Final = func.coalesce(Line.value_usd, -1)


class InsiderSort(StrEnum):
    """What the feed is ordered by."""

    DATE = "date"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class Filters:
    """What a client can narrow the feed by, parsed. The defaults are the feed's."""

    codes: frozenset[str] | None = DEFAULT_CODES
    """None for every code."""
    roles: frozenset[Role] = frozenset()
    """Any of them; empty for any insider at all."""
    min_value: Decimal | None = None
    start: date | None = None
    end: date | None = None
    exclude_10b5_1: bool = False
    insider_ciks: frozenset[str] = frozenset()
    """Any of these insiders; empty for anyone."""
    filed_start: date | None = None
    """Filed on or after this day, in UTC."""
    filed_end: date | None = None
    """Filed on or before this day, in UTC."""
    trades_from: date | None = None
    """The caller's window: no trade before this day. Not the client's to
    choose, so not a parameter: :func:`_filters` sets it."""


def _keyset(listing: str, sort: InsiderSort, order: SortOrder) -> Keyset:
    """The sort, then the newest trade, then the line. Every key runs the same
    way, so a page boundary is one row comparison, which the indexes on
    ``(transaction_date DESC, id DESC)`` answer by seeking."""
    descending = order is SortOrder.DESC
    lead = (
        [SortKey("value_key", _VALUE_KEY, Decimal, descending)] if sort is InsiderSort.VALUE else []
    )
    return Keyset(
        f"{listing}.{sort.value}.{order.value}",
        (
            *lead,
            SortKey("transaction_date", Line.transaction_date.expression, date, descending),
            SortKey("id", Line.id.expression, int, descending),
        ),
    )


def _codes_condition(codes: frozenset[str]) -> ColumnElement[bool]:
    """``transaction_code IN (...)``, with the codes as literals when they are
    the default, so the open-market index's predicate is provably implied."""
    if codes == DEFAULT_CODES:
        return Line.transaction_code.in_(
            [literal_column(f"'{code}'") for code in sorted(OPEN_MARKET_CODES)]
        )
    return Line.transaction_code.in_(sorted(codes))


def _issuer_ciks(security_id: SecurityId) -> Select[Any]:
    """The CIK SEC lists the stock's current ticker under, as a subquery."""
    sec_spelling = (
        select(func.replace(Security.ticker, ".", "-"))
        .where(Security.id == security_id)
        .scalar_subquery()
    )
    return select(SecCompanyTicker.cik).where(SecCompanyTicker.ticker == sec_spelling)


def _of_stock(security_id: SecurityId) -> ColumnElement[bool]:
    """The stock's lines, and its issuer's lines linked to no security."""
    return or_(
        Line.security_id == security_id,
        and_(Line.security_id.is_(None), Line.issuer_cik.in_(_issuer_ciks(security_id))),
    )


#: The feed with nothing asked for.
DEFAULT_FILTERS: Final = Filters()


def transactions_query(
    page: PageParams,
    filters: Filters = DEFAULT_FILTERS,
    *,
    security_id: SecurityId | None = None,
    sort: InsiderSort = InsiderSort.DATE,
    order: SortOrder = SortOrder.DESC,
    listing: str = "insiders",
) -> tuple[Select[Any], Keyset]:
    """One page of Form 4 lines passing ``filters``, named, and its keyset.

    The filters run on ``insider_transaction`` joined to the line's owner row
    (a primary-key probe each), and the filing, the insider and the security
    are joined to the page alone.

    :param security_id: One stock's lines (:func:`_of_stock`), or everyone's.
    :param listing: Names the keyset, so a cursor is only taken back by the
        listing that made it.
    """
    keyset = _keyset(listing, sort, order)
    owner = ReportingOwner
    conditions: list[ColumnElement[bool]] = []
    if filters.codes is not None:
        conditions.append(_codes_condition(filters.codes))
    if security_id is not None:
        conditions.append(_of_stock(security_id))
    if filters.roles:
        flags = {
            Role.DIRECTOR: owner.is_director,
            Role.OFFICER: owner.is_officer,
            Role.TEN_PERCENT_OWNER: owner.is_ten_percent_owner,
            Role.OTHER: owner.is_other,
        }
        conditions.append(or_(*(flags[role] for role in sorted(filters.roles))))
    if filters.min_value is not None:
        conditions.append(Line.value_usd >= filters.min_value)
    if filters.trades_from is not None:
        conditions.append(Line.transaction_date >= filters.trades_from)
    if filters.start is not None:
        conditions.append(Line.transaction_date >= filters.start)
    if filters.end is not None:
        conditions.append(Line.transaction_date <= filters.end)
    if filters.exclude_10b5_1:
        conditions.append(Line.is_10b5_1.is_(false()))
    if filters.insider_ciks:
        conditions.append(
            Line.insider_id.in_(
                select(Insider.id).where(Insider.cik.in_(sorted(filters.insider_ciks)))
            )
        )
    if filters.filed_start is not None or filters.filed_end is not None:
        filed = []
        if filters.filed_start is not None:
            filed.append(Filing.filed_at >= _utc_midnight(filters.filed_start))
        if filters.filed_end is not None:
            filed.append(Filing.filed_at < _utc_midnight(filters.filed_end + timedelta(days=1)))
            # Nothing is traded after it is filed: the later partitions are skipped.
            conditions.append(Line.transaction_date <= filters.filed_end + timedelta(days=1))
        conditions.append(Line.filing_id.in_(select(Filing.id).where(*filed)))

    rows = (
        select(
            Line.id,
            Line.transaction_date,
            Line.filing_id,
            Line.insider_id,
            Line.line_number,
            Line.issuer_cik,
            Line.issuer_name,
            Line.issuer_ticker,
            Line.security_id,
            Line.security_title,
            Line.is_derivative,
            Line.transaction_code,
            Line.acquired_disposed,
            Line.shares,
            Line.price_per_share,
            Line.value_usd,
            Line.shares_owned_after,
            Line.is_10b5_1,
            Line.footnotes,
            owner.is_director,
            owner.is_officer,
            owner.is_ten_percent_owner,
            owner.is_other,
            owner.officer_title,
            owner.other_text,
            _VALUE_KEY.label("value_key"),
        )
        .select_from(Line)
        .join(
            owner,
            and_(owner.filing_id == Line.filing_id, owner.insider_id == Line.insider_id),
        )
        .where(*conditions)
    )
    page_rows = page_statement(rows, keyset, page).cte("page")
    statement = (
        select(
            page_rows,
            Filing.accession_no,
            Filing.filed_at,
            Insider.cik.label("insider_cik"),
            Insider.name.label("insider_name"),
            Security.cusip,
            *ticker_labels(page_rows.c.transaction_date),
        )
        .select_from(page_rows)
        .join(Filing, Filing.id == page_rows.c.filing_id)
        .join(Insider, Insider.id == page_rows.c.insider_id)
        .outerjoin(Security, Security.id == page_rows.c.security_id)
        .order_by(*keyset.order_by(on=page_rows))
    )
    return statement, keyset


def _utc_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def _transaction(row: Any) -> InsiderTransaction:
    roles = [
        role
        for role, checked in (
            (Role.DIRECTOR, row.is_director),
            (Role.OFFICER, row.is_officer),
            (Role.TEN_PERCENT_OWNER, row.is_ten_percent_owner),
            (Role.OTHER, row.is_other),
        )
        if checked
    ]
    return InsiderTransaction(
        accession_no=row.accession_no,
        line_number=row.line_number,
        transaction_date=row.transaction_date,
        filed_at=row.filed_at,
        issuer_cik=row.issuer_cik,
        issuer_name=row.issuer_name,
        issuer_ticker=row.issuer_ticker,
        cusip=row.cusip,
        ticker=row.ticker,
        current_ticker=row.current_ticker,
        delisted_by=row.delisted_by,
        insider_cik=row.insider_cik,
        insider_name=row.insider_name,
        roles=roles,
        officer_title=row.officer_title,
        other_role=row.other_text,
        code=row.transaction_code,
        code_label=CODE_LABELS.get(row.transaction_code),
        acquired_disposed=row.acquired_disposed,
        is_derivative=row.is_derivative,
        security_title=row.security_title,
        shares=row.shares,
        price_per_share=row.price_per_share,
        value_usd=row.value_usd,
        shares_owned_after=row.shares_owned_after,
        is_10b5_1=row.is_10b5_1,
        footnotes=row.footnotes,
    )


def _meta(filters: Filters, *, default: bool) -> InsiderFeedMeta:
    codes = filters.codes
    return InsiderFeedMeta(
        codes=None
        if codes is None
        else [TransactionCode(code=code, label=CODE_LABELS.get(code)) for code in sorted(codes)],
        codes_default=default,
        insider_ciks=sorted(filters.insider_ciks) or None,
        filed_from=filters.filed_start,
        filed_to=filters.filed_end,
        generated_at=datetime.now(UTC),
    )


# --- parameters -----------------------------------------------------------------


def _names(value: object) -> list[str]:
    """A repeatable, comma-separated query parameter, as FastAPI hands it over."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("a comma-separated list")
    return [name.strip() for item in value for name in item.split(",") if name.strip()]


def _codes(value: object) -> frozenset[str]:
    """``?code=P,S``, ``?code=p&code=s``, or ``?code=all``. Any one letter is
    a code: the set is the SEC's, and an unlisted one is served unlabelled."""
    if isinstance(value, frozenset):  # a default, never a query string
        return value
    names = [name.upper() for name in _names(value)]
    if ALL_CODES.upper() in names:
        if len(names) > 1:
            raise ValueError(f"`{ALL_CODES}` is every code: give it alone")
        return frozenset({ALL_CODES})
    if bad := [name for name in names if len(name) != 1 or not ("A" <= name <= "Z")]:
        raise ValueError(
            f"not a transaction code: {', '.join(map(repr, bad))}; codes are one letter"
        )
    if not names:
        raise ValueError("no code given")
    return frozenset(names)


def _ciks(value: object) -> frozenset[str]:
    """``?insider_cik=1197647,0001045810``, or repeated: CIKs, zero-padded to ten."""
    if isinstance(value, frozenset):
        return value
    names = _names(value)
    if bad := [name for name in names if not name.isdigit() or len(name) > 10]:
        raise ValueError(f"not a CIK: {', '.join(map(repr, bad))}; a CIK is up to ten digits")
    if not names:
        raise ValueError("no CIK given")
    return frozenset(name.zfill(10) for name in names)


def _roles(value: object) -> frozenset[Role]:
    """``?role=director,officer``, or repeated."""
    if isinstance(value, frozenset):
        return value
    names = _names(value)
    known = {role.value for role in Role}
    if unknown := [name for name in names if name not in known]:
        raise ValueError(
            f"unknown role {', '.join(map(repr, unknown))}: "
            f"choose from {', '.join(role.value for role in Role)}"
        )
    return frozenset(Role(name) for name in names)


CodesParam = Annotated[
    frozenset[str] | None,
    PlainValidator(_codes),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["P,S"]]}),
    Query(
        description=(
            "Transaction codes, comma-separated or repeated: `P` open-market purchase, "
            "`S` open-market sale, `A` grant, `M` option exercise, `F` tax "
            "withholding, `G` gift, `C` conversion, and the SEC's others. `all` for "
            "every code. **Defaults to `P,S`**, which `meta.codes` and "
            "`meta.codes_default` state."
        )
    ),
]
RolesParam = Annotated[
    frozenset[Role] | None,
    PlainValidator(_roles),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["director"]]}),
    Query(
        description=(
            "Only insiders who checked any of these on the filing: `director`, "
            "`officer`, `ten_percent_owner`, `other`. Comma-separated or repeated."
        )
    ),
]
MinValueParam = Annotated[
    Decimal | None,
    Query(
        ge=0,
        description=(
            "Only lines worth at least this many dollars. A line with no value (a "
            "grant, a gift, a price given only as a range) is left out."
        ),
    ),
]
StartParam = Annotated[
    date | None, Query(alias="from", description="The first trade day to include, `2026-06-01`.")
]
EndParam = Annotated[
    date | None, Query(alias="to", description="The last trade day to include, `2026-06-30`.")
]
Exclude10b51Param = Annotated[
    bool,
    Query(
        description=(
            "Leave out trades made under a Rule 10b5-1 plan, which was set up months "
            "before and says nothing about what the insider thinks now."
        )
    ),
]
InsiderCiksParam = Annotated[
    frozenset[str] | None,
    PlainValidator(_ciks),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["0001197647"]]}),
    Query(
        description=(
            "Only these insiders, by CIK, comma-separated or repeated, with or without "
            "the leading zeros. Combine with `code=all` for everything they filed. A "
            "joint filing's lines are under its first owner's CIK."
        )
    ),
]
FiledFromParam = Annotated[
    date | None,
    Query(
        description=(
            "The first day, in UTC, the filing reached EDGAR, `2026-06-01`. Unlike "
            "`from`, which is the trade day: a late filing for an old trade is in."
        )
    ),
]
FiledToParam = Annotated[
    date | None,
    Query(description="The last day, in UTC, the filing reached EDGAR, `2026-06-30`."),
]
SortParam = Annotated[
    InsiderSort,
    Query(description="`date`, the trade day (the default), or `value`, in dollars."),
]
OrderParam = Annotated[SortOrder, Query(description="Newest or largest first by default.")]
FeedTickerParam = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=32,
        description=(
            "One stock: a ticker, a former ticker, or a CUSIP, as `/v1/stocks/{ticker}` "
            "reads it. Its issuer's lines no security could be linked to are included."
        ),
    ),
]

_NOT_FOUND = not_found(
    "Nothing by that ticker, alias or CUSIP. Suggests what may have been meant.",
    StockNotFound,
)


def _filters(
    codes: frozenset[str] | None,
    roles: frozenset[Role] | None,
    min_value: Decimal | None,
    start: date | None,
    end: date | None,
    exclude_10b5_1: bool,
    *,
    entitled: EntitlementContext,
    insider_ciks: frozenset[str] | None = None,
    filed_start: date | None = None,
    filed_end: date | None = None,
) -> Filters:
    if start is not None and end is not None and start > end:
        raise invalid("from", f"from ({start}) is after to ({end}).")
    if filed_start is not None and filed_end is not None and filed_start > filed_end:
        raise invalid("filed_from", f"filed_from ({filed_start}) is after filed_to ({filed_end}).")
    for day in (start, end, filed_start, filed_end):
        entitled.require_day(day)
    if codes is None:
        chosen: frozenset[str] | None = DEFAULT_CODES
    elif ALL_CODES in codes:
        chosen = None
    else:
        chosen = codes
    return Filters(
        codes=chosen,
        roles=roles or frozenset(),
        min_value=min_value,
        start=start,
        end=end,
        exclude_10b5_1=exclude_10b5_1,
        insider_ciks=insider_ciks or frozenset(),
        filed_start=filed_start,
        filed_end=filed_end,
        trades_from=entitled.insiders_from,
    )


async def _envelope(
    session: AsyncSession,
    page: PageParams,
    filters: Filters,
    *,
    defaulted: bool,
    security_id: int | None,
    sort: InsiderSort,
    order: SortOrder,
    listing: str,
) -> InsiderTransactionEnvelope:
    statement, keyset = transactions_query(
        page, filters, security_id=security_id, sort=sort, order=order, listing=listing
    )
    rows, page_info = page_of(list(await session.execute(statement)), keyset, page)
    return InsiderTransactionEnvelope(
        data=[_transaction(row) for row in rows],
        meta=_meta(filters, default=defaulted),
        page=page_info,
    )


# --- the routes -----------------------------------------------------------------


@router.get(
    "/insiders/transactions",
    operation_id="listInsiderTransactions",
    response_model=InsiderTransactionEnvelope,
    summary="Insiders' Form 4 trades, open-market purchases and sales by default",
    responses={**_NOT_FOUND, **PLAN_LIMITED, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.INSIDERS)
async def list_insider_transactions(
    session: SessionDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    ticker: FeedTickerParam = None,
    code: CodesParam = None,
    role: RolesParam = None,
    min_value: MinValueParam = None,
    start: StartParam = None,
    end: EndParam = None,
    exclude_10b5_1: Exclude10b51Param = False,
    insider_cik: InsiderCiksParam = None,
    filed_from: FiledFromParam = None,
    filed_to: FiledToParam = None,
    sort: SortParam = InsiderSort.DATE,
    order: OrderParam = SortOrder.DESC,
) -> InsiderTransactionEnvelope:
    """Form 4 lines, newest trade first, one row per line as filed. Only
    open-market purchases and sales unless `code` says otherwise: grants,
    exercises and tax withholding are most lines and carry no view. `meta`
    echoes the codes, insiders and filing window the rows were limited to."""
    filters = _filters(
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
    security_id = (await _resolve(session, ticker)).id if ticker is not None else None
    return await _envelope(
        session,
        page,
        filters,
        defaulted=code is None,
        security_id=security_id,
        sort=sort,
        order=order,
        listing="insiders" if security_id is None else "insiders.stock",
    )


@router.get(
    "/stocks/{ticker}/insiders",
    operation_id="getStockInsiders",
    response_model=InsiderTransactionEnvelope,
    summary="One stock's insider trades, open-market purchases and sales by default",
    responses={**_NOT_FOUND, **PLAN_LIMITED, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.INSIDERS)
async def read_stock_insiders(
    ticker: TickerParam,
    session: SessionDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    code: CodesParam = None,
    role: RolesParam = None,
    min_value: MinValueParam = None,
    start: StartParam = None,
    end: EndParam = None,
    exclude_10b5_1: Exclude10b51Param = False,
    sort: SortParam = InsiderSort.DATE,
    order: OrderParam = SortOrder.DESC,
) -> InsiderTransactionEnvelope:
    """The stock's Form 4 lines, newest trade first, with its issuer's lines
    that no one security could be linked to. Filtered as
    `/v1/insiders/transactions` is, open-market purchases and sales by default."""
    filters = _filters(code, role, min_value, start, end, exclude_10b5_1, entitled=entitled)
    security = await _resolve(session, ticker)
    return await _envelope(
        session,
        page,
        filters,
        defaulted=code is None,
        security_id=security.id,
        sort=sort,
        order=order,
        listing="stock-insiders",
    )


# --- clusters -------------------------------------------------------------------


class ClusterSort(StrEnum):
    """What the clusters are ordered by."""

    DATE = "date"
    INSIDERS = "insiders"
    VALUE = "value"


# A cluster whose lines gave no price has no value; below every real one.
_CLUSTER_VALUE_KEY: Final = func.coalesce(InsiderCluster.total_value, -1)


def _cluster_keyset(sort: ClusterSort, order: SortOrder) -> Keyset:
    """The sort, then the latest episode, then the issuer: unique within a
    parameter set, since one issuer's episodes do not overlap."""
    descending = order is SortOrder.DESC
    lead = {
        ClusterSort.DATE: [],
        ClusterSort.INSIDERS: [
            SortKey("insider_count", InsiderCluster.insider_count.expression, int, descending)
        ],
        ClusterSort.VALUE: [SortKey("value_key", _CLUSTER_VALUE_KEY, Decimal, descending)],
    }[sort]
    return Keyset(
        f"insider-clusters.{sort.value}.{order.value}",
        (
            *lead,
            SortKey("window_end", InsiderCluster.window_end.expression, date, descending),
            SortKey("issuer_cik", InsiderCluster.issuer_cik.expression, str, descending),
        ),
    )


def clusters_query(
    page: PageParams,
    *,
    window: int = DEFAULT_WINDOW,
    min_insiders: int = DEFAULT_MIN_INSIDERS,
    security_id: SecurityId | None = None,
    start: date | None = None,
    end: date | None = None,
    sort: ClusterSort = ClusterSort.DATE,
    order: SortOrder = SortOrder.DESC,
    trades_from: date | None,
) -> tuple[Select[Any], Keyset]:
    """One page of one parameter set's episodes, with their security, and the keyset.

    :param trades_from: The caller's window: no episode begun before it, so
        no purchase outside it is in a total. None for every day.
    :param security_id: One stock's: every episode of its issuer, and any
        linked to it.
    :param start: Episodes still going on or after this day.
    :param end: Episodes begun on or before this day.
    """
    keyset = _cluster_keyset(sort, order)
    cluster = InsiderCluster
    conditions: list[ColumnElement[bool]] = [
        cluster.window_days == window,
        cluster.min_insiders == min_insiders,
    ]
    if security_id is not None:
        conditions.append(
            or_(
                cluster.security_id == security_id,
                cluster.issuer_cik.in_(_issuer_ciks(security_id)),
            )
        )
    if trades_from is not None:
        conditions.append(cluster.window_start >= trades_from)
    if start is not None:
        conditions.append(cluster.window_end >= start)
    if end is not None:
        conditions.append(cluster.window_start <= end)

    rows = select(
        cluster.issuer_cik,
        cluster.window_start,
        cluster.window_end,
        cluster.issuer_name,
        cluster.security_id,
        cluster.insider_count,
        cluster.transaction_count,
        cluster.total_shares,
        cluster.total_value,
        _CLUSTER_VALUE_KEY.label("value_key"),
    ).where(*conditions)
    page_rows = page_statement(rows, keyset, page).cte("page")
    statement = (
        select(page_rows, Security.cusip, *ticker_labels(page_rows.c.window_end))
        .select_from(page_rows)
        .outerjoin(Security, Security.id == page_rows.c.security_id)
        .order_by(*keyset.order_by(on=page_rows))
    )
    return statement, keyset


def cluster_insiders_query(
    keys: list[tuple[str, date]], *, window: int, min_insiders: int
) -> Select[Any]:
    """Each insider's purchases in each of the episodes ``keys`` names, summed.

    The lines are found by ``id = ANY(transaction_ids)`` with their date
    between the episode's ends, both columns of each partition's primary key,
    so every partition is probed once per episode, and the ones outside its
    years are skipped at execution.
    """
    cluster = InsiderCluster
    latest_title = func.array_agg(
        aggregate_order_by(ReportingOwner.officer_title, Filing.filed_at.desc())
    ).filter(ReportingOwner.officer_title.is_not(None))[1]
    return (
        select(
            cluster.issuer_cik,
            cluster.window_start,
            Insider.cik.label("insider_cik"),
            Insider.name.label("insider_name"),
            func.bool_or(ReportingOwner.is_director).label("is_director"),
            func.bool_or(ReportingOwner.is_officer).label("is_officer"),
            latest_title.label("officer_title"),
            func.min(Line.transaction_date).label("first_date"),
            func.max(Line.transaction_date).label("last_date"),
            func.count().label("transaction_count"),
            func.sum(Line.shares).label("shares"),
            func.sum(Line.value_usd).label("value_usd"),
        )
        .select_from(cluster)
        .join(
            Line,
            and_(
                Line.id == func.any(cluster.transaction_ids),
                Line.transaction_date.between(cluster.window_start, cluster.window_end),
                Line.issuer_cik == cluster.issuer_cik,
            ),
        )
        .join(
            ReportingOwner,
            and_(
                ReportingOwner.filing_id == Line.filing_id,
                ReportingOwner.insider_id == Line.insider_id,
            ),
        )
        .join(Filing, Filing.id == Line.filing_id)
        .join(Insider, Insider.id == Line.insider_id)
        .where(
            cluster.window_days == window,
            cluster.min_insiders == min_insiders,
            tuple_(cluster.issuer_cik, cluster.window_start).in_(keys),
        )
        .group_by(cluster.issuer_cik, cluster.window_start, Insider.id)
        .order_by(
            cluster.issuer_cik,
            cluster.window_start,
            func.sum(Line.value_usd).desc().nulls_last(),
            Insider.name,
            Insider.cik,
        )
    )


def _cluster_insider(row: Any) -> ClusterInsider:
    return ClusterInsider(
        insider_cik=row.insider_cik,
        insider_name=row.insider_name,
        roles=[
            role
            for role, checked in ((Role.DIRECTOR, row.is_director), (Role.OFFICER, row.is_officer))
            if checked
        ],
        officer_title=row.officer_title,
        first_date=row.first_date,
        last_date=row.last_date,
        transaction_count=row.transaction_count,
        shares=row.shares,
        value_usd=row.value_usd,
    )


def _cluster(row: Any, insiders: list[ClusterInsider]) -> ClusterRow:
    return ClusterRow(
        issuer_cik=row.issuer_cik,
        issuer_name=row.issuer_name,
        cusip=row.cusip,
        ticker=row.ticker,
        current_ticker=row.current_ticker,
        delisted_by=row.delisted_by,
        window_start=row.window_start,
        window_end=row.window_end,
        insider_count=row.insider_count,
        transaction_count=row.transaction_count,
        total_shares=row.total_shares,
        total_value=row.total_value,
        insiders=insiders,
    )


def _grid_value(name: str, value: int, allowed: tuple[int, ...]) -> int:
    if value not in allowed:
        raise invalid(
            name,
            f"{name} must be one of {', '.join(map(str, allowed))}: clusters are "
            "detected ahead of time for those, not on request.",
        )
    return value


WindowParam = Annotated[
    int,
    Query(
        description=(
            "Days a qualifying window spans: a purchase, and every other up to this many "
            f"days before it. One of {', '.join(map(str, WINDOWS))}."
        ),
        json_schema_extra={"enum": list(WINDOWS)},
    ),
]
MinInsidersParam = Annotated[
    int,
    Query(
        description=(
            f"Distinct insiders a window needs. One of {', '.join(map(str, MIN_INSIDERS))}."
        ),
        json_schema_extra={"enum": list(MIN_INSIDERS)},
    ),
]
ClusterSortParam = Annotated[
    ClusterSort,
    Query(
        description=(
            "`date`, the episode's last purchase (the default), `insiders`, how many "
            "bought, or `value`, dollars paid."
        )
    ),
]
ClusterStartParam = Annotated[
    date | None,
    Query(alias="from", description="Episodes still going on or after this day, `2026-01-01`."),
]
ClusterEndParam = Annotated[
    date | None,
    Query(alias="to", description="Episodes begun on or before this day, `2026-06-30`."),
]


@router.get(
    "/insiders/clusters",
    operation_id="listInsiderClusters",
    response_model=InsiderClusterEnvelope,
    summary="Cluster buys: several of a company's insiders buying within a window",
    responses={**_NOT_FOUND, **PLAN_LIMITED, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.INSIDERS)
async def list_insider_clusters(
    session: SessionDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    window: WindowParam = DEFAULT_WINDOW,
    min_insiders: MinInsidersParam = DEFAULT_MIN_INSIDERS,
    ticker: FeedTickerParam = None,
    start: ClusterStartParam = None,
    end: ClusterEndParam = None,
    sort: ClusterSortParam = ClusterSort.DATE,
    order: OrderParam = SortOrder.DESC,
) -> InsiderClusterEnvelope:
    """Episodes in which at least `min_insiders` of one company's officers and
    directors bought its shares on the open market within `window` days, latest
    first. Windows that share a purchase are one episode. Found over all of
    history, not back from today. `meta.rules` states what counts as a purchase:
    plan purchases and 10% owners are left out. None begun before the caller's
    plan's window."""
    window = _grid_value("window", window, WINDOWS)
    min_insiders = _grid_value("min_insiders", min_insiders, MIN_INSIDERS)
    if start is not None and end is not None and start > end:
        raise invalid("from", f"from ({start}) is after to ({end}).")
    entitled.require_day(start)
    entitled.require_day(end)
    security_id = (await _resolve(session, ticker)).id if ticker is not None else None

    statement, keyset = clusters_query(
        page,
        window=window,
        min_insiders=min_insiders,
        security_id=security_id,
        start=start,
        end=end,
        sort=sort,
        order=order,
        trades_from=entitled.insiders_from,
    )
    rows, page_info = page_of(list(await session.execute(statement)), keyset, page)
    insiders: dict[tuple[str, date], list[ClusterInsider]] = {}
    if rows:
        keys = [(row.issuer_cik, row.window_start) for row in rows]
        for row in await session.execute(
            cluster_insiders_query(keys, window=window, min_insiders=min_insiders)
        ):
            insiders.setdefault((row.issuer_cik, row.window_start), []).append(
                _cluster_insider(row)
            )
    detected_at = await session.scalar(
        select(func.max(InsiderCluster.detected_at)).where(
            InsiderCluster.window_days == window, InsiderCluster.min_insiders == min_insiders
        )
    )
    return InsiderClusterEnvelope(
        data=[_cluster(row, insiders.get((row.issuer_cik, row.window_start), [])) for row in rows],
        meta=InsiderClusterMeta(
            window=window,
            min_insiders=min_insiders,
            rules=list(RULES),
            refreshed_at=detected_at,
            generated_at=datetime.now(UTC),
        ),
        page=page_info,
    )
