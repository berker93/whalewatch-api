"""Downloads of the tables a page shows: a ``.csv`` and an ``.xlsx`` beside each listing.

``GET /v1/investors/{slug}/portfolio.csv`` (``.xlsx``)
    One period's holdings: :func:`~app.api.routers.portfolio.read_portfolio`'s rows.
``GET /v1/investors/{slug}/activity.csv`` (``.xlsx``)
    What the investor did: :func:`~app.api.routers.portfolio.read_activity`'s.
``GET /v1/stocks/{ticker}/owners.csv`` (``.xlsx``)
    Who holds the stock: :func:`~app.api.routers.stocks.read_owners`'s.
``GET /v1/flows.csv`` (``.xlsx``)
    The screener's results: :func:`~app.api.routers.market.read_flows`'s.
``GET /v1/insiders/transactions.csv`` (``.xlsx``)
    The insider feed: :func:`~app.api.routers.insiders.list_insider_transactions`'s.

Each surface is one dependency (:func:`portfolio_export` and the rest) that
takes its listing's parameters but the page's, runs the checks as the listing
runs them, and opens the listing's query with no page as a cursor: every row,
in the listing's order. The holdings take two parameters their listing
does not: ``action`` and ``q``, the holdings table's action chips and search
box, so the browser need not narrow the file it was sent. An unknown
investor or stock is a 404 and a period before the plan's window a 402,
before any of the file is written. What it
returns, an :class:`Export`, is written as CSV by :mod:`app.api.csv_export`
and as a workbook by :mod:`app.api.xlsx_export`, so the two files of a surface
have the same rows, the same columns and the same provenance.

Who may, and how much
---------------------
Every export takes :data:`AllowanceDep`, which asks, in this order:

1. ``export_enabled`` (Pro and the API plans), for either format: without it,
   a 402 ``plan_limit`` before anything is read. Signed out is Free, so the
   same 402, never a 401 or 403: what the website shows for it is the plan
   that has export, which the refusal names.
2. The listing's own checks: a 404, a 422, a period before the plan's window.
3. ``export_rows``, the plan's most rows in one file: a table with more is a
   402 ``plan_limit`` naming how many it has and the plan that takes them.
   Counted before the file starts, so it is a refusal, not a file cut short.
4. :attr:`~app.core.config.Settings.export_rate_limit_per_hour` (20) exports
   an hour per account, whatever its plan: a 429 ``rate_limited`` with
   ``Retry-After``. Abuse protection, so a setting, not an entitlement.
5. ``exports_month``, counted in the account's quota month
   (:mod:`app.accounts.usage`) and kept in ``usage_counter``, which
   ``GET /v1/me`` shows the account page. No limit on any paid plan today;
   one set by an UPDATE is a 429 ``quota_exceeded`` once spent, as an API
   key's calls are.

Only an export that passed all of them is counted or limited: a refusal
spends nothing. Each is also counted on its UTC day for the account that took
it (``user_activity_day.data_exports``), which the admin console shows. Redis
down lets an export through unlimited and uncounted, as it lets API calls.
Not cached: a file is one person's and taken once, and it is streamed, so
there is no answer to keep.

Provenance
----------
The last columns of every file (:mod:`app.api.csv_export`), and the same
in the workbook's ``Data`` sheet:

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

And above the rows, the CSV's ``#`` lines and the workbook's ``About`` sheet
say the same things: what the file is, the period, when it was made, how many
investors had filed, where on EDGAR the filings are, and the disclaimer. The
workbook adds a ``Methodology`` sheet (:mod:`app.api.methodology`).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Final
from urllib.parse import quote, urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import Row, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from app.accounts import activity
from app.accounts.api_keys import KeyHolder
from app.accounts.delivery import Courier
from app.accounts.sessions import Viewer, now
from app.api import methodology
from app.api.csv_export import CSV_FILE, Column, Kind, csv_response, filename, stream_rows
from app.api.deps import CourierDep, SessionDep, SettingsDep
from app.api.entitlements import (
    PLAN_LIMITED,
    EntitlementContext,
    EntitlementContextDep,
    plan_limit_error,
)
from app.api.errors import INVALID, ApiError, Responses, invalid, not_found
from app.api.meta import period_meta
from app.api.metering import METERED, spend
from app.api.rate_limit import SlidingWindowLimiter, bucket
from app.api.routers import insiders, market, portfolio, stocks
from app.api.routers.investors import SlugParam
from app.api.schemas.envelope import Coverage
from app.api.schemas.error import ErrorCode, Problem
from app.api.schemas.insider import CODE_LABELS, Role
from app.api.schemas.stock import StockNotFound
from app.api.xlsx_export import XLSX_FILE, Section, xlsx_response
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.periods import quarter_label
from app.db.models import Filer, FilerCik, Filing
from app.db.models.implied_price import ImpliedPrice
from app.db.models.position_change import ChangeAction, PositionChange
from app.db.queries.amendments import period_concerns
from app.db.queries.effective import EFFECTIVE_FILING
from app.derived.views import CONSENSUS_HOLDINGS

logger = get_logger(__name__)

router = APIRouter(tags=["exports"])

SOURCE_13F: Final = (
    "SEC EDGAR Form 13F-HR filings as each investor filed them. Compiled by WhaleWatch."
)
SOURCE_FORM4: Final = "SEC EDGAR Form 4 filings as each insider filed them. Compiled by WhaleWatch."
DISCLAIMER_13F: Final = (
    "A 13F states what was held on the last day of a quarter and is filed up to 45 days "
    "after it. Positions may have changed since. Not investment advice."
)
DISCLAIMER_FORM4: Final = (
    "A Form 4 is due two business days after the trade and some are filed late. "
    "Not investment advice."
)
UNITS: Final = (
    "Units: dollars for *_usd and prices. Percent for *_pct (12.5 is 12.5%). "
    "Empty is unknown or not applicable."
)
#: The About sheet's coverage for the insider feed, which has none to give.
NO_COVERAGE: Final = (
    "Not applicable: a Form 4 is filed for each trade, not for each quarter, so there "
    "is no set of insiders that should have filed."
)

#: EDGAR's own downloads of every 13F and of every Form 4: the source of an
#: export that draws on many filers' filings.
EDGAR_13F_DATA: Final = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
EDGAR_FORM4_DATA: Final = (
    "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
)

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)


# --- shared ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Export:
    """One surface's rows, opened, and what is said about them: ready to be either file.

    The CSV's ``#`` lines and the workbook's About sheet are both made from
    these fields, so the two never say different things.
    """

    name: tuple[str, ...]
    """:func:`~app.api.csv_export.filename`'s parts."""
    title: str
    period: str
    """What the rows are of, in words: a quarter, a span of quarters, a range of days."""
    page: str
    """The web page the rows are the table of."""
    edgar: str
    """Where on EDGAR the filings behind the rows are."""
    cursor: AsyncResult[Any]
    columns: tuple[Column, ...]
    methodology: tuple[Section, ...]
    form4: bool = False
    coverage: str | None = None
    """How many investors had filed for the period. ``None`` for Form 4s."""
    notes: tuple[tuple[str, str], ...] = ()
    """The rest, as label and text: the period's caveats, the actions or codes chosen."""
    generated_at: datetime = field(default_factory=now)

    @property
    def source(self) -> str:
        return SOURCE_FORM4 if self.form4 else SOURCE_13F

    @property
    def disclaimer(self) -> str:
        return DISCLAIMER_FORM4 if self.form4 else DISCLAIMER_13F

    def csv(self) -> StreamingResponse:
        header = [
            f"WhaleWatch export: {self.title}",
            f"Source: {self.source}",
            f"Generated: {_iso(self.generated_at)}",
            f"Disclaimer: {self.disclaimer}",
            UNITS,
            f"Page: {self.page}",
            f"EDGAR: {self.edgar}",
            *([] if self.coverage is None else [f"Coverage: {self.coverage}"]),
            *(f"{label}: {text}" for label, text in self.notes),
        ]
        return csv_response(filename(*self.name), header, self.cursor, self.columns)

    async def xlsx(self) -> StreamingResponse:
        title = f"WhaleWatch export: {self.title}"
        about: list[tuple[str, str | datetime]] = [
            ("Export", title),
            ("Period", self.period),
            ("Generated at", self.generated_at),
            ("Coverage", NO_COVERAGE if self.coverage is None else self.coverage),
            ("Source", self.source),
            ("EDGAR", self.edgar),
            ("Page", self.page),
            *self.notes,
            ("Disclaimer", self.disclaimer),
        ]
        return await xlsx_response(
            filename(*self.name, extension="xlsx"),
            self.cursor,
            self.columns,
            title=title,
            about=about,
            methodology=self.methodology,
        )


