"""Investor (13F filer) endpoints: the list, and one investor.

Both read ``mv_filer_summary``, not the snapshot it aggregates, so they are as
of the views' last refresh. Each answers in one query, whatever the page size.
Everything a row shows that is not in the view is joined to the rows being
returned, never to the whole table before it is paged:

- the latest period, which is the view's newest row per filer (``DISTINCT
  ON``, over 1,300 rows),
- the top holding, from ``position_snapshot`` for that period (``DISTINCT ON``
  again, see :mod:`app.db.queries.top_holding`),
- the sparkline, the view's rows for the eight quarters before it,
- and when its filings were filed, through ``effective_filing``.

The top holding is the one figure read live, so for as long as the views are
stale it can be from a newer rebuild of the period than the value beside it.
The period is the same either way: the view decides it.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import (
    CTE,
    BindParameter,
    Interval,
    Row,
    Select,
    func,
    literal_column,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.api.deps import PageParamsDep, SessionDep
from app.api.meta import unscoped_meta
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.schemas.envelope import Envelope
from app.api.schemas.investor import (
    SPARKLINE_QUARTERS,
    InvestorDetail,
    InvestorSummary,
    TopHolding,
)
from app.core.periods import quarters_ending
from app.db.models import Filer, FilerCik, Filing, Security
from app.db.models.filer import FilerCategory
from app.db.queries.effective import EFFECTIVE_FILING
from app.db.queries.top_holding import top_holdings
from app.derived.views import FILER_SUMMARY

router = APIRouter(tags=["investors"])

#: Each filer's newest row of the view: its latest published period.
LATEST: Final = (
    select(FILER_SUMMARY)
    .distinct(FILER_SUMMARY.c.filer_id)
    .order_by(FILER_SUMMARY.c.filer_id, FILER_SUMMARY.c.period_of_report.desc())
    .subquery("latest")
)

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)

# Sort keys must be NOT NULL, and a filer with nothing published has no value
# and no positions. -1 is below every real one, so those filers come last.
_VALUE_KEY: Final = func.coalesce(LATEST.c.portfolio_value_usd, -1)
_POSITIONS_KEY: Final = func.coalesce(LATEST.c.position_count, -1)


class InvestorSort(StrEnum):
    VALUE = "value"
    POSITIONS = "positions"
    NAME = "name"


#: Largest first for the numbers, A to Z for the name. Each ends on a unique
#: column, so ties do not straddle a page boundary.
KEYSETS: Final = {
    InvestorSort.VALUE: Keyset(
        "investors.value",
        (
            SortKey("value_key", _VALUE_KEY, Decimal, descending=True),
            SortKey("filer_id", Filer.id.expression, int, descending=True),
        ),
    ),
    InvestorSort.POSITIONS: Keyset(
        "investors.positions",
        (
            SortKey("positions_key", _POSITIONS_KEY, int, descending=True),
            SortKey("filer_id", Filer.id.expression, int, descending=True),
        ),
    ),
    InvestorSort.NAME: Keyset(
        "investors.name",
        (
            SortKey("display_name", _DISPLAY_NAME, str),
            SortKey("slug", Filer.slug.expression, str),
        ),
    ),
}


def _filers() -> Select[Any]:
    """Every filer, with its latest period's row of the view, or nulls."""
    return (
        select(
            Filer.id.label("filer_id"),
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            Filer.manager_name,
            Filer.category,
            LATEST.c.period_of_report,
            LATEST.c.portfolio_value_usd,
            LATEST.c.position_count,
            LATEST.c.top10_weight_pct,
            LATEST.c.turnover_pct,
            _VALUE_KEY.label("value_key"),
            _POSITIONS_KEY.label("positions_key"),
        )
        .select_from(Filer)
        .outerjoin(LATEST, LATEST.c.filer_id == Filer.id)
    )


def _decorated(filers: CTE) -> Select[Any]:
    """``filers``, with what each row needs from outside the view.

    ``filers`` has :func:`_filers`'s columns, and is what is being returned.
    """
    top = top_holdings(filers).subquery("top")

    summary = FILER_SUMMARY
    # Eight quarters back from a quarter end is the same day of the same
    # month, two years earlier. Only the bound: _sparkline places each period.
    since = filers.c.period_of_report - literal_column("interval '2 years'", Interval)
    sparkline = (
        select(
            func.array_agg(
                aggregate_order_by(summary.c.period_of_report, summary.c.period_of_report)
            ).label("sparkline_periods"),
            func.array_agg(
                aggregate_order_by(summary.c.portfolio_value_usd, summary.c.period_of_report)
            ).label("sparkline_values"),
        )
        .where(
            summary.c.filer_id == filers.c.filer_id,
            summary.c.period_of_report > since,
        )
        .lateral("sparkline")
    )

    # Through effective_filing, for period_meta's reason: a superseded
    # original or a withheld filing is not what the period was published from.
    last_filed_at = (
        select(func.max(Filing.filed_at))
        .select_from(EFFECTIVE_FILING)
        .join(Filing, Filing.id == EFFECTIVE_FILING.c.filing_id)
        .where(
            EFFECTIVE_FILING.c.filer_id == filers.c.filer_id,
            EFFECTIVE_FILING.c.period_of_report == filers.c.period_of_report,
        )
        .scalar_subquery()
    )

    return (
        select(
            filers,
            Security.cusip.label("top_cusip"),
            Security.ticker.label("top_ticker"),
            Security.name.label("top_issuer_name"),
            top.c.weight_pct.label("top_weight_pct"),
            last_filed_at.label("last_filed_at"),
            sparkline.c.sparkline_periods,
            sparkline.c.sparkline_values,
        )
        .select_from(filers)
        .outerjoin(top, top.c.filer_id == filers.c.filer_id)
        .outerjoin(Security, Security.id == top.c.security_id)
        .outerjoin(sparkline, true())
    )


