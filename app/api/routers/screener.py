"""The screener: one period's positions, through a bounded set of named filters.

``GET /v1/screener``
    Every investor's position in every stock in ``?period=`` that passes each
    filter given, ANDed, keyset-paged. Read live from ``position_change``, with
    each page joined to the views for the stock's holders and net flow, and to
    the price estimate. The filters are :data:`app.api.screener.VOCABULARY`
    and nothing else: an unknown parameter is a 422.
``GET /v1/screener/filters``
    That vocabulary, for a client to build its controls from: each filter's
    key, label, type and options.
``GET /v1/screener/presets``
    The presets: screens chosen by hand, each a name, one sentence and the
    filters it is (:mod:`app.api.screener_presets`). Free for everyone.
``GET /v1/screener/presets/{preset}``
    One preset's screen: the same rows ``GET /v1/screener`` gives for its
    filters, of the latest quarter unless ``?period=`` says otherwise, with
    the preset in ``meta``. A URL to share; its filters are fixed, and any
    filter given beside them is a 422.

The parameters are the vocabulary's
------------------------------------
Each filter is a query parameter, declared from :data:`~app.api.screener.VOCABULARY`
(:func:`screen_filters`'s signature is written from it), so the OpenAPI
document, the validation and the SQL are one list, and a filter added there is
added everywhere. ``period`` is the route's own parameter, being required and
what the cache's lifetime is read from.

The backstop
------------
Every query runs under ``SET LOCAL statement_timeout`` of
:data:`~app.api.screener.STATEMENT_TIMEOUT_SECONDS`, inside the request's
transaction, so nothing outlives it. A query cancelled by it is a 504
``timeout``. None should be: every combination of filters reads one period of
``position_change`` and probes the views by their unique index.

The plan's window
-----------------
The period is inside the caller's ``history_from`` or it is a 402, as on every
read of 13F data. ``insider_cluster`` and ``insider_buyers`` read Form 4
trades too, so the quarter must start inside ``insiders_from``, and no trade
before it counts. A preset is free, not unlimited: its quarter is held to the
same window, and its default, the latest quarter, is inside every plan's.
"""

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import WithJsonSchema
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import Lifetime, cached
from app.api.deps import PageParamsDep, SessionDep
from app.api.entitlements import PLAN_LIMITED, EntitlementContext, EntitlementContextDep
from app.api.errors import INVALID, INVALID_CURSOR, ApiError, Responses, invalid, not_found
from app.api.estimates import estimates
from app.api.meta import unscoped_meta
from app.api.pagination import PageParams, page_of
from app.api.routers.market import _meta, _period
from app.api.routers.portfolio import SortOrder
from app.api.schemas.envelope import Envelope, Page
from app.api.schemas.error import ErrorCode, Problem
from app.api.schemas.screener import (
    AppliedFilter,
    ScreenerEnvelope,
    ScreenerFilter,
    ScreenerFilterEnvelope,
    ScreenerMeta,
    ScreenerPreset,
    ScreenerPresetEnvelope,
    ScreenerPresetMeta,
    ScreenerPresetResultEnvelope,
    ScreenerRow,
)
from app.api.schemas.types import Period
from app.api.screener import (
    FILTERS,
    STATEMENT_TIMEOUT_SECONDS,
    VOCABULARY,
    Applied,
    Filter,
    Scope,
    Screen,
    ScreenSort,
    quarter_start,
    screen_query,
    unknown_slugs,
)
from app.api.screener_presets import Preset, get_preset, list_presets
from app.derived.views import CONSENSUS_HOLDINGS, FILER_SUMMARY, QUARTER_FLOWS

router = APIRouter(tags=["screener"])

#: The query parameters that are not filters.
_NOT_FILTERS: Final = frozenset({"sort", "order", "limit", "cursor"})
_KNOWN: Final = frozenset(FILTERS) | _NOT_FILTERS
#: What a preset's screen takes: its filters are its own.
_PRESET_PARAMETERS: Final = frozenset({"period", "limit", "cursor"})

#: Postgres's ``query_canceled``: what ``statement_timeout`` raises.
_QUERY_CANCELED: Final = "57014"


# --- the parameters -------------------------------------------------------------


def _parameter(filter_: Filter) -> inspect.Parameter:
    """``filter_`` as a query parameter: every repeat collected, parsed by it."""
    return inspect.Parameter(
        filter_.key,
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=Annotated[
            list[str] | None,
            WithJsonSchema(filter_.json_schema()),
            Query(description=f"**{filter_.label}.** {filter_.description}"),
        ],
    )


async def screen_filters(request: Request, **raw: list[str] | None) -> Screen:
    """Every filter given, parsed, and a 422 naming each one that cannot be,
    or that is not a filter at all, in one ``detail.errors`` list."""
    errors: list[dict[str, Any]] = [
        {
            "type": "value_error",
            "loc": ("query", key),
            "msg": f"{key!r} is not a filter: the screener takes {', '.join(FILTERS)}",
        }
        for key in sorted(set(request.query_params) - _KNOWN)
    ]
    applied: list[Applied] = []
    for filter_ in VOCABULARY:
        values = raw.get(filter_.key)
        if filter_.required or values is None:
            continue
        try:
            applied.append(Applied(filter_.key, filter_.parse(values)))
        except ValueError as error:
            errors.append({"type": "value_error", "loc": ("query", filter_.key), "msg": str(error)})
    if errors:
        raise RequestValidationError(errors)
    return Screen(tuple(applied))


