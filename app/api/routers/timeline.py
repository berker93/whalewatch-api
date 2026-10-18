"""The timeline: everything that happened to what a user follows, newest first, in one feed.

``GET /v1/timeline``
    Form 4 lines and 13F filings for a set of refs, filed within a window of
    days counted in a time zone. One request answers the whole window: not
    paged, since a window is at most 120 days of what one person follows.
``GET /v1/timeline/upcoming``
    The current quarter's 13Fs still due for the same refs.

Refs
----
``?ref=`` names each followed thing as ``kind:id``, repeated or
comma-separated: ``stock:037833100``, ``investor:berkshire-hathaway``,
``insider:0001197647``. The frontend's ``watchRefKey`` writes exactly this. One
parameter rather than one per kind, so that the order they were sent in, which
is the watchlist's and which ``/upcoming`` answers in, survives. A ref that
names nothing contributes nothing.

The rules
---------
They are the frontend's ``buildTimeline()``, which stays its mock and the
oracle this is tested against (``tests/integration/test_timeline_endpoint.py``
mirrors its cases):

- A followed **insider**: every Form 4 line they filed, any code.
- A followed **stock**: Form 4 open-market lines (``P``, ``S``) linked to it,
  or linked to no security and filed by its issuer (Q6), and each quarter's
  four largest holder changes by traded dollars, holds
  aside. The cut is over the whole quarter, before the window, so an event
  never appears because a larger one aged out; ties go to the slug, A to Z.
  An amended quarter counts once per holder, from its latest filing.
- A followed **investor**: one summary per 13F filing.
- A line from a followed insider at a followed stock is one event, filed under
  the insider. A followed investor that is also a top mover in a followed
  stock is two: they answer different follows.

Every event is placed at its filing's ``filed_at``, a 13F never at its period
end, and the window is on that: ``[from, to]`` as days in ``tz``, both
included. A line its issuer filed with no linked security (two classes, a
symbol naming neither or both) is under each of the issuer's followed stocks,
as on the stock page, but once: under the first the request named.

Time zones
----------
``tz`` is the account's zone, which the frontend always sends. Omitted, it is
the signed-in user's, or ``America/New_York`` for no one. Days are turned into
instants by Postgres, the same database that validates the zone
(:func:`app.accounts.rules.time_zone_problem`), so a zone it accepts is one it
can count in.

Not cached: an answer can depend on who is asking (``tz`` left out), and the
cached endpoints are shared.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from sqlalchemy import (
    ColumnElement,
    Date,
    DateTime,
    Row,
    Select,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.rules import DEFAULT_TIME_ZONE, time_zone_problem
from app.api.auth import OptionalViewerDep
from app.api.deps import SessionDep
from app.api.errors import INVALID, invalid
from app.api.routers.insiders import _codes_condition, _transaction
from app.api.routers.watchlist import MAX_ITEMS, normalise_cik, normalise_cusip
from app.api.schemas.envelope import Coverage
from app.api.schemas.market import LargestChange
from app.api.schemas.timeline import (
    ChangeCounts,
    FilingSummaryEvent,
    HolderChange,
    HolderChangeEvent,
    InsiderTradeEvent,
    InvestorRef,
    PendingHolders,
    TimelineEvent,
    TimelineEventEnvelope,
    TimelineMeta,
    TimelineSide,
    UpcomingFiling,
    UpcomingFilingEnvelope,
    UpcomingMeta,
)
from app.api.schemas.watchlist import WatchInsider, WatchInvestor, WatchStock
from app.core.periods import filing_deadline, quarter_end, quarter_label, quarters_ending
from app.db.models import (
    OPEN_MARKET_CODES,
    Filer,
    Filing,
    Insider,
    PositionChange,
    PositionSnapshot,
    ReportingOwner,
    SecCompanyTicker,
    Security,
)
from app.db.models import InsiderTransaction as Line
from app.db.models.position_change import ChangeAction
from app.db.queries.tickers import ticker_labels
from app.derived.views import FILER_SUMMARY, FILING_FEED, traded_usd

router = APIRouter(prefix="/timeline", tags=["timeline"])

#: The longest window, in days, both ends included.
MAX_WINDOW_DAYS: Final = 120

#: Holders of a followed stock shown per quarter: the largest moves by traded dollars.
HOLDER_CHANGES_PER_PERIOD: Final = 4

#: Pending holders an upcoming filing names; the rest are counted.
PENDING_NAMED: Final = 3

_CENT: Final = Decimal("0.01")
_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)
_MOVES: Final = (ChangeAction.NEW, ChangeAction.ADD, ChangeAction.TRIM, ChangeAction.EXIT)


# --- refs -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Refs:
    """The refs a request names, parsed, in the order it named them."""

    ordered: tuple[WatchStock | WatchInvestor | WatchInsider, ...] = ()
    stocks: frozenset[str] = field(default_factory=frozenset)
    investors: frozenset[str] = field(default_factory=frozenset)
    insiders: frozenset[str] = field(default_factory=frozenset)


def parse_refs(values: Sequence[str] | None) -> Refs:
    """``stock:<cusip>``, ``investor:<slug>``, ``insider:<cik>``, each once.

    :raises RequestValidationError: A ref that is not one of those, or more of
        them than a watchlist holds.
    """
    ordered: list[WatchStock | WatchInvestor | WatchInsider] = []
    seen: set[tuple[str, str]] = set()
    for value in values or ():
        for key in (part.strip() for part in value.split(",")):
            if not key:
                continue
            kind, _, ident = key.partition(":")
            ref: WatchStock | WatchInvestor | WatchInsider
            if kind == "stock" and (cusip := normalise_cusip(ident)) is not None:
                ref = WatchStock(kind="stock", cusip=cusip)
                ident = cusip
            elif kind == "investor" and ident.strip():
                ident = ident.strip()
                ref = WatchInvestor(kind="investor", slug=ident)
            elif kind == "insider" and (cik := normalise_cik(ident)) is not None:
                ref = WatchInsider(kind="insider", insider_cik=cik)
                ident = cik
            else:
                raise invalid(
                    "ref",
                    f"{key!r} is not a ref: write stock:<cusip>, investor:<slug> or insider:<cik>",
                )
            if (kind, ident) not in seen:
                seen.add((kind, ident))
                ordered.append(ref)
    if len(ordered) > MAX_ITEMS:
        raise invalid("ref", f"at most {MAX_ITEMS} refs, as many as a watchlist holds")
    return Refs(
        ordered=tuple(ordered),
        stocks=frozenset(r.cusip for r in ordered if isinstance(r, WatchStock)),
        investors=frozenset(r.slug for r in ordered if isinstance(r, WatchInvestor)),
        insiders=frozenset(r.insider_cik for r in ordered if isinstance(r, WatchInsider)),
    )


async def resolve_zone(session: AsyncSession, tz: str | None, viewer_zone: str | None) -> str:
    """``tz``, checked; or the viewer's zone; or the market's."""
    if tz is None:
        return viewer_zone or DEFAULT_TIME_ZONE
    if problem := await time_zone_problem(session, tz):
        raise invalid("tz", problem)
    return tz


# --- money and sides ------------------------------------------------------------


def money(value: Decimal) -> Decimal:
    """To the cent, and never ``-0.00``."""
    cents = value.quantize(_CENT)
    return cents if cents != 0 else Decimal("0.00")


def signed(value: Decimal, *, negative: bool) -> Decimal:
    return money(-abs(value) if negative else abs(value))


def form4_side(code: str) -> TimelineSide:
    """``P``, ``A`` and ``M`` acquire; ``S`` and ``G`` dispose; every other code is neither."""
    code = code.strip().upper()
    if code in ("P", "A", "M"):
        return TimelineSide.ACQUIRE
    if code in ("S", "G"):
        return TimelineSide.DISPOSE
    return TimelineSide.NEUTRAL


def action_side(action: str) -> TimelineSide:
    if action in (ChangeAction.NEW, ChangeAction.ADD):
        return TimelineSide.ACQUIRE
    if action in (ChangeAction.TRIM, ChangeAction.EXIT):
        return TimelineSide.DISPOSE
    return TimelineSide.NEUTRAL


def sign_side(value: Decimal) -> TimelineSide:
    if value > 0:
        return TimelineSide.ACQUIRE
    if value < 0:
        return TimelineSide.DISPOSE
    return TimelineSide.NEUTRAL


# --- the window -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    """``[start, end)``: the instants the days ``first`` to ``last`` in a zone cover."""

    first: date
    last: date
    start: datetime
    end: datetime


async def window_of(session: AsyncSession, first: date, last: date, tz: str) -> Window:
    """Midnight starting ``first`` and midnight after ``last``, in ``tz``, as instants."""

    def midnight(day: date) -> Any:
        return func.timezone(
            tz, literal(datetime.combine(day, time()), DateTime()), type_=DateTime(timezone=True)
        )

    start, end = (
        await session.execute(select(midnight(first), midnight(last + timedelta(days=1))))
    ).one()
    return Window(first=first, last=last, start=start, end=end)


# --- Form 4 ---------------------------------------------------------------------


def issuers_query(cusips: frozenset[str]) -> Select[Any]:
    """Each followed stock's issuer: the CIK SEC lists its current ticker under,
    as the stock page's insider table finds it (``_issuer_ciks`` there)."""
    return (
        select(Security.cusip, SecCompanyTicker.cik)
        .join(SecCompanyTicker, SecCompanyTicker.ticker == func.replace(Security.ticker, ".", "-"))
        .where(Security.cusip.in_(sorted(cusips)))
    )


