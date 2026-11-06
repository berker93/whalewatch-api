"""Congress endpoints: what members, their spouses and children traded, from their PTRs.

``GET /v1/congress/transactions``
    Every member's PTR lines, latest disclosed first, filtered and keyset-paged.
``GET /v1/congress/members/{bioguide}``
    One member's lines, with who they are and which of their filings the rows
    cannot include.
``GET /v1/stocks/{ticker}/congress``
    One stock's lines, and options on it: the stock page's Congress section,
    beside its 13F holders.

All three read ``congress_transaction`` live, joined to the filing, the name
on it and the member that name is (:mod:`app.db.models.congress_member`).

Which lines
-----------
A line is in the rows when it was read surely enough (``parsed``): a line in
review is not a trade yet, and a rejected one never was. Besides those:

- A House line filed as ``deleted`` withdraws a line of an earlier filing, so
  it is left out. The line it withdraws is not: nothing links the two.
- A Senate amendment restates the whole report. The report it replaces — the
  same filer's, for the same day, with a lower amendment number — is left
  out once the amendment has a line in the rows, or every trade would be
  there twice.
- A filer not known to be a member yet is in, with ``member`` null and
  ``member_match`` saying why. A party filter leaves them out: their party is
  not known.

A filing with no line in the rows is counted in ``meta.congress_coverage``, and
on the member's page listed by name (``meta.unread_filings``), with the link
to the original.

The lags
--------
``notification_lag_days`` is the notification date less the trade's: how long
before the member was told, which is days for an account someone else
manages. ``disclosure_lag_days`` is the filing's date less the trade's: how
long before the public could know. The STOCK Act deadline is 30 days from
notification and never more than 45 from the trade, and ``late`` is the filing
date past it. The filing date is when the chamber received it; it can be
published a few days after, so a line marked on time is on time, and a late
one at least that late.

Both lags are null where they would not be the fact they claim to be: a
notification dated before the trade, a trade dated after its filing, and an
amending line, whose filing is not the one that first disclosed the trade.

Amounts
-------
The form asks for a band, and a band is all there is. ``meta.summary`` adds
the chosen lines' bands up end by end — the least they can have been worth,
and the most — over every page, never a midpoint. One open band ("over
$50,000,000") leaves the most unknown, and null.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import PlainValidator, WithJsonSchema
from sqlalchemy import (
    ColumnElement,
    Integer,
    Numeric,
    Select,
    Subquery,
    and_,
    case,
    cast,
    distinct,
    exists,
    func,
    select,
    true,
)
from sqlalchemy import Text as SqlText
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.cache import Lifetime, cached
from app.api.deps import PageParamsDep, SessionDep
from app.api.errors import INVALID, INVALID_CURSOR, ApiError, invalid, not_found
from app.api.pagination import Keyset, PageParams, SortKey, page_of, page_statement
from app.api.routers.portfolio import SortOrder
from app.api.routers.stocks import TickerParam, _resolve
from app.api.schemas.congress import (
    DEADLINE_AFTER_NOTIFICATION,
    DEADLINE_AFTER_TRADE,
    Chamber,
    CongressCoverage,
    CongressFeedMeta,
    CongressMember,
    CongressMemberEnvelope,
    CongressMemberMeta,
    CongressMemberRef,
    CongressSummary,
    CongressTerm,
    CongressTrade,
    CongressTradeEnvelope,
    MemberMatch,
    Owner,
    TradeType,
    TypeRange,
    UnreadFiling,
    UnreadReason,
)
from app.api.schemas.error import ErrorCode
from app.api.schemas.stock import StockNotFound
from app.db.models import (
    CongressFiledName,
    CongressFiling,
    CongressMemberTerm,
    CongressTransaction,
    Security,
)
from app.db.models import CongressMember as Member
from app.db.queries.tickers import ticker_labels
from app.ingestion.congress.assets import RESOLVABLE, AssetClass
from app.ingestion.congress.ptr import RowStatus

router = APIRouter(tags=["congress"])

Line = CongressTransaction
Filing = CongressFiling

#: What the form's owner codes are; no code is the member's own.
OWNERS: Final = {"SP": Owner.SPOUSE, "JT": Owner.JOINT, "DC": Owner.DEPENDENT_CHILD}

#: What ``?type=`` means by each: a partial sale is a sale.
TYPES: Final = {
    TradeType.PURCHASE: (TradeType.PURCHASE,),
    TradeType.SALE: (TradeType.SALE, TradeType.PARTIAL_SALE),
    TradeType.PARTIAL_SALE: (TradeType.PARTIAL_SALE,),
    TradeType.EXCHANGE: (TradeType.EXCHANGE,),
}

#: The order ``meta.summary.by_type`` lists them in.
SUMMARY_TYPES: Final = (TradeType.PURCHASE, TradeType.SALE, TradeType.EXCHANGE)

#: The most of a member's unread filings ``meta.unread_filings`` lists.
MAX_UNREAD: Final = 200

_PARSED: Final = RowStatus.PARSED.value


class CongressSort(StrEnum):
    """What the rows are ordered by."""

    DISCLOSED = "disclosed"
    TRADED = "traded"
    AMOUNT = "amount"
    LAG = "lag"


@dataclass(frozen=True, slots=True)
class Filters:
    """What a client can narrow the rows by, parsed. The defaults are everything."""

    bioguide_ids: frozenset[str] = frozenset()
    chamber: Chamber | None = None
    parties: frozenset[str] = frozenset()
    """Lower case."""
    types: frozenset[TradeType] = frozenset()
    """As stored: ``sale`` already widened to its partials."""
    asset_classes: frozenset[AssetClass] = frozenset()
    start: date | None = None
    end: date | None = None
    disclosed_start: date | None = None
    disclosed_end: date | None = None
    min_amount: int | None = None
    late: bool | None = None


DEFAULT_FILTERS: Final = Filters()


# --- the lines ------------------------------------------------------------------


def _superseded() -> ColumnElement[bool]:
    """A Senate report a later amendment of it, with a line in the rows, restates."""
    later, its_line = aliased(Filing), aliased(Line)
    return and_(
        Filing.chamber == Chamber.SENATE.value,
        exists().where(
            later.chamber == Filing.chamber,
            later.filed_name_id == Filing.filed_name_id,
            later.report_date == Filing.report_date,
            func.coalesce(later.amendment, 0) > func.coalesce(Filing.amendment, 0),
            exists().where(its_line.filing_id == later.id, its_line.parse_status == _PARSED),
        ),
    )


def lines_subquery() -> Subquery:
    """Every line in the rows, one per row, with what each endpoint shows and filters on.

    Not filtered by anything a client asks: a subquery Postgres pulls up into
    the statement that filters it, so its conditions reach the tables.
    """
    term = aliased(CongressMemberTerm)
    # The term they were serving on the trade day: their party then, not now.
    term_at = (
        select(term.party, term.state, term.district)
        .where(
            term.member_id == Member.id,
            term.chamber == Filing.chamber,
            term.start_date <= Line.transaction_date,
        )
        .order_by(term.start_date.desc())
        .limit(1)
        .lateral("term_at")
    )
    first_disclosure = and_(
        Line.filing_status.is_distinct_from("amended"),
        Filing.amendment.is_(None),
        Filing.filing_date >= Line.transaction_date,
    )
    notified = Line.notification_date >= Line.transaction_date
    deadline = case(
        (
            first_disclosure,
            func.least(
                Line.transaction_date + DEADLINE_AFTER_TRADE,
                case((notified, Line.notification_date + DEADLINE_AFTER_NOTIFICATION)),
            ),
        )
    )
    disclosure_lag = case(
        (first_disclosure, cast(Filing.filing_date - Line.transaction_date, Integer))
    )
    days_late = case(
        (first_disclosure, func.greatest(cast(Filing.filing_date - deadline, Integer), 0))
    )
    return (
        select(
            Line.id,
            Line.filing_id,
            Line.line,
            Filing.chamber,
            Filing.doc_id,
            Filing.filing_date,
            Filing.document_url,
            Filing.prefix,
            Filing.first_name,
            Filing.last_name,
            Filing.suffix,
            Filing.amendment,
            Filing.filed_name_id,
            func.coalesce(CongressFiledName.status, MemberMatch.UNLINKED.value).label(
                "member_match"
            ),
            Member.id.label("member_id"),
            Member.bioguide_id,
            Member.full_name.label("member_name"),
            func.coalesce(term_at.c.party, Member.party).label("party"),
            func.coalesce(term_at.c.state, Member.state).label("state"),
            case((term_at.c.state.is_(None), Member.district), else_=term_at.c.district).label(
                "district"
            ),
            Line.owner,
            Line.asset_name,
            Line.asset_type,
            Line.asset_class,
            Line.ticker.label("filed_ticker"),
            Line.security_id,
            Line.resolution_confidence.label("asset_confidence"),
            Line.transaction_type,
            Line.transaction_date,
            Line.notification_date,
            Line.amount_low,
            Line.amount_high,
            Line.amount_exact,
            case((notified, cast(Line.notification_date - Line.transaction_date, Integer))).label(
                "notification_lag_days"
            ),
            disclosure_lag.label("disclosure_lag_days"),
            deadline.label("disclosure_deadline"),
            days_late.label("days_late"),
            Line.filing_status,
            Line.cap_gains_over_200,
            Line.description,
            Line.comment,
            Line.extraction,
            Line.confidence.label("read_confidence"),
            # Sort keys must be NOT NULL. A parsed line has a band or a figure;
            # a lag nobody knows sorts below every known one.
            cast(func.coalesce(Line.amount_low, Line.amount_exact, 0), Numeric).label("amount_key"),
            func.coalesce(disclosure_lag, -1).label("lag_key"),
        )
        .select_from(Line)
        .join(Filing, Filing.id == Line.filing_id)
        .outerjoin(CongressFiledName, CongressFiledName.id == Filing.filed_name_id)
        .outerjoin(Member, Member.id == CongressFiledName.member_id)
        .outerjoin(term_at, true())
        .where(
            Line.parse_status == _PARSED,
            Line.filing_status.is_distinct_from("deleted"),
            ~_superseded(),
        )
        .subquery("lines")
    )


def _conditions(
    lines: Subquery,
    filters: Filters,
    *,
    security_id: int | None = None,
    member_id: int | None = None,
) -> list[ColumnElement[bool]]:
    c = lines.c
    conditions: list[ColumnElement[bool]] = []
    if security_id is not None:
        conditions.append(c.security_id == security_id)
    if member_id is not None:
        conditions.append(c.member_id == member_id)
    if filters.bioguide_ids:
        conditions.append(c.bioguide_id.in_(sorted(filters.bioguide_ids)))
    if filters.chamber is not None:
        conditions.append(c.chamber == filters.chamber.value)
    if filters.parties:
        conditions.append(func.lower(c.party).in_(sorted(filters.parties)))
    if filters.types:
        conditions.append(c.transaction_type.in_(sorted(kind.value for kind in filters.types)))
    if filters.asset_classes:
        conditions.append(c.asset_class.in_(sorted(kind.value for kind in filters.asset_classes)))
    if filters.start is not None:
        conditions.append(c.transaction_date >= filters.start)
    if filters.end is not None:
        conditions.append(c.transaction_date <= filters.end)
    if filters.disclosed_start is not None:
        conditions.append(c.filing_date >= filters.disclosed_start)
    if filters.disclosed_end is not None:
        conditions.append(c.filing_date <= filters.disclosed_end)
    if filters.min_amount is not None:
        conditions.append(c.amount_key >= filters.min_amount)
    if filters.late is not None:
        conditions.append(
            c.days_late > 0 if filters.late else c.days_late == 0,
        )
    return conditions


def _keyset(lines: Subquery, listing: str, sort: CongressSort, order: SortOrder) -> Keyset:
    """The sort, then the trade day, then the line: one row comparison a page."""
    descending = order is SortOrder.DESC
    c = lines.c
    lead = {
        CongressSort.DISCLOSED: [SortKey("filing_date", c.filing_date, date, descending)],
        CongressSort.TRADED: [],
        CongressSort.AMOUNT: [SortKey("amount_key", c.amount_key, Decimal, descending)],
        CongressSort.LAG: [SortKey("lag_key", c.lag_key, int, descending)],
    }[sort]
    return Keyset(
        f"{listing}.{sort.value}.{order.value}",
        (
            *lead,
            SortKey("transaction_date", c.transaction_date, date, descending),
            SortKey("id", c.id, int, descending),
        ),
    )


def transactions_query(
    page: PageParams | None,
    filters: Filters = DEFAULT_FILTERS,
    *,
    security_id: int | None = None,
    member_id: int | None = None,
    sort: CongressSort = CongressSort.DISCLOSED,
    order: SortOrder = SortOrder.DESC,
    listing: str = "congress",
) -> tuple[Select[Any], Keyset]:
    """One page of the lines passing ``filters``, with their security, and the keyset.

    :param listing: Names the keyset, so a cursor is only taken back by the
        listing that made it.
    """
    lines = lines_subquery()
    keyset = _keyset(lines, listing, sort, order)
    rows = select(lines).where(
        *_conditions(lines, filters, security_id=security_id, member_id=member_id)
    )
    page_rows = page_statement(rows, keyset, page).cte("page")
    statement = (
        select(page_rows, Security.cusip, *ticker_labels(page_rows.c.transaction_date))
        .select_from(page_rows)
        .outerjoin(Security, Security.id == page_rows.c.security_id)
        .order_by(*keyset.order_by(on=page_rows))
    )
    return statement, keyset


def _filer_name(row: Any) -> str:
    parts = (row.prefix, row.first_name, row.last_name, row.suffix)
    return " ".join(part.strip() for part in parts if part and part.strip())


def _trade(row: Any) -> CongressTrade:
    member = None
    if row.bioguide_id is not None:
        member = CongressMemberRef(
            bioguide_id=row.bioguide_id,
            name=row.member_name,
            party=row.party,
            state=row.state,
            district=row.district,
        )
    days_late = row.days_late
    return CongressTrade(
        chamber=Chamber(row.chamber),
        doc_id=row.doc_id,
        line=row.line,
        filing_date=row.filing_date,
        document_url=row.document_url,
        member=member,
        member_match=MemberMatch(row.member_match),
        filer_name=_filer_name(row),
        owner=OWNERS.get(row.owner, Owner.SELF),
        asset_name=row.asset_name,
        asset_type=row.asset_type,
        asset_class=row.asset_class,
        filed_ticker=row.filed_ticker,
        cusip=row.cusip,
        ticker=row.ticker,
        current_ticker=row.current_ticker,
        delisted_by=row.delisted_by,
        asset_confidence=row.asset_confidence,
        type=TradeType(row.transaction_type),
        transaction_date=row.transaction_date,
        notification_date=row.notification_date,
        amount_low=row.amount_low,
        amount_high=row.amount_high,
        amount_exact=row.amount_exact,
        notification_lag_days=row.notification_lag_days,
        disclosure_lag_days=row.disclosure_lag_days,
        disclosure_deadline=row.disclosure_deadline,
        days_late=days_late,
        late=None if days_late is None else days_late > 0,
        filing_status=row.filing_status,
        amendment=row.amendment,
        cap_gains_over_200=row.cap_gains_over_200,
        description=row.description,
        comment=row.comment,
        extraction=row.extraction,
        read_confidence=row.read_confidence,
    )


# --- the summary ------------------------------------------------------------------


async def summarise(
    session: AsyncSession,
    filters: Filters = DEFAULT_FILTERS,
    *,
    security_id: int | None = None,
    member_id: int | None = None,
) -> CongressSummary:
    """Every line passing ``filters`` added up: counts, the lags, and per kind of
    trade the bands' low ends and high ends."""
    lines = lines_subquery()
    chosen = (
        select(lines)
        .where(*_conditions(lines, filters, security_id=security_id, member_id=member_id))
        .subquery("chosen")
    )
    c = chosen.c
    # A filer with no member is told apart by their filed name, or, not yet
    # linked to one, by the name on the filing.
    filer = func.coalesce(
        cast(c.filed_name_id, SqlText),
        func.concat(c.chamber, ":", c.last_name, ":", c.first_name),
    )
    totals = (
        await session.execute(
            select(
                func.count(),
                func.count(distinct(c.filing_id)),
                func.count(distinct(c.member_id)),
                func.count(distinct(filer)).filter(c.member_id.is_(None)),
                func.min(c.transaction_date),
                func.max(c.transaction_date),
                func.count().filter(c.days_late > 0),
                func.count(c.disclosure_lag_days),
                func.percentile_disc(0.5).within_group(c.disclosure_lag_days),
                func.max(c.disclosure_lag_days),
            )
        )
    ).one()
    kind = case(
        (c.transaction_type == TradeType.PARTIAL_SALE.value, TradeType.SALE.value),
        else_=c.transaction_type,
    )
    low = func.coalesce(c.amount_low, c.amount_exact)
    high = func.coalesce(c.amount_high, c.amount_exact)
    open_band = and_(c.amount_low.is_not(None), c.amount_high.is_(None))
    ranges = {
        row.kind: row
        for row in await session.execute(
            select(
                kind.label("kind"),
                func.count().label("transactions"),
                func.count().filter(open_band).label("open_ended"),
                func.floor(func.sum(low)).label("low"),
                case((func.bool_or(open_band), None), else_=func.ceil(func.sum(high))).label(
                    "high"
                ),
            ).group_by(kind)
        )
    }
    (
        transactions,
        filings,
        members,
        unmatched,
        first_day,
        last_day,
        late,
        lag_known,
        median_lag,
        max_lag,
    ) = totals
    return CongressSummary(
        transactions=transactions,
        filings=filings,
        members=members,
        unmatched_filers=unmatched,
        by_type=[
            TypeRange(
                type=kind_,
                transactions=ranges[kind_.value].transactions,
                amount_low=int(ranges[kind_.value].low),
                amount_high=_whole(ranges[kind_.value].high),
                open_ended=ranges[kind_.value].open_ended,
            )
            for kind_ in SUMMARY_TYPES
            if kind_.value in ranges
        ],
        first_transaction_date=first_day,
        last_transaction_date=last_day,
        late=late,
        lag_known=lag_known,
        median_disclosure_lag_days=median_lag,
        max_disclosure_lag_days=max_lag,
    )