# FastAPI reads a dependency's parameters from its signature: one per filter,
# from the vocabulary, beside the request.
screen_filters.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
    [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        *(_parameter(f) for f in VOCABULARY if not f.required),
    ]
)

ScreenDep = Annotated[Screen, Depends(screen_filters)]
PeriodParam = Annotated[
    Period,
    Query(description=f"**{FILTERS['period'].label}.** {FILTERS['period'].description}"),
]
SortParam = Annotated[
    ScreenSort,
    Query(
        description=(
            "`value`, the position's dollars (the default), `weight`, its share of the "
            "portfolio, or `weight_change`, its move in points. Ties go to the larger "
            "position. A row with no weight or move is last either way."
        )
    ),
]
OrderParam = Annotated[SortOrder, Query(description="Largest first by default.")]

TIMED_OUT: Final[Responses] = {
    status.HTTP_504_GATEWAY_TIMEOUT: {
        "model": Problem,
        "description": (
            f"`timeout`: the screen's query ran past {STATEMENT_TIMEOUT_SECONDS} seconds "
            "and was cancelled."
        ),
    }
}


@asynccontextmanager
async def statement_timeout(session: AsyncSession, seconds: float) -> AsyncIterator[None]:
    """Every statement of ``session``'s transaction cancelled past ``seconds``,
    and a cancelled one answered 504."""
    # SET takes no bind parameters: the milliseconds are ours, an int.
    await session.execute(text(f"SET LOCAL statement_timeout = {round(seconds * 1000)}"))
    try:
        yield
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != _QUERY_CANCELED:
            raise
        raise ApiError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            ErrorCode.TIMEOUT,
            f"The screen took longer than {seconds:g} seconds and was stopped. Try again, "
            "or add a filter that narrows it.",
        ) from None


async def _screen(
    session: AsyncSession,
    page: PageParams,
    entitled: EntitlementContext,
    period: date | None,
    screen: Screen,
    sort: ScreenSort,
    order: SortOrder,
) -> tuple[list[ScreenerRow], ScreenerMeta, Page]:
    """One page of ``screen`` in ``period``, or the latest quarter inside the
    caller's window for None: the rows, ``meta`` and the page."""
    async with statement_timeout(session, STATEMENT_TIMEOUT_SECONDS):
        resolved = await _period(
            session, FILER_SUMMARY, period, what="positions", entitled=entitled
        )
        scope = Scope(period=resolved, insiders_from=entitled.insiders_from)
        if any(a.filter.insiders for a in screen.applied):
            # The whole quarter's trades, or "no cluster" would be a guess.
            entitled.require_day(quarter_start(resolved))
        if (slugs := screen.value("filer_slug")) and (
            unknown := await unknown_slugs(session, slugs)
        ):
            raise invalid("filer_slug", f"no investor has the slug {', '.join(map(repr, unknown))}")

        statement, keyset = screen_query(screen, scope, page, sort=sort, order=order)
        rows, page_info = page_of(list(await session.execute(statement)), keyset, page)
        meta = await _meta(session, resolved, CONSENSUS_HOLDINGS, QUARTER_FLOWS)
    return (
        [_row(row, resolved) for row in rows],
        ScreenerMeta(**meta.model_dump(), filters=_applied(screen)),
        page_info,
    )


def _applied(screen: Screen) -> list[AppliedFilter]:
    return [
        AppliedFilter(key=a.key, label=a.filter.label, values=a.display()) for a in screen.applied
    ]


# --- the routes -----------------------------------------------------------------


@router.get(
    "/screener",
    operation_id="screenPositions",
    response_model=ScreenerEnvelope,
    summary="Screen one quarter's positions by named filters",
    responses={
        **not_found("Nothing published for the period."),
        **PLAN_LIMITED,
        **INVALID_CURSOR,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            **INVALID[status.HTTP_422_UNPROCESSABLE_CONTENT],
            "description": (
                "`period` is missing, a filter's value is malformed, a parameter is not a "
                "filter, `sector` was asked for, or a slug names no investor. "
                "`detail.errors` says which."
            ),
        },
        **TIMED_OUT,
    },
)
@cached(period="period")
async def read_screener(
    session: SessionDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    period: PeriodParam,
    screen: ScreenDep,
    sort: SortParam = ScreenSort.VALUE,
    order: OrderParam = SortOrder.DESC,
) -> ScreenerEnvelope:
    """Every investor's position in a stock in `period` that passes every filter
    given: the filters are ANDed, and `GET /v1/screener/filters` lists them. A
    filter is about the position, its investor or its stock, and all of them
    hold for each row. Without `action`, the positions held at the period end.
    Largest position first. `meta.filters` echoes the filters; the rows are
    live, and their `holder_count` and `net_value_usd` as of `meta.refreshed_at`."""
    rows, meta, page_info = await _screen(session, page, entitled, period, screen, sort, order)
    return ScreenerEnvelope(data=rows, meta=meta, page=page_info)