def list_query(
    page: PageParams,
    *,
    sort: InvestorSort = InvestorSort.VALUE,
    category: FilerCategory | None = None,
    q: str | None = None,
) -> tuple[Select[Any], Keyset]:
    """``GET /v1/investors``'s one query, and the keyset to hand its rows to
    :func:`~app.api.pagination.page_of` with."""
    statement = _filers()
    if category is not None:
        statement = statement.where(Filer.category == category.value)
    if q is not None:
        pattern = f"%{_escape_like(q)}%"
        statement = statement.where(
            or_(
                Filer.display_name.ilike(pattern, escape="\\"),
                Filer.name.ilike(pattern, escape="\\"),
                Filer.manager_name.ilike(pattern, escape="\\"),
            )
        )

    keyset = KEYSETS[sort]
    filers = page_statement(statement, keyset, page).cte("page")
    return _decorated(filers).order_by(*keyset.order_by(on=filers)), keyset


def detail_query(slug: str | BindParameter[str]) -> Select[Any]:
    """``GET /v1/investors/{slug}``'s one query: one row, or none for an unknown slug."""
    filer = _filers().where(Filer.slug == slug).cte("investor")
    first_period = (
        select(func.min(FILER_SUMMARY.c.period_of_report))
        .where(FILER_SUMMARY.c.filer_id == filer.c.filer_id)
        .scalar_subquery()
    )
    ciks = (
        select(func.array_agg(aggregate_order_by(FilerCik.cik, FilerCik.priority, FilerCik.cik)))
        .where(FilerCik.filer_id == filer.c.filer_id)
        .scalar_subquery()
    )
    return _decorated(filer).add_columns(first_period.label("first_period"), ciks.label("ciks"))


SortParam = Annotated[
    InvestorSort,
    Query(
        description=(
            "`value` and `positions` largest first, as of each investor's latest "
            "period. `name` A to Z. Investors with nothing published come last "
            "under the first two."
        )
    ),
]
CategoryParam = Annotated[FilerCategory | None, Query(description="Only investors of this style.")]
SearchParam = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=100,
        description="Part of the investor's name, its name on EDGAR, or its manager's.",
        examples=["buffett"],
    ),
]
SlugParam = Annotated[str, Path(examples=["berkshire-hathaway"])]


@router.get(
    "/investors",
    response_model=Envelope[InvestorSummary],
    summary="Every tracked investor, with its latest published portfolio",
)
async def list_investors(
    session: SessionDep,
    page: PageParamsDep,
    sort: SortParam = InvestorSort.VALUE,
    category: CategoryParam = None,
    q: SearchParam = None,
) -> Envelope[InvestorSummary]:
    """List investors, paginated. Each row names its own ``latest_period``."""
    statement, keyset = list_query(page, sort=sort, category=category, q=q)
    rows_on_page, page_info = page_of(list(await session.execute(statement)), keyset, page)

    return Envelope(
        data=[InvestorSummary(**_summary_fields(row)) for row in rows_on_page],
        meta=unscoped_meta(),
        page=page_info,
    )


@router.get(
    "/investors/{slug}",
    response_model=InvestorDetail,
    summary="One investor",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No investor with that slug."}},
)
async def read_investor(slug: SlugParam, session: SessionDep) -> InvestorDetail:
    """One investor, with the list's fields and its concentration, turnover,
    first period and CIKs."""
    statement = detail_query(slug)
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No investor {slug!r}. GET /v1/investors lists every one.",
        )

    return InvestorDetail(
        **_summary_fields(row),
        first_period=row.first_period,
        top10_weight_pct=row.top10_weight_pct,
        turnover_pct=row.turnover_pct,
        ciks=row.ciks or [],
    )


def _summary_fields(row: Row[Any]) -> dict[str, Any]:
    top = None
    if row.top_cusip is not None:
        top = TopHolding(
            cusip=row.top_cusip,
            ticker=row.top_ticker,
            issuer_name=row.top_issuer_name,
            weight_pct=row.top_weight_pct,
        )
    return {
        "slug": row.slug,
        "display_name": row.display_name,
        "manager_name": row.manager_name,
        "category": row.category,
        "latest_period": row.period_of_report,
        "last_filed_at": row.last_filed_at,
        "portfolio_value_usd": row.portfolio_value_usd,
        "position_count": row.position_count,
        "top_holding": top,
        "sparkline": _sparkline(row.period_of_report, row.sparkline_periods, row.sparkline_values),
    }


def _sparkline(
    latest: date | None, periods: list[date] | None, values: list[Decimal] | None
) -> list[Decimal | None]:
    """A value per quarter, for the quarters ending at ``latest``; None where none was published."""
    if latest is None:
        return []
    published = dict(zip(periods or [], values or [], strict=True))
    return [published.get(quarter) for quarter in quarters_ending(latest, SPARKLINE_QUARTERS)]


def _escape_like(text: str) -> str:
    """``text``, to match literally under ``ILIKE ... ESCAPE '\\'``: no wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