@dataclass(frozen=True, slots=True)
class Allowance:
    """What lets the caller take an export, and takes it: the plan's flag,
    checked when the dependency is resolved, and its rows, the hour's limit
    and the month's count in :meth:`take`, once the rows are known."""

    request: Request
    background: BackgroundTasks
    courier: Courier
    session: AsyncSession
    settings: Settings
    entitled: EntitlementContext

    async def take(self, statement: Select[Any]) -> None:
        """Refuse an export of ``statement``'s rows, or count it. Commits,
        before the cursor opens its own transaction.

        :raises PlanLimitError: 402, more rows than the plan's ``export_rows``.
        :raises ApiError: 429, the hour's exports or the month's taken.
        """
        await self._rows(statement)
        await self._hour()
        who = self.entitled.viewer
        if isinstance(who, Viewer | KeyHolder):
            await spend(
                self.request,
                self.background,
                self.courier,
                who,
                "exports_month",
                self.session,
                self.settings,
                headers=False,
            )
            await activity.count_export(self.session, who.user.id, now())
            await self.session.commit()

    async def _rows(self, statement: Select[Any]) -> None:
        """402 when ``statement`` has more rows than the plan exports.

        Counted up to one past the cap, with no order, so a plain listing stops
        there; one built on window functions (activity) is read whole either
        way, about a third of its own query's time. The whole count only for a
        refusal, to say how many it has.
        """
        cap = self.entitled.limit("export_rows")
        if cap is None:
            return
        unordered = statement.order_by(None)
        over = await self.session.scalar(_count(unordered.limit(cap + 1)))
        if not over or over <= cap:
            return
        rows = await self.session.scalar(_count(unordered)) or over
        raise plan_limit_error(
            "export_rows",
            allowed=cap,
            needed=rows,
            message=(
                f"Your plan exports up to {cap:,} rows at once, and this table has "
                f"{rows:,}. Narrow it with a filter or a shorter range."
            ),
        )

    async def _hour(self) -> None:
        """429 when the caller has taken the hour's exports. Per account, or
        per address signed out; nothing to count with, nothing refused."""
        limiter: SlidingWindowLimiter | None = getattr(
            self.request.app.state, "export_limiter", None
        )
        if limiter is None:
            return
        who = self.entitled.viewer
        if isinstance(who, Viewer | KeyHolder):
            client = f"user:{who.user.id}"
        elif self.request.client is not None:
            client = f"ip:{bucket(self.request.client.host)}"
        else:
            return
        try:
            decision = await limiter.hit(client)
        except (RedisError, OSError) as exc:
            logger.warning("export_limit.unavailable", error=repr(exc))
            return
        if not decision.admitted:
            hours = limiter.window_ms // 3_600_000
            raise ApiError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                ErrorCode.RATE_LIMITED,
                f"Too many exports: at most {limiter.limit} "
                f"{'an hour' if hours == 1 else f'in {hours} hours'}. "
                f"Try again in {decision.retry_after} seconds.",
                headers={"retry-after": str(decision.retry_after)},
            )