async def followed_issuers(session: AsyncSession, refs: Refs) -> dict[str, str]:
    """Issuer CIK to the followed stock its unlinked lines are filed under: the
    first of the issuer's stocks the request named, when it named several."""
    if not refs.stocks:
        return {}
    by_cusip: dict[str, list[str]] = {}
    for row in await session.execute(issuers_query(refs.stocks)):
        by_cusip.setdefault(row.cusip, []).append(row.cik)
    issuers: dict[str, str] = {}
    for ref in refs.ordered:
        if isinstance(ref, WatchStock):
            for cik in by_cusip.get(ref.cusip, ()):
                issuers.setdefault(cik, ref.cusip)
    return issuers


def insider_trades_query(refs: Refs, window: Window, issuers: dict[str, str]) -> Select[Any]:
    """Lines filed in the window by a followed insider (any code), or at a
    followed stock (open-market codes), with what :func:`_transaction` reads.

    At a followed stock means linked to it, or linked to no security at all and
    filed by its issuer (Q6): the loader leaves a line unlinked when the issuer
    has several classes and the filing's symbol names neither or both, as
    Lennar's ``LEN, LEN.B`` does. The stock page counts those lines as each
    class's, and so does this.
    """
    owner = ReportingOwner
    open_market = _codes_condition(frozenset(OPEN_MARKET_CODES))
    matches: list[ColumnElement[bool]] = []
    if refs.insiders:
        matches.append(Insider.cik.in_(sorted(refs.insiders)))
    if refs.stocks:
        matches.append(and_(Security.cusip.in_(sorted(refs.stocks)), open_market))
    if issuers:
        matches.append(
            and_(
                Line.security_id.is_(None),
                Line.issuer_cik.in_(sorted(issuers)),
                open_market,
            )
        )
    return (
        select(
            Line.line_number,
            Line.transaction_date,
            Line.issuer_cik,
            Line.issuer_name,
            Line.issuer_ticker,
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
            Filing.accession_no,
            Filing.filed_at,
            Insider.cik.label("insider_cik"),
            Insider.name.label("insider_name"),
            Security.cusip,
            *ticker_labels(Line.transaction_date),
        )
        .select_from(Line)
        .join(owner, and_(owner.filing_id == Line.filing_id, owner.insider_id == Line.insider_id))
        .join(Filing, Filing.id == Line.filing_id)
        .join(Insider, Insider.id == Line.insider_id)
        .outerjoin(Security, Security.id == Line.security_id)
        .where(
            Filing.filed_at >= window.start,
            Filing.filed_at < window.end,
            # Nothing is traded after it is filed: lets the yearly partitions
            # after the window be skipped.
            Line.transaction_date <= window.last + timedelta(days=1),
            or_(*matches),
        )
    )