# --- coverage ------------------------------------------------------------------


def _filing_counts() -> Subquery:
    """Per filing: its lines in the rows, in review, placeholders, and the
    rows' lines that should name a stock, and those that do."""
    resolvable = Line.asset_class.in_([kind.value for kind in RESOLVABLE])
    parsed = Line.parse_status == _PARSED
    return (
        select(
            Line.filing_id,
            func.count().filter(parsed).label("parsed"),
            func.count().filter(Line.parse_status == RowStatus.NEEDS_REVIEW.value).label("review"),
            func.count().filter(Line.parse_status == RowStatus.PLACEHOLDER.value).label("holder"),
            func.count().filter(parsed, resolvable).label("resolvable"),
            func.count()
            .filter(parsed, resolvable, Line.security_id.is_not(None))
            .label("resolved"),
        )
        .group_by(Line.filing_id)
        .subquery("counts")
    )


def _unread_reason(counts: Subquery) -> ColumnElement[str | None]:
    """Why a filing has no line in the rows, or null when it has one (or every
    line of it was rejected, which is not a trade left out)."""
    parsed = func.coalesce(counts.c.parsed, 0)
    return case(
        (parsed > 0, None),
        (Filing.parser_version.is_(None), UnreadReason.PENDING.value),
        (func.coalesce(counts.c.holder, 0) > 0, UnreadReason.UNREADABLE.value),
        (func.coalesce(counts.c.review, 0) > 0, UnreadReason.IN_REVIEW.value),
    )