def _count(statement: Select[Any]) -> Select[tuple[int]]:
    return select(func.count()).select_from(statement.subquery("counted"))


async def allowance(
    request: Request,
    background: BackgroundTasks,
    courier: CourierDep,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
) -> Allowance:
    """The caller's :class:`Allowance`: 402 unless the plan has ``export_enabled``."""
    entitled.require_flag("export_enabled", message="Exporting is not on your plan.")
    return Allowance(request, background, courier, session, settings, entitled)


AllowanceDep = Annotated[Allowance, Depends(allowance)]

#: What every export documents beside its own refusals: the metered routes'
#: 429, which an export's two limits answer too.
LIMITED: Final[Responses] = {
    **PLAN_LIMITED,
    status.HTTP_429_TOO_MANY_REQUESTS: {
        **METERED[status.HTTP_429_TOO_MANY_REQUESTS],
        "description": (
            "`rate_limited`: too many requests from this address, or the hour's "
            "exports are taken; wait `Retry-After` seconds. `quota_exceeded`: the API "
            "key's month of calls is spent, or the month's exports on a plan that "
            "limits them; `detail.limit` says which, and `resets_at` when it resets."
        ),
    },
}


def _label(period: date | None) -> str | None:
    return None if period is None else quarter_label(period)


def _quarter(period: date) -> str:
    return f"{quarter_label(period)} (the quarter ended {period})"


def _query(request: Request) -> str:
    """The request's query string, for the page's link: the web's pages take
    the listing's parameters by the same names."""
    return f"?{request.url.query}" if request.url.query else ""