def insider_trade_events(
    rows: Sequence[Row[Any]], refs: Refs, issuers: dict[str, str]
) -> list[InsiderTradeEvent]:
    events: dict[str, InsiderTradeEvent] = {}
    for row in rows:
        trade = _transaction(row)
        open_market = trade.code.strip().upper() in OPEN_MARKET_CODES
        ref: WatchStock | WatchInsider
        if trade.insider_cik in refs.insiders:
            ref = WatchInsider(kind="insider", insider_cik=trade.insider_cik)
        elif trade.cusip in refs.stocks and open_market:
            ref = WatchStock(kind="stock", cusip=trade.cusip)
        elif trade.cusip is None and trade.issuer_cik in issuers and open_market:
            # Unlinked: under the followed stock of its issuer, cusip null on the trade.
            ref = WatchStock(kind="stock", cusip=issuers[trade.issuer_cik])
        else:  # pragma: no cover - the query asks for nothing else
            continue
        event_id = f"{trade.accession_no}:{trade.line_number}"
        events.setdefault(
            event_id,
            InsiderTradeEvent(
                kind="insider_trade",
                source="form4",
                id=event_id,
                at=trade.filed_at,
                ref=ref,
                side=form4_side(trade.code),
                value_usd=None
                if trade.value_usd is None
                else signed(trade.value_usd, negative=trade.acquired_disposed == "D"),
                trade=trade,
            ),
        )
    return list(events.values())