async def chamber_coverage(
    session: AsyncSession, *, chamber: Chamber | None = None, member_id: int | None = None
) -> list[CongressCoverage]:
    """Per chamber, or ``chamber`` alone: its filings, and what the rows include of them.

    :param member_id: Only that member's filings.
    """
    counts = _filing_counts()
    reason = _unread_reason(counts)
    statement = (
        select(
            Filing.chamber,
            func.count().label("filings"),
            func.count().filter(counts.c.parsed > 0).label("read"),
            func.count()
            .filter(reason.in_([UnreadReason.UNREADABLE.value, UnreadReason.IN_REVIEW.value]))
            .label("unreadable"),
            func.count().filter(Filing.parser_version.is_(None)).label("pending"),
            func.coalesce(func.sum(counts.c.review), 0).cast(Integer).label("review"),
            func.count()
            .filter(CongressFiledName.status.is_distinct_from(MemberMatch.MATCHED.value))
            .label("unmatched"),
            func.coalesce(func.sum(counts.c.resolvable), 0).cast(Integer).label("resolvable"),
            func.coalesce(func.sum(counts.c.resolved), 0).cast(Integer).label("resolved"),
            func.min(Filing.filing_date).label("first"),
            func.max(Filing.filing_date).label("latest"),
            func.max(Filing.parsed_at).label("last_read_at"),
        )
        .select_from(Filing)
        .outerjoin(counts, counts.c.filing_id == Filing.id)
        .outerjoin(CongressFiledName, CongressFiledName.id == Filing.filed_name_id)
        .group_by(Filing.chamber)
    )
    if chamber is not None:
        statement = statement.where(Filing.chamber == chamber.value)
    if member_id is not None:
        statement = statement.where(CongressFiledName.member_id == member_id)
    found = {row.chamber: row for row in await session.execute(statement)}
    chambers = [chamber] if chamber is not None else list(Chamber)
    shown = []
    for each in chambers:
        row = found.get(each.value)
        if row is None:
            shown.append(
                CongressCoverage(
                    chamber=each,
                    filings=0,
                    filings_read=0,
                    filings_unreadable=0,
                    filings_pending=0,
                    lines_in_review=0,
                    filings_unmatched_filer=0,
                )
            )
            continue
        shown.append(
            CongressCoverage(
                chamber=each,
                filings=row.filings,
                filings_read=row.read,
                filings_unreadable=row.unreadable,
                filings_pending=row.pending,
                lines_in_review=row.review,
                filings_unmatched_filer=row.unmatched,
                assets_resolved_pct=_percent(row.resolved, row.resolvable),
                first_filing_date=row.first,
                latest_filing_date=row.latest,
                last_read_at=row.last_read_at,
            )
        )
    return shown