def _coverage(coverage: Coverage | None, label: str) -> str | None:
    if coverage is None:
        return None
    return (
        f"{coverage.filers_reported} of {coverage.filers_tracked} investors have filed for {label}."
    )


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _edgar_list(cik: str, form: str) -> str:
    """EDGAR's list of ``form`` filings by or about ``cik``."""
    query = urlencode({"action": "getcompany", "CIK": cik, "type": form, "owner": "include"})
    return f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"


async def _filer_on_edgar(session: AsyncSession, filer_id: int) -> str:
    """The investor's 13Fs on EDGAR, under its current CIK."""
    cik = await session.scalar(
        select(FilerCik.cik)
        .where(FilerCik.filer_id == filer_id)
        .order_by(FilerCik.priority.desc())
        .limit(1)
    )
    return EDGAR_13F_DATA if cik is None else _edgar_list(cik, "13F-HR")


async def _filing_on_edgar(session: AsyncSession, filer_id: int, period: date) -> str:
    """The investor's latest filing for ``period`` on EDGAR, the one ``filed_at``
    dates; its list of filings when the URL was not stored."""
    url = await session.scalar(
        select(Filing.source_url)
        .join(EFFECTIVE_FILING, EFFECTIVE_FILING.c.filing_id == Filing.id)
        .where(
            EFFECTIVE_FILING.c.filer_id == filer_id,
            EFFECTIVE_FILING.c.period_of_report == period,
            Filing.source_url.is_not(None),
        )
        .order_by(Filing.filed_at.desc())
        .limit(1)
    )
    return url or await _filer_on_edgar(session, filer_id)


#: The estimate's columns (:mod:`app.api.estimates`), as the listing's rows name them.
ESTIMATE_COLUMNS: Final = (
    Column("implied_price", kind=Kind.PRICE),
    Column("estimated_return_pct", kind=Kind.PCT, delta=True),
    Column(
        "estimated_return_as_of",
        lambda r: r.quote_date if r.estimated_return_pct is not None else None,
    ),
    Column("return_to_last_quarter_pct", kind=Kind.PCT, delta=True),
    Column(
        "last_quarter_period",
        lambda r: (
            _label(r.last_quarter_period) if r.return_to_last_quarter_pct is not None else None
        ),
    ),
)

# A position's change, coloured by its sign in a workbook.
SHARES_DELTA: Final = Column("shares_delta", kind=Kind.QUANTITY, delta=True)
SHARES_DELTA_PCT: Final = Column("shares_delta_pct", kind=Kind.PCT, delta=True)
WEIGHT_DELTA: Final = Column("weight_delta", kind=Kind.POINTS, delta=True)
CHANGE_COLUMNS: Final = (SHARES_DELTA, SHARES_DELTA_PCT, WEIGHT_DELTA)

#: What names a security, where a row is one.
SECURITY_COLUMNS: Final = (
    Column("cusip", kind=Kind.CODE),
    Column("ticker", kind=Kind.CODE),
    Column("current_ticker", kind=Kind.CODE),
    Column("issuer_name"),
    Column("delisted_by"),
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
        Column(
            "observation_count",
            (lambda r: r.price_observation_count) if price else nothing,
            kind=Kind.QUANTITY,
        ),
    ]
    if splits:
        columns += [
            Column("is_split_adjusted", lambda r: r.is_split),
            Column("split_ratio", kind=Kind.RATIO),
        ]
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