# --- 13F ------------------------------------------------------------------------


def _holdings_base(row: Row[Any]) -> dict[str, Any]:
    period: date = row.period_of_report
    filed_at: datetime = row.filed_at
    return {
        "source": "13f",
        "at": filed_at,
        "period": quarter_label(period),
        "as_of": period,
        "lag_days": (filed_at.astimezone(UTC).date() - period).days,
        "accession_no": row.accession_no,
        "investor": InvestorRef(slug=row.slug, display_name=row.display_name),
    }


def _traded() -> Any:
    """Dollars traded, rounded as ``/activity`` rounds them."""
    return func.round(traded_usd(), 2)


def filing_summaries_query(refs: Refs, window: Window) -> Select[Any]:
    """The followed investors' filings in the window, with their period's
    counts and net, value, and largest trade."""
    change, feed, summary = PositionChange, FILING_FEED, FILER_SUMMARY
    traded = _traded()
    followed = select(Filer.id).where(Filer.slug.in_(sorted(refs.investors)))
    counts = (
        select(
            change.filer_id,
            change.period_of_report,
            *(
                func.count().filter(change.action == action.value).label(action.value)
                for action in _MOVES
            ),
            func.sum(case((change.shares_delta < 0, -traded), else_=traded)).label("net"),
        )
        .where(
            change.filer_id.in_(followed),
            change.action != ChangeAction.HOLD.value,
            # The first period we have: every position "new", none of them
            # bought, which /activity leaves out too.
            change.prev_period_of_report.is_not(None),
        )
        .group_by(change.filer_id, change.period_of_report)
        .subquery("counts")
    )
    return (
        select(
            feed.c.period_of_report,
            feed.c.filed_at,
            feed.c.position_count,
            feed.c.largest_security_id,
            feed.c.largest_action,
            feed.c.largest_shares_delta,
            feed.c.largest_traded_value_usd,
            Filing.accession_no,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            summary.c.portfolio_value_usd,
            *(counts.c[action.value] for action in _MOVES),
            counts.c.net,
            Security.cusip,
            *ticker_labels(feed.c.period_of_report),
            Security.name.label("issuer_name"),
        )
        .select_from(feed)
        .join(Filing, Filing.id == feed.c.filing_id)
        .join(Filer, Filer.id == feed.c.filer_id)
        .outerjoin(
            summary,
            and_(
                summary.c.filer_id == feed.c.filer_id,
                summary.c.period_of_report == feed.c.period_of_report,
            ),
        )
        .outerjoin(
            counts,
            and_(
                counts.c.filer_id == feed.c.filer_id,
                counts.c.period_of_report == feed.c.period_of_report,
            ),
        )
        .outerjoin(Security, Security.id == feed.c.largest_security_id)
        .where(
            feed.c.filer_id.in_(followed),
            feed.c.filed_at >= window.start,
            feed.c.filed_at < window.end,
        )
    )