def _whole(dollars: Decimal | None) -> int | None:
    return None if dollars is None else int(dollars)


def _percent(part: int, whole: int) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(100 * part) / whole).quantize(Decimal("0.1"), ROUND_HALF_UP)


async def unread_filings(session: AsyncSession, member_id: int) -> list[UnreadFiling]:
    """The member's filings with no line in the rows, latest first."""
    counts = _filing_counts()
    reason = _unread_reason(counts)
    rows = await session.execute(
        select(
            Filing.chamber,
            Filing.doc_id,
            Filing.filing_date,
            Filing.document_url,
            reason.label("reason"),
        )
        .select_from(Filing)
        .join(CongressFiledName, CongressFiledName.id == Filing.filed_name_id)
        .outerjoin(counts, counts.c.filing_id == Filing.id)
        .where(CongressFiledName.member_id == member_id, reason.is_not(None))
        .order_by(Filing.filing_date.desc(), Filing.doc_id.desc())
        .limit(MAX_UNREAD)
    )
    return [
        UnreadFiling(
            chamber=Chamber(row.chamber),
            doc_id=row.doc_id,
            filing_date=row.filing_date,
            document_url=row.document_url,
            reason=UnreadReason(row.reason),
        )
        for row in rows
    ]


# --- parameters -----------------------------------------------------------------