async def portfolio_export(
    slug: SlugParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    allowed: AllowanceDep,
    period: portfolio.PeriodParam = None,
    include_options: portfolio.IncludeOptions = False,
    sort: portfolio.SortParam = portfolio.PortfolioSort.WEIGHT,
    order: portfolio.OrderParam = portfolio.SortOrder.DESC,
    action: portfolio.HeldActionsParam = None,
    q: portfolio.HoldingSearchParam = None,
) -> Export:
    """``GET /v1/investors/{slug}/portfolio``'s every row, as an :class:`Export`,
    narrowed as the holdings table's action chips and search box narrow it."""
    entitled.require_period(period)
    filer_id, period = await portfolio._portfolio_period(session, slug, period, entitled)
    meta = await period_meta(session, period, filer_id=filer_id)
    caveats = await period_concerns(session, slug=slug, period=period)
    name = await session.scalar(select(_DISPLAY_NAME).where(Filer.id == filer_id))
    edgar = await _filing_on_edgar(session, filer_id, period)
    statement, _ = portfolio.portfolio_query(
        filer_id,
        period,
        None,
        sort=sort,
        order=order,
        include_options=include_options,
        actions=action,
        search=q,
        history_from=entitled.history_from,
    )
    await allowed.take(statement)

    narrowed: list[tuple[str, str]] = []
    if action is not None:
        narrowed.append(("Actions", "; ".join(sorted(a.value for a in action))))
    if q is not None:
        narrowed.append(("Matching", f"Ticker or company contains {q!r}"))
    label = quarter_label(period)
    columns = (
        *SECURITY_COLUMNS,
        Column("put_call"),
        Column("shares", kind=Kind.QUANTITY),
        Column("value_usd", kind=Kind.USD),
        Column("weight_pct", kind=Kind.PCT),
        Column("action"),
        *CHANGE_COLUMNS,
        Column("first_period"),
        *ESTIMATE_COLUMNS,
        *_provenance(as_of=lambda r: label, filed_at=lambda r: meta.latest_filing_at),
    )
    return Export(
        name=(slug, label, "holdings"),
        title=f"{name} holdings in {_quarter(period)}",
        period=_quarter(period),
        page=f"{settings.web_url}/investors/{quote(slug, safe='')}?{urlencode({'period': label})}",
        edgar=edgar,
        cursor=await stream_rows(session, statement),
        columns=columns,
        methodology=methodology.thirteen_f(),
        coverage=_coverage(meta.coverage, label),
        notes=(*narrowed, *(("Caveat", caveat) for caveat in caveats)),
    )


PORTFOLIO_RESPONSES: Final = {
    **not_found("No investor with that slug, or nothing published for the period."),
    **LIMITED,
    **INVALID,
}


@router.get(
    "/investors/{slug}/portfolio.csv",
    operation_id="exportInvestorPortfolio",
    response_class=StreamingResponse,
    summary="One investor's positions in one period, as a CSV file",
    responses={**CSV_FILE, **PORTFOLIO_RESPONSES},
)
async def export_portfolio(
    export: Annotated[Export, Depends(portfolio_export)],
) -> StreamingResponse:
    """Every position `GET /v1/investors/{slug}/portfolio` pages through, with
    the same parameters, in the same order, as
    `berkshire-hathaway_2026Q3_holdings.csv`. `action` and `q` narrow it as the
    holdings table's chips and search box do, and the `#` lines say so.
    `filed_at` is the investor's latest filing for the period; the `#` lines
    name what `meta.caveats` would.
    Needs `export_enabled`."""
    return export.csv()


@router.get(
    "/investors/{slug}/portfolio.xlsx",
    operation_id="exportInvestorPortfolioXlsx",
    response_class=StreamingResponse,
    summary="One investor's positions in one period, as an Excel workbook",
    responses={**XLSX_FILE, **PORTFOLIO_RESPONSES},
)
async def export_portfolio_xlsx(
    export: Annotated[Export, Depends(portfolio_export)],
) -> StreamingResponse:
    """The rows of `portfolio.csv` as `berkshire-hathaway_2026Q3_holdings.xlsx`:
    a Data sheet in Excel's own formats, the Methodology, and an About sheet
    with the period, its coverage, the filing on EDGAR and what `meta.caveats`
    would say. Needs `export_enabled`."""
    return await export.xlsx()