def filing_summary_events(rows: Sequence[Row[Any]]) -> list[FilingSummaryEvent]:
    events = []
    for row in rows:
        net = money(row.net if row.net is not None else Decimal(0))
        largest = None
        if row.largest_security_id is not None:
            largest = LargestChange(
                cusip=row.cusip,
                ticker=row.ticker,
                current_ticker=row.current_ticker,
                delisted_by=row.delisted_by,
                issuer_name=row.issuer_name,
                action=row.largest_action,
                shares_delta=row.largest_shares_delta,
                traded_value_usd=row.largest_traded_value_usd,
            )
        events.append(
            FilingSummaryEvent(
                kind="filing_summary",
                id=row.accession_no,
                ref=WatchInvestor(kind="investor", slug=row.slug),
                side=sign_side(net),
                value_usd=net,
                counts=ChangeCounts(
                    **{action.value: getattr(row, action.value) or 0 for action in _MOVES}
                ),
                largest_change=largest,
                position_count=row.position_count,
                portfolio_value_usd=row.portfolio_value_usd,
                **_holdings_base(row),
            )
        )
    return events


def holder_changes_query(refs: Refs, window: Window) -> Select[Any]:
    """Each followed stock's four largest holder moves in every quarter that has
    a filing in the window, ranked over the whole quarter, then windowed.

    A holder's move is dated by its period's latest filing in the feed: an
    amended quarter counts once, from the filing its figures come from.
    """
    change, feed = PositionChange, FILING_FEED
    traded = _traded()
    latest = (
        select(feed.c.filer_id, feed.c.period_of_report, feed.c.filing_id, feed.c.filed_at)
        .distinct(feed.c.filer_id, feed.c.period_of_report)
        .order_by(
            feed.c.filer_id,
            feed.c.period_of_report,
            feed.c.filed_at.desc(),
            feed.c.filing_id.desc(),
        )
        .subquery("latest")
    )
    in_window = select(feed.c.period_of_report).where(
        feed.c.filed_at >= window.start, feed.c.filed_at < window.end
    )
    followed = select(Security.id).where(Security.cusip.in_(sorted(refs.stocks)))
    ranked = (
        select(
            change.period_of_report,
            change.security_id,
            change.action,
            change.shares_delta,
            change.shares_delta_pct,
            traded.label("traded_value_usd"),
            latest.c.filing_id,
            latest.c.filed_at,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            func.row_number()
            .over(
                partition_by=(change.security_id, change.period_of_report),
                # Byte order, as the frontend's comparison of ASCII slugs.
                order_by=(traded.desc(), Filer.slug.collate("C")),
            )
            .label("rank"),
        )
        .join(
            latest,
            and_(
                latest.c.filer_id == change.filer_id,
                latest.c.period_of_report == change.period_of_report,
            ),
        )
        .join(Filer, Filer.id == change.filer_id)
        .where(
            change.security_id.in_(followed),
            change.action != ChangeAction.HOLD.value,
            change.prev_period_of_report.is_not(None),
            change.period_of_report.in_(in_window),
        )
        .subquery("ranked")
    )
    return (
        select(
            ranked,
            Filing.accession_no,
            Security.cusip,
            *ticker_labels(ranked.c.period_of_report),
            Security.name.label("issuer_name"),
        )
        .select_from(ranked)
        .join(Filing, Filing.id == ranked.c.filing_id)
        .join(Security, Security.id == ranked.c.security_id)
        .where(
            ranked.c.rank <= HOLDER_CHANGES_PER_PERIOD,
            ranked.c.filed_at >= window.start,
            ranked.c.filed_at < window.end,
        )
    )


def holder_change_events(rows: Sequence[Row[Any]]) -> list[HolderChangeEvent]:
    return [
        HolderChangeEvent(
            kind="holder_change",
            id=f"{row.accession_no}:{row.cusip}",
            ref=WatchStock(kind="stock", cusip=row.cusip),
            side=action_side(row.action),
            value_usd=signed(row.traded_value_usd, negative=row.shares_delta < 0),
            change=HolderChange(
                cusip=row.cusip,
                ticker=row.ticker,
                current_ticker=row.current_ticker,
                issuer_name=row.issuer_name,
                action=row.action,
                shares_delta=row.shares_delta,
                shares_delta_pct=row.shares_delta_pct,
                traded_value_usd=money(row.traded_value_usd),
            ),
            **_holdings_base(row),
        )
        for row in rows
    ]


