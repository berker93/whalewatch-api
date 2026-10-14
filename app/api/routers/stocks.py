"""Stock endpoints: one security, who owns it, and how that has moved.

``GET /v1/stocks/{ticker}``
    The security, and how the tracked filers held and traded it in the latest
    published period, from ``mv_consensus_holdings`` and ``mv_quarter_flows``.
    So as of the views' last refresh, like the investor detail.
``GET /v1/stocks/{ticker}/owners``
    Every filer holding it in one period, largest first, read live from
    ``position_snapshot`` with each position's ``position_change`` beside it.
    Defaults to the latest period published for any filer, which ``meta``
    then states: a stock has no period of its own to default to. Keyset-paged.
``GET /v1/stocks/{ticker}/ownership-history``
    Per quarter, the shares and filers holding it, and the five largest
    holders as named series with everyone else as ``other``. Live, so the five
    and the total they are subtracted from are always the same rebuild.
``GET /v1/stocks/{ticker}/implied-prices``
    Per quarter, the price its holders' 13Fs imply, from ``implied_price``,
    with how many filings and how far they agree, and the price in today's
    shares beside it.
``GET /v1/stocks/{ticker}/splits``
    The splits ``security_split`` has detected, for annotating a chart of
    either of the above.

Finding the stock
-----------------
``{ticker}`` is a label, read as of the period being viewed: the owners'
``?period=``, and today for the detail and the history. It is spelled as
stored first (``brk-b`` and ``BRK/B`` are ``BRK.B``), then matched against, in
order (:func:`lookup_query`):

1. a ticker the security traded under *on that date*, from ``security_alias``;
2. its current ticker, ``security.ticker``;
3. a ticker it traded under at any other time, the most recent first, so
   ``FB`` still finds Meta;
4. its CUSIP, so a security no ticker resolved for can still be asked for.

So a recycled ticker finds the company that held it in the period being
viewed, and a renamed one is found under either name. Within a tier, the
security held most recently wins, then the newer row. When nothing matches,
the 404 suggests what might have been meant (:func:`suggestions_query`).

The five in the history
-----------------------
Chosen once, by value in the latest quarter anyone held the stock, and then
followed back through every quarter. Choosing them per quarter would give each
quarter a different five, and a chart whose series change identity as it goes
cannot be read. So an early quarter's largest holder may be in ``other``, and
that is the cost of a legible chart.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Path, Query, status
from sqlalchemy import (
    BindParameter,
    Date,
    Float,
    Integer,
    Row,
    Select,
    and_,
    cast,
    exists,
    func,
    literal,
    literal_column,
    null,
    select,
    true,
    union,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import Lifetime, cached
from app.api.deps import PageParamsDep, SessionDep
from app.api.errors import INVALID, INVALID_CURSOR, ApiError, not_found
from app.api.estimates import estimate_columns, estimates
from app.api.meta import period_meta, unscoped_meta
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.routers.investors import _escape_like
from app.api.schemas.envelope import Coverage, Envelope
from app.api.schemas.error import ErrorCode, Problem
from app.api.schemas.stock import (
    HolderPoint,
    ImpliedPricePoint,
    ImpliedPricePointEnvelope,
    OtherHolders,
    OwnershipPoint,
    OwnershipPointEnvelope,
    StockDetail,
    StockNotFound,
    StockNotFoundDetail,
    StockOwner,
    StockOwnerEnvelope,
    StockSplit,
    StockSplitEnvelope,
    StockSuggestion,
)
from app.api.schemas.types import Period
from app.core.periods import quarter_label, quarters_between
from app.core.tickers import normalise_ticker
from app.db.models import Filer, PositionChange, PositionSnapshot, Security, SecurityAlias
from app.db.models.implied_price import ImpliedPrice
from app.db.models.security_split import SecuritySplit
from app.db.queries.returns import estimated_return
from app.db.queries.tickers import valid_on
from app.derived.implied_price import PRICE_PLACES
from app.derived.views import CONSENSUS_HOLDINGS, FILER_SUMMARY, QUARTER_FLOWS

router = APIRouter(tags=["stocks"])

# What the query builders take: a value, or a bind parameter left for
# ``make explain`` to fill in.
SecurityId = int | BindParameter[int]
PeriodEnd = date | BindParameter[date]

#: How many holders the ownership history names.
TOP_HOLDERS: Final = 5
#: How many stocks a 404 suggests.
SUGGESTIONS: Final = 5
#: Below this, a name has too few trigrams to be compared usefully, and the
#: index has nothing to narrow the search with.
_MIN_NAME_QUERY: Final = 3

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)

# Zeros at the scale the columns have, so a stock nobody holds reads as
# "0.0000" shares and "0.00" dollars, like every other figure.
_NO_SHARES: Final = Decimal("0.0000")
_NO_DOLLARS: Final = Decimal("0.00")
#: What a split-adjusted price is rounded to: ``implied_price.price``'s scale.
_PRICE_QUANTUM: Final = Decimal(1).scaleb(-PRICE_PLACES)


def _key(ticker: str) -> str:
    """What every comparison is against: the path segment, spelled as stored."""
    return normalise_ticker(ticker)


def _last_held(security_id: Any) -> Any:
    """The newest period ``security_id`` is in ``position_snapshot``: one
    entry of the (security_id, period_of_report) index, read backwards."""
    snapshot = PositionSnapshot
    return (
        select(func.max(snapshot.period_of_report))
        .where(snapshot.security_id == security_id)
        .scalar_subquery()
    )


def lookup_query(
    key: str | BindParameter[str], *, cusip: bool, on: PeriodEnd | None = None
) -> Select[Any]:
    """The security ``key`` names on ``on``, as one row, or none.

    See the module docstring for the order of tiers.

    :param key: As stored (:func:`_key`).
    :param cusip: Whether to try ``key`` as a CUSIP too. Only a nine-character
        key can be one, and comparing a longer one with ``char(9)`` would
        either fail or, cast, compare its first nine characters.
    :param on: The date being viewed. Today when ``None``.
    """
    alias = SecurityAlias
    day: Any = on if on is not None else func.current_date()
    # How recent a range is: an open one is the most recent of all.
    recency = func.coalesce(alias.valid_to, literal(date.max, Date))
    always = literal(date.max, Date)
    candidates = [
        select(
            alias.security_id.label("security_id"),
            literal_column("0", Integer).label("via"),
            recency.label("recency"),
        ).where(func.upper(alias.ticker) == key, valid_on(day, alias)),
        select(Security.id, literal_column("1", Integer), always).where(
            func.upper(Security.ticker) == key
        ),
        select(alias.security_id, literal_column("2", Integer), recency).where(
            func.upper(alias.ticker) == key
        ),
    ]
    if cusip:
        # Cast, or a parameter shared with the text comparisons above is typed
        # text, cusip is compared as text, and uq_security_cusip is no use.
        candidates.append(
            select(Security.id, literal_column("3", Integer), always).where(
                Security.cusip == cast(key, Security.cusip.type)
            )
        )
    matches = union_all(*candidates).subquery("matches")

    return (
        select(Security.id, Security.cusip, Security.ticker, Security.name, Security.delisted_by)
        .join(matches, matches.c.security_id == Security.id)
        .order_by(
            matches.c.via,
            matches.c.recency.desc(),
            _last_held(Security.id).desc().nulls_last(),
            Security.id.desc(),
        )
        .limit(1)
    )


def suggestions_query(key: str) -> Select[Any]:
    """Up to :data:`SUGGESTIONS` securities ``key`` may have been meant for.

    A ticker, current or former, starting with it first, then a name with a word like it
    (``pg_trgm``'s ``<%``, which the trigram index on ``security.name``
    serves). Within each, the most dollars held in the latest period first,
    which is what puts APPLE INC above APPLIED DNA SCIENCES for ``APPL``.

    :param key: Upper-cased (:func:`_key`).
    """
    prefix = f"{_escape_like(key)}%"
    candidates = [
        select(
            Security.id.label("security_id"),
            literal_column("0", Integer).label("via"),
            literal_column("1.0", Float).label("score"),
        ).where(func.upper(Security.ticker).like(prefix, escape="\\")),
        select(
            SecurityAlias.security_id, literal_column("0", Integer), literal_column("1.0", Float)
        ).where(func.upper(SecurityAlias.ticker).like(prefix, escape="\\")),
    ]
    if len(key) >= _MIN_NAME_QUERY:
        candidates.append(
            select(
                Security.id,
                literal_column("1", Integer),
                func.word_similarity(key, Security.name, type_=Float),
            ).where(literal(key).op("<%")(Security.name))
        )
    matched = union_all(*candidates).subquery("matched")
    best = (
        select(
            matched.c.security_id,
            func.min(matched.c.via).label("via"),
            func.max(matched.c.score).label("score"),
        )
        .group_by(matched.c.security_id)
        .subquery("best")
    )

    consensus = CONSENSUS_HOLDINGS
    latest = select(func.max(consensus.c.period_of_report)).scalar_subquery()
    return (
        select(Security.cusip, Security.ticker, Security.name, Security.delisted_by)
        .select_from(best)
        .join(Security, Security.id == best.c.security_id)
        .outerjoin(
            consensus,
            and_(
                consensus.c.security_id == best.c.security_id,
                consensus.c.period_of_report == latest,
            ),
        )
        .order_by(
            best.c.via,
            best.c.score.desc(),
            func.coalesce(consensus.c.total_value_usd, -1).desc(),
            Security.id,
        )
        .limit(SUGGESTIONS)
    )


async def _resolve(session: AsyncSession, ticker: str, *, on: date | None = None) -> Row[Any]:
    """The security ``ticker`` names on ``on`` (today when ``None``): ``id``,
    ``cusip``, ``ticker``, ``name``, ``delisted_by``.

    :raises ApiError: 404, with suggestions, when it names none.
    """
    key = _key(ticker)
    statement = lookup_query(key, cusip=len(key) == 9, on=on)
    row = (await session.execute(statement)).one_or_none()
    if row is not None:
        return row

    suggested: list[Row[Any]] = list(await session.execute(suggestions_query(key))) if key else []
    raise ApiError(
        status.HTTP_404_NOT_FOUND,
        ErrorCode.NOT_FOUND,
        f"No stock {ticker!r}: not a ticker, alias or CUSIP we have. Few CUSIPs "
        "have a ticker resolved yet, so a stock may only be found by its CUSIP.",
        detail=StockNotFoundDetail(
            suggestions=[
                StockSuggestion(
                    cusip=s.cusip,
                    ticker=s.ticker,
                    current_ticker=s.ticker,
                    issuer_name=s.name,
                    delisted_by=s.delisted_by,
                )
                for s in suggested
            ],
        ),
    )


# --- the detail -----------------------------------------------------------------


def detail_query(security_id: SecurityId) -> Select[Any]:
    """The latest published period, its coverage, and the stock's row of each
    view for it. One row, whose figures are null where the stock was not held
    or not changed, and all null when nothing is published."""
    consensus, flows, summary = CONSENSUS_HOLDINGS, QUARTER_FLOWS, FILER_SUMMARY
    # max() over the views' unique index, which leads with the period.
    latest = select(func.max(consensus.c.period_of_report).label("period")).subquery("latest")
    return (
        select(
            latest.c.period,
            select(func.count())
            .select_from(summary)
            .where(summary.c.period_of_report == latest.c.period)
            .scalar_subquery()
            .label("filers_reported"),
            select(func.count()).select_from(Filer).scalar_subquery().label("filers_tracked"),
            consensus.c.holder_count,
            consensus.c.total_shares,
            consensus.c.total_value_usd,
            flows.c.net_shares,
            flows.c.net_value_usd,
            flows.c.new_positions,
            flows.c.exits,
        )
        .select_from(latest)
        .outerjoin(
            consensus,
            and_(
                consensus.c.period_of_report == latest.c.period,
                consensus.c.security_id == security_id,
            ),
        )
        .outerjoin(
            flows,
            and_(
                flows.c.period_of_report == latest.c.period,
                flows.c.security_id == security_id,
            ),
        )
    )


# --- the owners -----------------------------------------------------------------

#: Largest position first, then the filer. Both descending, so a page boundary
#: is one row comparison.
OWNERS_KEYSET: Final = Keyset(
    "stock-owners",
    (
        SortKey("value_usd", PositionSnapshot.value_usd.expression, Decimal, descending=True),
        SortKey("filer_id", PositionSnapshot.filer_id.expression, int, descending=True),
    ),
)


async def _owners_period(session: AsyncSession, period: date | None) -> date:
    """``period``, or the latest one published for any filer.

    :raises HTTPException: 404 when nothing is published, or nothing for
        ``period``. An empty list would read as a stock nobody holds.
    """
    snapshot = PositionSnapshot
    # Both through the (period_of_report, security_id) index: max() reads
    # one entry backwards, and exists() one entry.
    latest = select(func.max(snapshot.period_of_report)).scalar_subquery()
    published = (
        exists().where(snapshot.period_of_report == period) if period is not None else null()
    )
    row = (
        await session.execute(select(latest.label("latest"), published.label("published")))
    ).one()
    if row.latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Nothing is published yet."
        )
    latest_period: date = row.latest
    if period is None:
        return latest_period
    if not row.published:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Nothing is published for {quarter_label(period)}. The latest quarter "
                f"published is {quarter_label(latest_period)}."
            ),
        )
    return period


def owners_query(security_id: SecurityId, period: PeriodEnd, page: PageParams) -> Select[Any]:
    """One page of the stock's holders in ``period``, named and with the
    estimates (:mod:`app.api.estimates`), in :data:`OWNERS_KEYSET` order.

    The price and the returns are the stock's, so the same on every row, and
    joined per row all the same: a few hundred microseconds a page, and one
    query rather than two.
    """
    snapshot, change = PositionSnapshot, PositionChange
    rows = (
        select(
            snapshot.filer_id,
            snapshot.security_id,
            snapshot.shares,
            snapshot.value_usd,
            snapshot.weight_pct,
            change.action,
            change.shares_delta,
            change.shares_delta_pct,
            change.weight_delta,
            change.is_split,
            change.split_ratio,
        )
        .select_from(snapshot)
        # Outer, for the portfolio's reason: a missing change row should cost
        # the row its deltas, not the row.
        .outerjoin(
            change,
            and_(
                change.filer_id == snapshot.filer_id,
                change.period_of_report == snapshot.period_of_report,
                change.security_id == snapshot.security_id,
            ),
        )
        .where(snapshot.security_id == security_id, snapshot.period_of_report == period)
    )
    page_rows = page_statement(rows, OWNERS_KEYSET, page).cte("page")
    returns = estimated_return(page_rows.c.security_id, period)
    return (
        select(
            page_rows,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            *estimate_columns(returns),
        )
        .select_from(page_rows)
        .join(Filer, Filer.id == page_rows.c.filer_id)
        .outerjoin(returns, true())
        .order_by(*OWNERS_KEYSET.order_by(on=page_rows))
    )


# --- the history ----------------------------------------------------------------


def totals_query(security_id: SecurityId) -> Select[Any]:
    """Every published quarter from the first the stock was held in, with its
    holders, shares and dollars in it. Oldest first.

    A quarter published without the stock in it has null totals. Published is
    every period in ``mv_filer_summary``, and every one the stock is in, so a
    period the views do not have yet is still a row.
    """
    snapshot, summary = PositionSnapshot, FILER_SUMMARY
    totals = (
        select(
            snapshot.period_of_report,
            func.count().label("holder_count"),
            func.sum(snapshot.shares).label("total_shares"),
            func.sum(snapshot.value_usd).label("total_value_usd"),
        )
        .where(snapshot.security_id == security_id)
        .group_by(snapshot.period_of_report)
        .cte("totals")
    )
    published = union(
        select(summary.c.period_of_report), select(totals.c.period_of_report)
    ).subquery("published")
    return (
        select(
            published.c.period_of_report,
            totals.c.holder_count,
            totals.c.total_shares,
            totals.c.total_value_usd,
        )
        .select_from(published)
        .outerjoin(totals, totals.c.period_of_report == published.c.period_of_report)
        .where(
            published.c.period_of_report
            >= select(func.min(totals.c.period_of_report)).scalar_subquery()
        )
        .order_by(published.c.period_of_report)
    )


def top_holders_query(security_id: SecurityId, period: PeriodEnd) -> Select[Any]:
    """The :data:`TOP_HOLDERS` largest holders in ``period``, and each one's
    position in the stock in every period it published. Largest holder first,
    then oldest period first.

    ``shares`` is null in a period the filer published without the stock.
    Published, as in :func:`totals_query`, is the filer's periods in
    ``mv_filer_summary`` and every one it held the stock in.
    """
    snapshot, summary = PositionSnapshot, FILER_SUMMARY
    top = (
        select(snapshot.filer_id, snapshot.value_usd.label("rank_value"))
        .where(snapshot.security_id == security_id, snapshot.period_of_report == period)
        .order_by(snapshot.value_usd.desc(), snapshot.filer_id.desc())
        .limit(TOP_HOLDERS)
        .cte("top")
    )
    held = (
        select(
            snapshot.filer_id,
            snapshot.period_of_report,
            snapshot.shares,
            snapshot.value_usd,
        )
        .where(
            snapshot.security_id == security_id,
            snapshot.filer_id.in_(select(top.c.filer_id)),
        )
        .cte("held")
    )
    published = union(
        select(summary.c.filer_id, summary.c.period_of_report).where(
            summary.c.filer_id.in_(select(top.c.filer_id))
        ),
        select(held.c.filer_id, held.c.period_of_report),
    ).subquery("published")
    return (
        select(
            top.c.filer_id,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            published.c.period_of_report,
            held.c.shares,
            held.c.value_usd,
        )
        .select_from(top)
        .join(Filer, Filer.id == top.c.filer_id)
        .join(published, published.c.filer_id == top.c.filer_id)
        .outerjoin(
            held,
            and_(
                held.c.filer_id == published.c.filer_id,
                held.c.period_of_report == published.c.period_of_report,
            ),
        )
        .order_by(top.c.rank_value.desc(), top.c.filer_id.desc(), published.c.period_of_report)
    )


def ownership_points(totals: list[Row[Any]], series: list[Row[Any]]) -> list[OwnershipPoint]:
    """One point per quarter, from the first the stock was held in to the latest
    published, with :func:`top_holders_query`'s holders named in each.

    A quarter with nothing published at all is a point of nulls, so a chart
    shows the gap rather than drawing across it.

    :param totals: :func:`totals_query`'s rows.
    :param series: :func:`top_holders_query`'s rows, for the latest period
        in ``totals`` with any holders.
    """
    if not totals:
        return []
    by_period = {row.period_of_report: row for row in totals}

    holders: dict[int, tuple[str, str]] = {}
    positions: defaultdict[int, dict[date, Row[Any]]] = defaultdict(dict)
    for row in series:
        holders.setdefault(row.filer_id, (row.slug, row.display_name))
        positions[row.filer_id][row.period_of_report] = row

    points = []
    for quarter in quarters_between(min(by_period), max(by_period)):
        top: list[HolderPoint] = []
        named_count, named_shares, named_value = 0, _NO_SHARES, _NO_DOLLARS
        for filer_id, (slug, display_name) in holders.items():
            position = positions[filer_id].get(quarter)
            if position is None:  # the filer published nothing for the quarter
                top.append(HolderPoint(slug=slug, display_name=display_name))
            elif position.shares is None:  # published, without the stock
                top.append(
                    HolderPoint(
                        slug=slug,
                        display_name=display_name,
                        shares=_NO_SHARES,
                        value_usd=_NO_DOLLARS,
                    )
                )
            else:
                top.append(
                    HolderPoint(
                        slug=slug,
                        display_name=display_name,
                        shares=position.shares,
                        value_usd=position.value_usd,
                    )
                )
                named_count += 1
                named_shares += position.shares
                named_value += position.value_usd

        total = by_period.get(quarter)
        if total is None:  # nothing published for the quarter, by anyone
            points.append(OwnershipPoint(period=quarter, top_holders=top))
            continue
        holder_count = total.holder_count or 0
        total_shares = total.total_shares if total.total_shares is not None else _NO_SHARES
        total_value = total.total_value_usd if total.total_value_usd is not None else _NO_DOLLARS
        points.append(
            OwnershipPoint(
                period=quarter,
                holder_count=holder_count,
                total_shares=total_shares,
                total_value_usd=total_value,
                top_holders=top,
                other=OtherHolders(
                    holder_count=holder_count - named_count,
                    shares=total_shares - named_shares,
                    value_usd=total_value - named_value,
                ),
            )
        )
    return points


# --- implied prices and splits --------------------------------------------------


def implied_prices_query(security_id: SecurityId) -> Select[Any]:
    """Every ``implied_price`` row of the stock, oldest first: its primary key's
    slice for the security."""
    price = ImpliedPrice
    return (
        select(
            price.period_of_report,
            price.price,
            price.confidence,
            price.observation_count,
            price.unpriced_count,
            price.agreeing_count,
        )
        .where(price.security_id == security_id)
        .order_by(price.period_of_report)
    )


def splits_query(security_id: SecurityId) -> Select[Any]:
    """Every detected split of the stock, oldest first."""
    split = SecuritySplit
    return (
        select(
            split.period_of_report,
            split.numerator,
            split.denominator,
            split.ratio,
            split.confidence,
            split.holders,
            split.holders_agreeing,
            split.holders_exact,
        )
        .where(split.security_id == security_id)
        .order_by(split.period_of_report)
    )


def implied_price_points(prices: list[Row[Any]], splits: list[Row[Any]]) -> list[ImpliedPricePoint]:
    """One point per quarter from the first priced to the last, a quarter with
    no row a point of nulls, with each price in today's shares beside it.

    Adjusted here rather than in SQL: a stock has a handful of splits at most,
    and a ``Fraction`` of their numerators and denominators is exact where a
    product in ``numeric`` is rounded at each step.

    :param prices: :func:`implied_prices_query`'s rows.
    :param splits: :func:`splits_query`'s rows.
    """
    if not prices:
        return []
    by_period = {row.period_of_report: row for row in prices}
    points = []
    for quarter in quarters_between(min(by_period), max(by_period)):
        row = by_period.get(quarter)
        if row is None:
            points.append(ImpliedPricePoint(period=quarter))
            continue
        since = Fraction(1)
        for split in splits:
            if split.period_of_report > quarter:
                since *= Fraction(split.numerator, split.denominator)
        adjusted = (
            None
            if row.price is None
            else (row.price * since.denominator / since.numerator).quantize(_PRICE_QUANTUM)
        )
        points.append(
            ImpliedPricePoint(
                period=quarter,
                price=row.price,
                # Never rounded to nothing: implied_price stores no price under
                # half a millionth of a cent, but a 1-for-50 could make one.
                split_adjusted_price=adjusted if adjusted else None,
                confidence=row.confidence,
                observation_count=row.observation_count,
                unpriced_count=row.unpriced_count,
                agreeing_count=row.agreeing_count,
            )
        )
    return points


# --- the routes -----------------------------------------------------------------

TickerParam = Annotated[
    str,
    Path(
        min_length=1,
        max_length=32,
        description=(
            "A ticker, a former ticker or other spelling we hold as an alias, or a "
            "CUSIP. Case does not matter."
        ),
        examples=["AAPL", "037833100"],
    ),
]
PeriodParam = Annotated[
    Period | None,
    Query(
        description=(
            "The quarter, as `2026Q1` or `2026-03-31`. Defaults to the latest one "
            "published for any investor, which `meta.period` states."
        )
    ),
]
_NOT_FOUND = not_found(
    "Nothing by that ticker, alias or CUSIP. Suggests what may have been meant.",
    StockNotFound,
)


@router.get(
    "/stocks/{ticker}",
    operation_id="getStock",
    response_model=StockDetail,
    summary="One stock, and how the tracked investors held and traded it",
    responses={**_NOT_FOUND, **INVALID},
)
@cached(Lifetime.CURRENT_PERIOD)
async def read_stock(ticker: TickerParam, session: SessionDep) -> StockDetail:
    """The stock, with its holders, shares and dollars held, and the net
    change in them, in the latest period published for any investor."""
    security = await _resolve(session, ticker)
    row = (await session.execute(detail_query(security.id))).one()

    stock = StockDetail(
        cusip=security.cusip,
        ticker=security.ticker,
        current_ticker=security.ticker,
        issuer_name=security.name,
        delisted_by=security.delisted_by,
    )
    if row.period is None:
        return stock
    stock.period = row.period
    stock.coverage = Coverage(
        filers_reported=row.filers_reported, filers_tracked=row.filers_tracked
    )
    stock.holder_count = row.holder_count or 0
    stock.total_shares = _or(row.total_shares, _NO_SHARES)
    stock.total_value_usd = _or(row.total_value_usd, _NO_DOLLARS)
    stock.net_shares = _or(row.net_shares, _NO_SHARES)
    stock.net_value_usd = _or(row.net_value_usd, _NO_DOLLARS)
    stock.new_positions = row.new_positions or 0
    stock.exits = row.exits or 0
    return stock


@router.get(
    "/stocks/{ticker}/owners",
    operation_id="getStockOwners",
    response_model=StockOwnerEnvelope,
    summary="Every tracked investor holding one stock in one period",
    responses={
        **not_found(
            "Nothing by that ticker, alias or CUSIP, with suggestions; or nothing "
            "published for the period, without.",
            StockNotFound | Problem,
        ),
        **INVALID_CURSOR,
        **INVALID,
    },
)
@cached(period="period")
async def read_owners(
    ticker: TickerParam,
    session: SessionDep,
    page: PageParamsDep,
    period: PeriodParam = None,
) -> Envelope[StockOwner]:
    """Holders, largest position first, each with its change since that
    investor's previous published period. Exits are not owners, and are not
    listed. The ticker is read as of the period asked for (today without one):
    an old ticker finds the company that held it then."""
    security = await _resolve(session, ticker, on=period)
    period = await _owners_period(session, period)
    statement = owners_query(security.id, period, page)
    rows, page_info = page_of(list(await session.execute(statement)), OWNERS_KEYSET, page)

    return Envelope(
        data=[
            StockOwner(
                slug=row.slug,
                display_name=row.display_name,
                shares=row.shares,
                value_usd=row.value_usd,
                weight_pct=row.weight_pct,
                shares_delta=row.shares_delta,
                shares_delta_pct=row.shares_delta_pct,
                weight_delta=row.weight_delta,
                action=row.action,
                **estimates(row, period),
            )
            for row in rows
        ],
        meta=await period_meta(session, period),
        page=page_info,
    )


@router.get(
    "/stocks/{ticker}/ownership-history",
    operation_id="getStockOwnershipHistory",
    response_model=OwnershipPointEnvelope,
    summary="One stock's tracked ownership quarter by quarter, with its five largest holders",
    responses={**_NOT_FOUND, **INVALID},
)
@cached(Lifetime.CURRENT_PERIOD)
async def read_ownership_history(
    ticker: TickerParam, session: SessionDep
) -> Envelope[OwnershipPoint]:
    """Every quarter from the first the stock was held in to the latest
    published, oldest first. Empty for a stock no investor's published
    portfolio has held. Not paginated."""
    security = await _resolve(session, ticker)
    totals = list(await session.execute(totals_query(security.id)))
    held = [row.period_of_report for row in totals if row.holder_count]
    series = list(await session.execute(top_holders_query(security.id, max(held)))) if held else []
    return Envelope(data=ownership_points(totals, series), meta=unscoped_meta(), page=None)


@router.get(
    "/stocks/{ticker}/implied-prices",
    operation_id="getStockImpliedPrices",
    response_model=ImpliedPricePointEnvelope,
    summary="One stock's quarter-end prices, as its holders' 13Fs imply them",
    responses={**_NOT_FOUND, **INVALID},
)
@cached(Lifetime.CURRENT_PERIOD)
async def read_implied_prices(
    ticker: TickerParam, session: SessionDep
) -> Envelope[ImpliedPricePoint]:
    """Every quarter from the first any investor's published portfolio held the
    stock to the last, oldest first, with how many filings each price is from
    and how far they agree. Not market data: each price is the median of value
    over shares across the holders' 13Fs. Empty for a stock nobody has held.
    Not paginated."""
    security = await _resolve(session, ticker)
    prices = list(await session.execute(implied_prices_query(security.id)))
    splits = list(await session.execute(splits_query(security.id))) if prices else []
    return Envelope(data=implied_price_points(prices, splits), meta=unscoped_meta(), page=None)


@router.get(
    "/stocks/{ticker}/splits",
    operation_id="getStockSplits",
    response_model=StockSplitEnvelope,
    summary="One stock's splits, as its holders' share counts show them",
    responses={**_NOT_FOUND, **INVALID},
)
@cached(Lifetime.CURRENT_PERIOD)
async def read_splits(ticker: TickerParam, session: SessionDep) -> Envelope[StockSplit]:
    """Every split detected, oldest first, each dated the first quarter end
    after it: for marking on a chart of the implied prices or the ownership.
    Detected from the filings, not read from a corporate-action feed: a split
    the tracked investors did not hold through is not here. Empty for most
    stocks. Not paginated."""
    security = await _resolve(session, ticker)
    rows = await session.execute(splits_query(security.id))
    return Envelope(
        data=[
            StockSplit(
                period=row.period_of_report,
                numerator=row.numerator,
                denominator=row.denominator,
                ratio=row.ratio,
                confidence=row.confidence,
                holders=row.holders,
                holders_agreeing=row.holders_agreeing,
                holders_exact=row.holders_exact,
            )
            for row in rows
        ],
        meta=unscoped_meta(),
        page=None,
    )


def _or[T](value: T | None, default: T) -> T:
    return default if value is None else value