async def activity_export(
    slug: SlugParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    allowed: AllowanceDep,
    start: portfolio.RangeStart = None,
    end: portfolio.RangeEnd = None,
    action: portfolio.ActionsParam = None,
) -> Export:
    """``GET /v1/investors/{slug}/activity``'s every row, as an :class:`Export`."""
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
    # The newest quarter's: how far the universe had filed by the file's end.
    coverage = (
        None
        if last is None
        else _coverage((await period_meta(session, last)).coverage, quarter_label(last))
    )
    edgar = await _filer_on_edgar(session, filer_id)
    await allowed.take(statement)

    span = (
        "no quarters"
        if first is None
        else quarter_label(first)
        if first == last
        else f"{quarter_label(first)}-{quarter_label(last)}"
    )
    columns = (
        *SECURITY_COLUMNS,
        Column("period", lambda r: r.period_of_report),
        Column("prev_period", lambda r: r.prev_period_of_report),
        Column("action"),
        Column("shares", kind=Kind.QUANTITY),
        SHARES_DELTA,
        SHARES_DELTA_PCT,
        Column("value_usd", kind=Kind.USD),
        Column("traded_value_usd", kind=Kind.USD),
        Column("weight_pct", kind=Kind.PCT),
        WEIGHT_DELTA,
        *ESTIMATE_COLUMNS,
        *_provenance(
            as_of=lambda r: quarter_label(r.period_of_report), filed_at=lambda r: r.filed_at
        ),
    )
    return Export(
        name=(slug, span if first is not None else "none", "activity"),
        title=f"{name} activity in {span}",
        period=span,
        page=f"{settings.web_url}/investors/{quote(slug, safe='')}",
        edgar=edgar,
        cursor=await stream_rows(session, statement),
        columns=columns,
        methodology=methodology.thirteen_f(),
        coverage=coverage,
        notes=(("Actions", "; ".join(sorted(a.value for a in actions))),),
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


ACTIVITY_RESPONSES: Final = {**not_found("No investor with that slug."), **LIMITED, **INVALID}


@router.get(
    "/investors/{slug}/activity.csv",
    operation_id="exportInvestorActivity",
    response_class=StreamingResponse,
    summary="What one investor opened, added to, trimmed and exited, as a CSV file",
    responses={**CSV_FILE, **ACTIVITY_RESPONSES},
)
async def export_activity(
    export: Annotated[Export, Depends(activity_export)],
) -> StreamingResponse:
    """Every change `GET /v1/investors/{slug}/activity` pages through, with the
    same parameters, in the same order, as
    `berkshire-hathaway_2025Q4-2026Q3_activity.csv`, the quarters the rows span.
    `filed_at` is the investor's latest filing for the row's period. Needs
    `export_enabled`."""
    return export.csv()


@router.get(
    "/investors/{slug}/activity.xlsx",
    operation_id="exportInvestorActivityXlsx",
    response_class=StreamingResponse,
    summary="What one investor opened, added to, trimmed and exited, as an Excel workbook",
    responses={**XLSX_FILE, **ACTIVITY_RESPONSES},
)
async def export_activity_xlsx(
    export: Annotated[Export, Depends(activity_export)],
) -> StreamingResponse:
    """The rows of `activity.csv` as `berkshire-hathaway_2025Q4-2026Q3_activity.xlsx`,
    with the Methodology and About sheets. Coverage is the newest quarter's.
    Needs `export_enabled`."""
    return await export.xlsx()


# --- the stock ------------------------------------------------------------------


async def owners_export(
    ticker: stocks.TickerParam,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    allowed: AllowanceDep,
    period: stocks.PeriodParam = None,
) -> Export:
    """``GET /v1/stocks/{ticker}/owners``'s every row, as an :class:`Export`."""
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
    label = quarter_label(period)
    coverage = _coverage((await period_meta(session, period)).coverage, label)
    await allowed.take(statement)

    shown = security.ticker or security.cusip
    columns = (
        Column("slug"),
        Column("display_name"),
        Column("shares", kind=Kind.QUANTITY),
        Column("value_usd", kind=Kind.USD),
        Column("weight_pct", kind=Kind.PCT),
        Column("action"),
        *CHANGE_COLUMNS,
        *ESTIMATE_COLUMNS,
        *_provenance(as_of=lambda r: label, filed_at=lambda r: r.filed_at),
    )
    return Export(
        name=(shown, label, "holders"),
        title=f"holders of {security.name} ({shown}; CUSIP {security.cusip}) in {_quarter(period)}",
        period=_quarter(period),
        page=f"{settings.web_url}/stocks/{quote(shown, safe='')}?{urlencode({'period': label})}",
        edgar=EDGAR_13F_DATA,
        cursor=await stream_rows(session, statement),
        columns=columns,
        methodology=methodology.thirteen_f(),
        coverage=coverage,
    )


OWNERS_RESPONSES: Final = {
    **not_found(
        "Nothing by that ticker, alias or CUSIP, with suggestions; or nothing "
        "published for the period, without.",
        StockNotFound | Problem,
    ),
    **LIMITED,
    **INVALID,
}


@router.get(
    "/stocks/{ticker}/owners.csv",
    operation_id="exportStockOwners",
    response_class=StreamingResponse,
    summary="Every tracked investor holding one stock in one period, as a CSV file",
    responses={**CSV_FILE, **OWNERS_RESPONSES},
)
async def export_owners(export: Annotated[Export, Depends(owners_export)]) -> StreamingResponse:
    """Every holder `GET /v1/stocks/{ticker}/owners` pages through, in the same
    order, as `aapl_2026Q3_holders.csv`. `filed_at` is each holder's latest
    filing for the period. Needs `export_enabled`."""
    return export.csv()


@router.get(
    "/stocks/{ticker}/owners.xlsx",
    operation_id="exportStockOwnersXlsx",
    response_class=StreamingResponse,
    summary="Every tracked investor holding one stock in one period, as an Excel workbook",
    responses={**XLSX_FILE, **OWNERS_RESPONSES},
)
async def export_owners_xlsx(
    export: Annotated[Export, Depends(owners_export)],
) -> StreamingResponse:
    """The rows of `owners.csv` as `aapl_2026Q3_holders.xlsx`, with the
    Methodology and About sheets. Needs `export_enabled`."""
    return await export.xlsx()


# --- the screener ---------------------------------------------------------------


async def flows_export(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    allowed: AllowanceDep,
    period: market.PeriodParam = None,
    period_type: market.PeriodTypeParam = market.PeriodType.QUARTER,
    sector: market.SectorParam = None,
    min_investors: market.MinInvestorsParam = None,
    min_value: market.MinValueParam = None,
    direction: market.DirectionParam = None,
    sort: market.FlowSortParam = market.FlowSort.NET_VALUE,
    order: market.FlowOrderParam = portfolio.SortOrder.DESC,
) -> Export:
    """``GET /v1/flows``'s every row, as an :class:`Export`."""
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
    await allowed.take(statement)

    label = quarter_label(period)
    quarters = meta.quarters
    span = label if quarters is None else f"{quarters[0]}-{quarters[-1]}"
    refreshed = meta.refreshed_at
    columns = (
        *SECURITY_COLUMNS,
        Column("gross_bought_usd", lambda r: r.bought_value_usd, kind=Kind.USD),
        Column("gross_sold_usd", lambda r: r.sold_value_usd, kind=Kind.USD),
        Column("net_value_usd", kind=Kind.USD, delta=True),
        Column("net_shares", kind=Kind.QUANTITY, delta=True),
        Column("buyer_count", kind=Kind.QUANTITY),
        Column("seller_count", kind=Kind.QUANTITY),
        Column("new_positions", kind=Kind.QUANTITY),
        Column("exits", kind=Kind.QUANTITY),
        Column("holder_count", kind=Kind.QUANTITY),
        Column("total_value_usd", kind=Kind.USD),
        *_provenance(as_of=lambda r: label, filed_at=lambda r: meta.latest_filing_at, splits=False),
    )
    return Export(
        name=("screener", span, "flows"),
        title=f"screener flows in {span}",
        period=(
            _quarter(period)
            if quarters is None
            else f"{span}: the {len(quarters)} quarters ending {period}"
        ),
        page=f"{settings.web_url}/flows{_query(request)}",
        edgar=EDGAR_13F_DATA,
        cursor=await stream_rows(session, statement),
        columns=columns,
        methodology=methodology.thirteen_f(flows=True),
        coverage=_coverage(meta.coverage, label),
        notes=(("Aggregates as of", _iso(refreshed) if refreshed else "unrecorded"),),
    )


FLOWS_RESPONSES: Final = {
    **not_found("Nothing published for the period."),
    **LIMITED,
    **INVALID,
}


@router.get(
    "/flows.csv",
    operation_id="exportFlows",
    response_class=StreamingResponse,
    summary="The screener's stocks, as a CSV file",
    responses={**CSV_FILE, **FLOWS_RESPONSES},
)
async def export_flows(export: Annotated[Export, Depends(flows_export)]) -> StreamingResponse:
    """Every stock `GET /v1/flows` pages through, with the same filters, in the
    same order, as `screener_2026Q3_flows.csv` (`screener_2025Q4-2026Q3_flows.csv`
    for a year). `filed_at` is the newest filing the period's figures include,
    and the `#` lines say when the aggregates were refreshed. Needs
    `export_enabled`."""
    return export.csv()


@router.get(
    "/flows.xlsx",
    operation_id="exportFlowsXlsx",
    response_class=StreamingResponse,
    summary="The screener's stocks, as an Excel workbook",
    responses={**XLSX_FILE, **FLOWS_RESPONSES},
)
async def export_flows_xlsx(export: Annotated[Export, Depends(flows_export)]) -> StreamingResponse:
    """The rows of `flows.csv` as `screener_2026Q3_flows.xlsx`, with the
    Methodology, which explains the screener's dollar figures too, and the
    About sheet. Needs `export_enabled`."""
    return await export.xlsx()


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
    Column("accession_no", kind=Kind.CODE),
    Column("line_number"),
    Column("transaction_date"),
    Column("issuer_cik", kind=Kind.CODE),
    Column("issuer_name"),
    Column("issuer_ticker", kind=Kind.CODE),
    Column("cusip", kind=Kind.CODE),
    Column("ticker", kind=Kind.CODE),
    Column("current_ticker", kind=Kind.CODE),
    Column("delisted_by"),
    Column("insider_cik", kind=Kind.CODE),
    Column("insider_name"),
    Column("roles", _roles),
    Column("officer_title"),
    Column("other_role", lambda r: r.other_text),
    Column("code", lambda r: r.transaction_code),
    Column("code_label", lambda r: CODE_LABELS.get(r.transaction_code)),
    Column("acquired_disposed"),
    Column("is_derivative"),
    Column("security_title"),
    Column("shares", kind=Kind.QUANTITY),
    Column("price_per_share", kind=Kind.PRICE),
    Column("value_usd", kind=Kind.USD_CENTS),
    Column("shares_owned_after", kind=Kind.QUANTITY),
    Column("is_10b5_1"),
    Column("footnotes"),
    *_provenance(
        as_of=lambda r: r.transaction_date, filed_at=lambda r: r.filed_at, price=False, splits=False
    ),
)