async def build_timeline(session: AsyncSession, refs: Refs, window: Window) -> list[TimelineEvent]:
    events: list[InsiderTradeEvent | HolderChangeEvent | FilingSummaryEvent] = []
    if refs.insiders or refs.stocks:
        issuers = await followed_issuers(session, refs)
        rows = (await session.execute(insider_trades_query(refs, window, issuers))).all()
        events += insider_trade_events(rows, refs, issuers)
    if refs.investors:
        rows = (await session.execute(filing_summaries_query(refs, window))).all()
        events += filing_summary_events(rows)
    if refs.stocks:
        rows = (await session.execute(holder_changes_query(refs, window))).all()
        events += holder_change_events(rows)
    # Newest first; events at the same instant by id, so the order never
    # depends on the queries'.
    events.sort(key=lambda e: e.id)
    events.sort(key=lambda e: e.at, reverse=True)
    return [TimelineEvent(event) for event in events]


# --- upcoming -------------------------------------------------------------------


def current_quarter(today: date) -> date:
    """The quarter that ended most recently before ``today``: the one being filed."""
    this_quarter = quarter_end(today.year, (today.month - 1) // 3 + 1)
    return quarters_ending(this_quarter, 2)[0]


def _filed(period: date) -> Any:
    """``filer_id`` has a published portfolio for ``period``."""
    return exists().where(
        FILER_SUMMARY.c.filer_id == Filer.id, FILER_SUMMARY.c.period_of_report == period
    )


def pending_holders_query(cusips: frozenset[str], prior: date, current: date) -> Select[Any]:
    """Each stock's holders with shares at ``prior`` that have not published
    ``current``, largest holding first. An exit held nothing at ``prior``'s end
    of the quarter after, and the snapshot has no row for one, so nothing here
    is an exit."""
    snapshot = PositionSnapshot
    return (
        select(Security.cusip, Filer.slug, _DISPLAY_NAME.label("display_name"))
        .select_from(snapshot)
        .join(Security, Security.id == snapshot.security_id)
        .join(Filer, Filer.id == snapshot.filer_id)
        .where(
            Security.cusip.in_(sorted(cusips)),
            snapshot.period_of_report == prior,
            snapshot.shares > 0,
            ~_filed(current),
        )
        .order_by(Security.cusip, snapshot.value_usd.desc(), Filer.slug.collate("C"))
    )


async def build_upcoming(
    session: AsyncSession, refs: Refs, tz: str
) -> tuple[list[UpcomingFiling], date, Coverage]:
    today = await session.scalar(select(cast(func.timezone(tz, func.now()), Date)))
    assert today is not None  # a SELECT without FROM has its one row
    current = current_quarter(today)
    reported, tracked = (
        await session.execute(
            select(
                select(func.count())
                .select_from(FILER_SUMMARY)
                .where(FILER_SUMMARY.c.period_of_report == current)
                .scalar_subquery(),
                select(func.count()).select_from(Filer).scalar_subquery(),
            )
        )
    ).one()
    coverage = Coverage(filers_reported=reported, filers_tracked=tracked)
    if tracked > 0 and reported >= tracked:
        return [], current, coverage

    period, due_at = quarter_label(current), filing_deadline(current)
    unfiled: set[str] = set()
    if refs.investors:
        unfiled = set(
            await session.scalars(
                select(Filer.slug).where(Filer.slug.in_(sorted(refs.investors)), ~_filed(current))
            )
        )
    pending: dict[str, list[InvestorRef]] = {}
    if refs.stocks:
        prior = quarters_ending(current, 2)[0]
        for row in await session.execute(pending_holders_query(refs.stocks, prior, current)):
            pending.setdefault(row.cusip, []).append(
                InvestorRef(slug=row.slug, display_name=row.display_name)
            )

    upcoming = []
    for ref in refs.ordered:
        if isinstance(ref, WatchInvestor) and ref.slug in unfiled:
            upcoming.append(UpcomingFiling(ref=ref, period=period, due_at=due_at))
        elif isinstance(ref, WatchStock) and (holders := pending.get(ref.cusip)):
            upcoming.append(
                UpcomingFiling(
                    ref=ref,
                    period=period,
                    due_at=due_at,
                    pending_holders=PendingHolders(
                        count=len(holders), first=holders[:PENDING_NAMED]
                    ),
                )
            )
    return upcoming, current, coverage


# --- the routes -----------------------------------------------------------------

RefsParam = Annotated[
    list[str] | None,
    Query(
        alias="ref",
        description=(
            "What is followed, as `kind:id`, repeated or comma-separated: "
            "`stock:037833100` (a CUSIP), `investor:berkshire-hathaway` (a slug), "
            "`insider:0001197647` (a CIK). The frontend's `watchRefKey` writes these. "
            "None is an empty answer."
        ),
        examples=[["stock:037833100", "investor:berkshire-hathaway"]],
    ),
]
TzParam = Annotated[
    str | None,
    Query(
        description=(
            "The IANA zone days are counted in: the account's, which the frontend always "
            "sends. Omitted: the signed-in user's zone, or `America/New_York`."
        ),
        examples=["America/New_York"],
    ),
]
FromParam = Annotated[
    date,
    Query(
        alias="from",
        description="The window's first day, in `tz`, as `2026-06-21`. Included.",
    ),
]
ToParam = Annotated[
    date,
    Query(
        alias="to",
        description=(
            f"The window's last day, in `tz`, as `2026-10-18`. Included. At most "
            f"{MAX_WINDOW_DAYS} days after `from`, counting both."
        ),
    ),
]


@router.get(
    "",
    operation_id="getTimeline",
    response_model=TimelineEventEnvelope,
    summary="Form 4 trades and 13F filings for what is followed, newest first",
    responses=INVALID,
)
async def read_timeline(
    session: SessionDep,
    viewer: OptionalViewerDep,
    start: FromParam,
    end: ToParam,
    ref: RefsParam = None,
    tz: TzParam = None,
) -> TimelineEventEnvelope:
    """Every event for `ref` whose filing reached EDGAR between `from` and `to`,
    days in `tz`, newest first, ties by `id`. Not paged: the whole window in one
    answer, `[]` when nothing happened. A 422 when `from` is after `to`, the
    window is over 120 days, a date is not a real one, or `tz` is not an IANA
    zone."""
    if start > end:
        raise invalid("from", f"from ({start}) is after to ({end}).")
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise invalid("from", f"the window is at most {MAX_WINDOW_DAYS} days, both ends included")
    refs = parse_refs(ref)
    zone = await resolve_zone(session, tz, viewer.user.time_zone if viewer else None)
    window = await window_of(session, start, end, zone)
    events = await build_timeline(session, refs, window)
    return TimelineEventEnvelope(
        data=events,
        meta=TimelineMeta(
            window_from=start, window_to=end, tz=zone, generated_at=datetime.now(UTC)
        ),
        page=None,
    )


@router.get(
    "/upcoming",
    operation_id="getUpcomingFilings",
    response_model=UpcomingFilingEnvelope,
    summary="The current quarter's 13Fs still due for what is followed",
    responses=INVALID,
)
async def read_upcoming(
    session: SessionDep, viewer: OptionalViewerDep, ref: RefsParam = None, tz: TzParam = None
) -> UpcomingFilingEnvelope:
    """The current quarter is the one that ended most recently, in `tz`: from
    the day after a quarter ends, every followed investor is upcoming until it
    files. A followed stock is upcoming while any of the previous quarter's
    holders (with shares at its end) has not filed. In the order `ref` named
    them; insiders are left out. Empty once every tracked filer has filed."""
    refs = parse_refs(ref)
    zone = await resolve_zone(session, tz, viewer.user.time_zone if viewer else None)
    upcoming, current, coverage = await build_upcoming(session, refs, zone)
    return UpcomingFilingEnvelope(
        data=upcoming,
        meta=UpcomingMeta(
            period=quarter_label(current),
            period_end=current,
            coverage=coverage,
            tz=zone,
            generated_at=datetime.now(UTC),
        ),
        page=None,
    )