@router.get(
    "/screener/filters",
    operation_id="getScreenerFilters",
    response_model=ScreenerFilterEnvelope,
    summary="The screener's filters: what each is called, takes and means",
)
@cached(Lifetime.METADATA)
async def read_screener_filters() -> Envelope[ScreenerFilter]:
    """Every filter `GET /v1/screener` takes, in the order to show them. Nothing
    beyond these: any other parameter is a 422. Not paginated."""
    return Envelope(
        data=[
            ScreenerFilter(
                key=f.key,
                label=f.label,
                type=f.type,
                description=f.description,
                required=f.required,
                available=f.available,
                options=list(f.options) or None,
                minimum=None if f.minimum is None else str(f.minimum),
                maximum=None if f.maximum is None else str(f.maximum),
            )
            for f in VOCABULARY
        ],
        meta=unscoped_meta(),
    )


def _preset(preset: Preset) -> ScreenerPreset:
    return ScreenerPreset(
        slug=preset.slug,
        name=preset.name,
        explanation=preset.explanation,
        filters=_applied(preset.screen),
        sort=preset.sort,
        order=preset.order,
        query=preset.query(),
    )


@router.get(
    "/screener/presets",
    operation_id="listScreenerPresets",
    response_model=ScreenerPresetEnvelope,
    summary="The presets: screens chosen by hand, each with what it shows",
)
@cached(Lifetime.METADATA)
async def read_screener_presets(session: SessionDep) -> ScreenerPresetEnvelope:
    """Every preset, in the order to show them, the first ones first. Each is a
    name, the sentence to show above its results, and the filters, sort and
    order it is: `GET /v1/screener/presets/{preset}` for its results, or its
    `query` to open it in a builder. Free for everyone. Not paginated."""
    presets = await list_presets(session)
    return ScreenerPresetEnvelope(data=[_preset(p) for p in presets], meta=unscoped_meta())


async def preset_parameters(request: Request) -> None:
    """A 422 naming every parameter a preset's screen does not take: its
    filters are fixed, and one more would be silently ignored otherwise."""
    if unknown := sorted(set(request.query_params) - _PRESET_PARAMETERS):
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", key),
                    "msg": (
                        f"{key!r} is not taken here: a preset's filters are its own. Open "
                        "its query in GET /v1/screener to change them"
                    ),
                }
                for key in unknown
            ]
        )


@router.get(
    "/screener/presets/{preset}",
    operation_id="screenPreset",
    response_model=ScreenerPresetResultEnvelope,
    summary="One preset's screen, of the latest quarter or another",
    dependencies=[Depends(preset_parameters)],
    responses={
        **not_found("No preset has the slug, or nothing is published for the period."),
        **PLAN_LIMITED,
        **INVALID_CURSOR,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            **INVALID[status.HTTP_422_UNPROCESSABLE_CONTENT],
            "description": (
                "`period` is malformed, or a parameter other than `period`, `limit` and "
                "`cursor` was given. `detail.errors` says which."
            ),
        },
        **TIMED_OUT,
    },
)
@cached(period="period")
async def read_screener_preset(
    session: SessionDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    preset: Annotated[str, Path(description="The preset's slug.", max_length=100)],
    period: Annotated[
        Period | None,
        Query(
            description=(
                "The quarter, as `2026Q1` or `2026-03-31`. The latest with positions "
                "inside the plan's window when left out."
            )
        ),
    ] = None,
) -> ScreenerPresetResultEnvelope:
    """The positions `GET /v1/screener` gives for the preset's filters, sort and
    order, in `period`: the same rows, and the same page for the same cursor.
    `meta.preset` is the preset, its `explanation` the sentence to show above
    the rows, and `meta.period` the quarter they are of."""
    found = await get_preset(session, preset)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No preset has the slug {preset!r}. GET /v1/screener/presets lists them.",
        )
    rows, meta, page_info = await _screen(
        session, page, entitled, period, found.screen, found.sort, found.order
    )
    return ScreenerPresetResultEnvelope(
        data=rows,
        meta=ScreenerPresetMeta(**meta.model_dump(), preset=_preset(found)),
        page=page_info,
    )


def _row(row: Any, period: Any) -> ScreenerRow:
    return ScreenerRow(
        slug=row.slug,
        display_name=row.display_name,
        category=row.category,
        cusip=row.cusip,
        ticker=row.ticker,
        current_ticker=row.current_ticker,
        delisted_by=row.delisted_by,
        issuer_name=row.issuer_name,
        prev_period=row.prev_period_of_report,
        action=row.action,
        shares=row.shares,
        shares_delta=row.shares_delta,
        shares_delta_pct=row.shares_delta_pct,
        value_usd=row.value_usd,
        weight_pct=row.weight_pct,
        weight_delta=row.weight_delta,
        holder_count=row.holder_count,
        net_value_usd=row.net_value_usd,
        traded_value_usd=row.traded_value_usd,
        **estimates(row, period),
    )