async def insider_transactions_export(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    entitled: EntitlementContextDep,
    allowed: AllowanceDep,
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
) -> Export:
    """``GET /v1/insiders/transactions``'s every line, as an :class:`Export`."""
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
    issuer_cik = (
        None
        if security is None
        else await session.scalar(insiders._issuer_ciks(security.id).limit(1))
    )
    await allowed.take(statement)

    first = filters.start or filters.trades_from
    last = filters.end or entitled.today
    span = f"{first}-to-{last}" if first is not None else f"to-{last}"
    subject = "insiders" if security is None else (security.ticker or security.cusip)
    codes = (
        "every code"
        if filters.codes is None
        else "; ".join(f"{c} ({CODE_LABELS.get(c, 'unlabelled')})" for c in sorted(filters.codes))
    )
    days = f"from {first or 'the first loaded'} to {last}"
    return Export(
        name=(subject, span, "trades"),
        title=f"insider trades{'' if security is None else f' in {security.name}'} {days}",
        period=f"Trades {days}",
        page=f"{settings.web_url}/insiders{_query(request)}",
        edgar=EDGAR_FORM4_DATA if issuer_cik is None else _edgar_list(issuer_cik, "4"),
        cursor=await stream_rows(session, statement),
        columns=INSIDER_COLUMNS,
        methodology=methodology.FORM4,
        form4=True,
        notes=(("Codes", f"{codes}{' (the default)' if code is None else ''}"),),
    )