def _names(value: object) -> list[str]:
    """A repeatable, comma-separated query parameter, as FastAPI hands it over."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("a comma-separated list")
    return [name.strip() for item in value for name in item.split(",") if name.strip()]


def _bioguide_ids(value: object) -> frozenset[str]:
    """``?member=P000197,K000389``, or repeated: Bioguide ids, any case."""
    if isinstance(value, frozenset):
        return value
    names = [name.upper() for name in _names(value)]
    if bad := [name for name in names if not _is_bioguide(name)]:
        raise ValueError(
            f"not a Bioguide id: {', '.join(map(repr, bad))}; one is a letter and six "
            "digits, `P000197`"
        )
    if not names:
        raise ValueError("no member given")
    return frozenset(names)


def _is_bioguide(name: str) -> bool:
    return len(name) == 7 and name[0].isascii() and name[0].isalpha() and name[1:].isdigit()


def _parties(value: object) -> frozenset[str]:
    """``?party=democrat,republican``: as the Bioguide spells a party, any case."""
    if isinstance(value, frozenset):
        return value
    names = [name.lower() for name in _names(value)]
    if not names:
        raise ValueError("no party given")
    return frozenset(names)


def _types(value: object) -> frozenset[TradeType]:
    """``?type=purchase,sale``; ``sale`` takes partial sales with it."""
    if isinstance(value, frozenset):
        return value
    names = _names(value)
    known = {kind.value for kind in TradeType}
    if unknown := [name for name in names if name not in known]:
        raise ValueError(
            f"unknown type {', '.join(map(repr, unknown))}: choose from "
            f"{', '.join(kind.value for kind in TradeType)}"
        )
    if not names:
        raise ValueError("no type given")
    return frozenset(stored for name in names for stored in TYPES[TradeType(name)])


def _asset_classes(value: object) -> frozenset[AssetClass]:
    """``?asset_class=equity,stock_option``."""
    if isinstance(value, frozenset):
        return value
    names = _names(value)
    known = {kind.value for kind in AssetClass}
    if unknown := [name for name in names if name not in known]:
        raise ValueError(
            f"unknown asset class {', '.join(map(repr, unknown))}: choose from "
            f"{', '.join(kind.value for kind in AssetClass)}"
        )
    if not names:
        raise ValueError("no asset class given")
    return frozenset(AssetClass(name) for name in names)


MembersParam = Annotated[
    frozenset[str] | None,
    PlainValidator(_bioguide_ids),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["P000197"]]}),
    Query(
        alias="member",
        description=(
            "Only these members, by Bioguide id, comma-separated or repeated. A filer "
            "not known to be a member yet is under none."
        ),
    ),
]
ChamberParam = Annotated[
    Chamber | None, Query(description="Only filings to this chamber: `house` or `senate`.")
]
PartiesParam = Annotated[
    frozenset[str] | None,
    PlainValidator(_parties),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["democrat"]]}),
    Query(
        alias="party",
        description=(
            "Only members of these parties on the trade day: `democrat`, `republican`, "
            "`independent`. Comma-separated or repeated. Leaves out filers not known "
            "to be a member."
        ),
    ),
]
TypesParam = Annotated[
    frozenset[TradeType] | None,
    PlainValidator(_types),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["purchase"]]}),
    Query(
        alias="type",
        description=(
            "Only these kinds of trade: `purchase`, `sale`, `partial_sale`, `exchange`. "
            "`sale` includes partial sales; `partial_sale` is those alone."
        ),
    ),
]
AssetClassesParam = Annotated[
    frozenset[AssetClass] | None,
    PlainValidator(_asset_classes),
    WithJsonSchema({"type": "array", "items": {"type": "string"}, "examples": [["equity"]]}),
    Query(
        alias="asset_class",
        description=(
            "Only these kinds of asset: `equity`, `stock_option`, `municipal_bond`, "
            "`treasury`, `mutual_fund`, `crypto`… A line not resolved yet is under none."
        ),
    ),
]
StartParam = Annotated[
    date | None, Query(alias="from", description="The first trade day to include, `2026-06-01`.")
]
EndParam = Annotated[
    date | None, Query(alias="to", description="The last trade day to include, `2026-06-30`.")
]
DisclosedFromParam = Annotated[
    date | None,
    Query(
        description=(
            "The first filing day to include. Unlike `from`, which is the trade's: "
            "this week's disclosures of last quarter's trades are in."
        )
    ),
]
DisclosedToParam = Annotated[
    date | None, Query(description="The last filing day to include, `2026-06-30`.")
]
MinAmountParam = Annotated[
    int | None,
    Query(
        ge=0,
        description=(
            "Only lines worth at least this many dollars for certain: whose band's "
            "lower end (or figure) is at least it. `50001` is the $50,001 - $100,000 "
            "band and every one above."
        ),
    ),
]
LateParam = Annotated[
    bool | None,
    Query(
        description=(
            "`true`: only lines filed after the STOCK Act deadline; `false`: only those "
            "filed in time. Either leaves out a line whose lag is not known."
        )
    ),
]
SortParam = Annotated[
    CongressSort,
    Query(
        description=(
            "`disclosed`, the filing day (the default); `traded`, the trade day; "
            "`amount`, the band's lower end; `lag`, `disclosure_lag_days`, with the "
            "lines whose lag is not known last."
        )
    ),
]
OrderParam = Annotated[SortOrder, Query(description="Latest, largest or longest first by default.")]
FeedTickerParam = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=32,
        description=(
            "One stock: a ticker, a former ticker, or a CUSIP, as `/v1/stocks/{ticker}` "
            "reads it. Its lines and options on it; a line whose asset no lookup "
            "found is under no stock."
        ),
    ),
]
BioguideParam = Annotated[
    str,
    Path(
        min_length=7,
        max_length=7,
        description="The member's Bioguide id, any case.",
        examples=["P000197"],
    ),
]

_STOCK_NOT_FOUND = not_found(
    "Nothing by that ticker, alias or CUSIP. Suggests what may have been meant.",
    StockNotFound,
)
_MEMBER_NOT_FOUND = not_found("No member of Congress since 2008 has that Bioguide id.")


def _filters(
    *,
    start: date | None,
    end: date | None,
    disclosed_start: date | None,
    disclosed_end: date | None,
    types: frozenset[TradeType] | None,
    asset_classes: frozenset[AssetClass] | None,
    min_amount: int | None,
    late: bool | None,
    bioguide_ids: frozenset[str] | None = None,
    chamber: Chamber | None = None,
    parties: frozenset[str] | None = None,
) -> Filters:
    if start is not None and end is not None and start > end:
        raise invalid("from", f"from ({start}) is after to ({end}).")
    if (
        disclosed_start is not None
        and disclosed_end is not None
        and disclosed_start > disclosed_end
    ):
        raise invalid(
            "disclosed_from",
            f"disclosed_from ({disclosed_start}) is after disclosed_to ({disclosed_end}).",
        )
    return Filters(
        bioguide_ids=bioguide_ids or frozenset(),
        chamber=chamber,
        parties=parties or frozenset(),
        types=types or frozenset(),
        asset_classes=asset_classes or frozenset(),
        start=start,
        end=end,
        disclosed_start=disclosed_start,
        disclosed_end=disclosed_end,
        min_amount=min_amount,
        late=late,
    )


async def _rows(
    session: AsyncSession,
    page: PageParams,
    filters: Filters,
    *,
    security_id: int | None = None,
    member_id: int | None = None,
    sort: CongressSort,
    order: SortOrder,
    listing: str,
) -> tuple[list[CongressTrade], Any, CongressSummary]:
    statement, keyset = transactions_query(
        page,
        filters,
        security_id=security_id,
        member_id=member_id,
        sort=sort,
        order=order,
        listing=listing,
    )
    rows, page_info = page_of(list(await session.execute(statement)), keyset, page)
    summary = await summarise(session, filters, security_id=security_id, member_id=member_id)
    return [_trade(row) for row in rows], page_info, summary


# --- the routes -----------------------------------------------------------------


@router.get(
    "/congress/transactions",
    operation_id="listCongressTransactions",
    response_model=CongressTradeEnvelope,
    summary="Members of Congress's trades, as their PTRs disclose them",
    responses={**_STOCK_NOT_FOUND, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.CONGRESS)
async def list_congress_transactions(
    session: SessionDep,
    page: PageParamsDep,
    member: MembersParam = None,
    chamber: ChamberParam = None,
    party: PartiesParam = None,
    ticker: FeedTickerParam = None,
    type_: TypesParam = None,
    asset_class: AssetClassesParam = None,
    start: StartParam = None,
    end: EndParam = None,
    disclosed_from: DisclosedFromParam = None,
    disclosed_to: DisclosedToParam = None,
    min_amount: MinAmountParam = None,
    late: LateParam = None,
    sort: SortParam = CongressSort.DISCLOSED,
    order: OrderParam = SortOrder.DESC,
) -> CongressTradeEnvelope:
    """PTR lines, latest disclosed first, one row per line as filed, each with how
    long after the trade it was disclosed and whether that was late. Amounts are
    the form's bands. `meta.summary` adds up every chosen line, in ranges;
    `meta.congress_coverage` says how much of each chamber's filings the rows can
    include."""
    filters = _filters(
        start=start,
        end=end,
        disclosed_start=disclosed_from,
        disclosed_end=disclosed_to,
        types=type_,
        asset_classes=asset_class,
        min_amount=min_amount,
        late=late,
        bioguide_ids=member,
        chamber=chamber,
        parties=party,
    )
    security_id = (await _resolve(session, ticker)).id if ticker is not None else None
    data, page_info, summary = await _rows(
        session,
        page,
        filters,
        security_id=security_id,
        sort=sort,
        order=order,
        listing="congress" if security_id is None else "congress.stock",
    )
    return CongressTradeEnvelope(
        data=data,
        meta=CongressFeedMeta(
            summary=summary,
            congress_coverage=await chamber_coverage(session, chamber=chamber),
            generated_at=datetime.now(UTC),
        ),
        page=page_info,
    )


@router.get(
    "/congress/members/{bioguide}",
    operation_id="getCongressMember",
    response_model=CongressMemberEnvelope,
    summary="One member of Congress's disclosed trades",
    responses={**_MEMBER_NOT_FOUND, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.CONGRESS)
async def read_congress_member(
    bioguide: BioguideParam,
    session: SessionDep,
    page: PageParamsDep,
    ticker: FeedTickerParam = None,
    type_: TypesParam = None,
    asset_class: AssetClassesParam = None,
    start: StartParam = None,
    end: EndParam = None,
    disclosed_from: DisclosedFromParam = None,
    disclosed_to: DisclosedToParam = None,
    min_amount: MinAmountParam = None,
    late: LateParam = None,
    sort: SortParam = CongressSort.DISCLOSED,
    order: OrderParam = SortOrder.DESC,
) -> CongressMemberEnvelope:
    """The member's PTR lines, filtered as `/v1/congress/transactions` is, with who
    they are (`meta.member`), every line of theirs added up in ranges
    (`meta.summary`), and their filings none of whose lines could be read
    (`meta.unread_filings`), with the link to each original."""
    filters = _filters(
        start=start,
        end=end,
        disclosed_start=disclosed_from,
        disclosed_end=disclosed_to,
        types=type_,
        asset_classes=asset_class,
        min_amount=min_amount,
        late=late,
    )
    found = await session.scalar(select(Member).where(Member.bioguide_id == bioguide.upper()))
    if found is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            f"No member of Congress since 2008 is {bioguide!r}.",
        )
    security_id = (await _resolve(session, ticker)).id if ticker is not None else None
    data, page_info, summary = await _rows(
        session,
        page,
        filters,
        security_id=security_id,
        member_id=found.id,
        sort=sort,
        order=order,
        listing="congress-member" if security_id is None else "congress-member.stock",
    )
    terms = await session.scalars(
        select(CongressMemberTerm)
        .where(CongressMemberTerm.member_id == found.id)
        .order_by(CongressMemberTerm.start_date.desc())
    )
    return CongressMemberEnvelope(
        data=data,
        meta=CongressMemberMeta(
            member=CongressMember(
                bioguide_id=found.bioguide_id,
                name=found.full_name,
                first_name=found.first_name,
                last_name=found.last_name,
                chamber=Chamber(found.chamber),
                state=found.state,
                district=found.district,
                party=found.party,
                in_office=found.current,
                terms=[
                    CongressTerm(
                        chamber=Chamber(term.chamber),
                        start_date=term.start_date,
                        end_date=term.end_date,
                        state=term.state,
                        district=term.district,
                        party=term.party,
                    )
                    for term in terms
                ],
            ),
            summary=summary,
            unread_filings=await unread_filings(session, found.id),
            congress_coverage=await chamber_coverage(session, member_id=found.id),
            generated_at=datetime.now(UTC),
        ),
        page=page_info,
    )


@router.get(
    "/stocks/{ticker}/congress",
    operation_id="getStockCongress",
    response_model=CongressTradeEnvelope,
    summary="One stock's trades by members of Congress, and options on it",
    responses={**_STOCK_NOT_FOUND, **INVALID_CURSOR, **INVALID},
)
@cached(Lifetime.CONGRESS)
async def read_stock_congress(
    ticker: TickerParam,
    session: SessionDep,
    page: PageParamsDep,
    member: MembersParam = None,
    chamber: ChamberParam = None,
    party: PartiesParam = None,
    type_: TypesParam = None,
    asset_class: AssetClassesParam = None,
    start: StartParam = None,
    end: EndParam = None,
    disclosed_from: DisclosedFromParam = None,
    disclosed_to: DisclosedToParam = None,
    min_amount: MinAmountParam = None,
    late: LateParam = None,
    sort: SortParam = CongressSort.DISCLOSED,
    order: OrderParam = SortOrder.DESC,
) -> CongressTradeEnvelope:
    """The PTR lines resolved to the stock — its shares, and options on it, which
    `asset_class` tells apart — filtered as `/v1/congress/transactions` is, with
    every one added up in ranges. A line whose asset no lookup found is not
    here: `meta.congress_coverage` says how many of the chamber's are."""
    filters = _filters(
        start=start,
        end=end,
        disclosed_start=disclosed_from,
        disclosed_end=disclosed_to,
        types=type_,
        asset_classes=asset_class,
        min_amount=min_amount,
        late=late,
        bioguide_ids=member,
        chamber=chamber,
        parties=party,
    )
    security = await _resolve(session, ticker)
    data, page_info, summary = await _rows(
        session,
        page,
        filters,
        security_id=security.id,
        sort=sort,
        order=order,
        listing="stock-congress",
    )
    return CongressTradeEnvelope(
        data=data,
        meta=CongressFeedMeta(
            summary=summary,
            congress_coverage=await chamber_coverage(session, chamber=chamber),
            generated_at=datetime.now(UTC),
        ),
        page=page_info,
    )