INSIDER_RESPONSES: Final = {
    **not_found(
        "Nothing by that ticker, alias or CUSIP. Suggests what may have been meant.",
        StockNotFound,
    ),
    **LIMITED,
    **INVALID,
}


@router.get(
    "/insiders/transactions.csv",
    operation_id="exportInsiderTransactions",
    response_class=StreamingResponse,
    summary="Insiders' Form 4 trades, as a CSV file",
    responses={**CSV_FILE, **INSIDER_RESPONSES},
)
async def export_insider_transactions(
    export: Annotated[Export, Depends(insider_transactions_export)],
) -> StreamingResponse:
    """Every line `GET /v1/insiders/transactions` pages through, with the same
    filters, in the same order: open-market purchases and sales unless `code`
    says otherwise, which the `#` lines state. As
    `insiders_2026-05-01-to-2026-11-01_trades.csv`, or `nvda_...` for one stock.
    `as_of_period` is the trade day and `filed_at` the Form 4's; a reported
    price has no `confidence`. Needs `export_enabled`."""
    return export.csv()


@router.get(
    "/insiders/transactions.xlsx",
    operation_id="exportInsiderTransactionsXlsx",
    response_class=StreamingResponse,
    summary="Insiders' Form 4 trades, as an Excel workbook",
    responses={**XLSX_FILE, **INSIDER_RESPONSES},
)
async def export_insider_transactions_xlsx(
    export: Annotated[Export, Depends(insider_transactions_export)],
) -> StreamingResponse:
    """The lines of `transactions.csv` as
    `insiders_2026-05-01-to-2026-11-01_trades.xlsx`, with a Methodology sheet
    on Form 4s and their codes, and the About sheet. Needs `export_enabled`."""
    return await export.xlsx()
