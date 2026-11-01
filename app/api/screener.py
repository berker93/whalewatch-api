"""The screener's vocabulary: a bounded set of named filters, each one known SQL fragment.

Not an expression language. A client names filters from :data:`VOCABULARY`
and gives each a value; every one becomes the one ``WHERE`` condition its
:meth:`Filter.to_sql` writes, and the conditions are ANDed. There is no OR
between filters, no nesting and no parentheses, so the queries that can be
asked are the ones in this file, and every one of them has been planned
(``make explain a="--only screener screener_filtered"``). A key not in the
vocabulary is a 422, never ignored: a misspelt ``min_wieght`` would otherwise
answer a broader question than the one asked, and nothing would say so.

What a row is
-------------
One filer's position in one stock in one period: a row of ``position_change``.
Each filter is about the position (``action``, ``min_value``, ``min_weight``,
``weight_change``, ``is_new_position``, ``added_quarters``), its filer
(``filer_category``, ``filer_slug``) or its stock (``min_holders``,
``net_flow``, ``min_exits``, ``insider_cluster``, ``insider_buyers``,
``sector``), and all of them hold for the row. So
"activists opening a position over 5% of their book in a stock 20 funds hold"
is one row per activist that did, and two position filters are about the same
position, never one each.

A position sold out of entirely is a row only under ``action=exit``: without
an ``action``, the rows are what the filers hold at the period end.

The period
----------
Required on every query, and the first condition of every one: it is the most
selective predicate (a period is about 1/20 of ``position_change``), and the
stock filters read the views at it. Every other condition narrows the
period's rows, or probes a view's unique index, which leads with the period.

Changes need a previous period
------------------------------
``action``, ``weight_change``, ``is_new_position`` and ``added_quarters``
describe a change, and a filer's first period has none: every position in it is ``new`` only because
it is the first we have. Those rows never pass a change filter, as the flows
leave them out (:mod:`app.derived.views`).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    ARRAY,
    BindParameter,
    ColumnElement,
    CompoundSelect,
    Date,
    Numeric,
    Select,
    Text,
    and_,
    any_,
    case,
    cast,
    distinct,
    exists,
    func,
    literal,
    select,
    true,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.estimates import estimate_columns
from app.api.pagination import Keyset, PageParams, SortKey, page_statement
from app.api.routers.market import Direction
from app.api.routers.portfolio import SortOrder
from app.core.periods import parse_period, quarters_ending
from app.db.models import Filer, FilerCategory, InsiderCluster, SecCompanyTicker, Security
from app.db.models.position_change import ChangeAction, PositionChange
from app.db.queries.returns import estimated_return
from app.db.queries.tickers import ticker_labels
from app.derived.insider_cluster import DEFAULT_MIN_INSIDERS, DEFAULT_WINDOW, counted_purchases
from app.derived.views import CONSENSUS_HOLDINGS, QUARTER_FLOWS, traded_usd

#: What a screen's queries may take, at most, before Postgres cancels them.
#: A backstop: every query the vocabulary can write is planned well under it.
STATEMENT_TIMEOUT_SECONDS: Final = 5

#: The most investors ``filer_slug`` names at once.
MAX_SLUGS: Final = 50
#: Beyond any real count or dollar figure, and inside what Postgres binds: a
#: threshold past them is a 422, not a bigint overflow's 500.
MAX_COUNT: Final = 1_000_000
MAX_DOLLARS: Final = Decimal(10) ** 15
#: The longest run of adds ``added_quarters`` asks for: two years. Each
#: quarter of it is one more probe of ``position_change`` per row.
MAX_ADDED_QUARTERS: Final = 8

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)


class FilterType(StrEnum):
    """What a filter's value is, which says how a client builds its control."""

    PERIOD = "period"
    """A quarter: ``2026Q1`` or ``2026-03-31``."""
    CHOICE = "choice"
    """One of ``options``."""
    CHOICES = "choices"
    """Any of ``options``, comma-separated or repeated."""
    INTEGER = "integer"
    DOLLARS = "dollars"
    """A decimal number of dollars."""
    PERCENT = "percent"
    """A decimal percentage, 0 to 100."""
    POINTS = "points"
    """Percentage points, signed: the direction is the sign."""
    BOOLEAN = "boolean"
    """``true`` or ``false``."""
    SLUGS = "slugs"
    """Investors' slugs, comma-separated or repeated."""


#: The types that take several values.
_MANY: Final = frozenset({FilterType.CHOICES, FilterType.SLUGS})


#: A period: a value, or a bind parameter left for ``make explain`` to fill in.
PeriodEnd = date | BindParameter[date]


@dataclass(frozen=True, slots=True)
class Scope:
    """What every fragment is written against, besides its own value."""

    period: PeriodEnd
    insiders_from: date | None = None
    """The caller's window for Form 4 trades: no insider cluster begun before it."""


def quarter_start(period: date) -> date:
    """The first day of ``period``'s quarter."""
    return quarters_ending(period, 2)[0] + timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Filter:
    """One named filter: what a client calls it, what it means, and the SQL it is."""

    key: str
    """The query parameter."""
    label: str
    """What a person calls it, for the control's label."""
    type: FilterType
    description: str
    source: str
    """The column it reads, for whoever is reading this file."""
    parser: Callable[[str], Any]
    """One value, as sent, to what :attr:`fragment` takes: ValueError for one it cannot."""
    fragment: Callable[[Any, Scope], ColumnElement[bool]]
    options: tuple[str, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    unavailable: str | None = None
    """Why a filter in the vocabulary has no data behind it yet: asking for it
    is a 422 saying so. None for every filter that works."""
    required: bool = False
    insiders: bool = False
    """Whether it reads Form 4 trades, so the quarter must be inside the
    caller's ``insiders_from``."""

    @property
    def available(self) -> bool:
        return self.unavailable is None

    @property
    def many(self) -> bool:
        return self.type in _MANY

    def parse(self, raw: Sequence[str]) -> Any:
        """The value of the parameter as FastAPI collected it, every repeat.

        :raises ValueError: With the sentence a client is shown.
        """
        if self.unavailable is not None:
            raise ValueError(f"{self.key} is not available yet: {self.unavailable}")
        if not self.many:
            if len(raw) != 1:
                raise ValueError(f"give {self.key} once")
            return self.parser(raw[0].strip())
        values = [part.strip() for item in raw for part in item.split(",") if part.strip()]
        if not values:
            raise ValueError(f"no value given for {self.key}")
        if len(values) > MAX_SLUGS:
            raise ValueError(f"at most {MAX_SLUGS} values for {self.key}")
        return frozenset(self.parser(value) for value in values)

    def to_sql(self, value: Any, scope: Scope) -> ColumnElement[bool]:
        """The condition on ``position_change`` that ``value`` is."""
        return self.fragment(value, scope)

    def json_schema(self) -> dict[str, Any]:
        """The parameter's schema, for the OpenAPI document."""
        schemas: dict[FilterType, dict[str, Any]] = {
            FilterType.PERIOD: {"type": "string", "examples": ["2026Q2"]},
            FilterType.CHOICE: {"type": "string", "enum": list(self.options)},
            FilterType.CHOICES: {"type": "string", "enum": list(self.options)},
            FilterType.INTEGER: {"type": "integer"},
            FilterType.DOLLARS: {"type": "string", "format": "decimal"},
            FilterType.PERCENT: {"type": "string", "format": "decimal"},
            FilterType.POINTS: {"type": "string", "format": "decimal"},
            FilterType.BOOLEAN: {"type": "boolean"},
            FilterType.SLUGS: {"type": "string"},
        }
        one = schemas[self.type]
        if self.many:
            return {"type": "array", "items": one, "maxItems": MAX_SLUGS}
        return one


# --- parsing --------------------------------------------------------------------


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a number") from None
    # NaN and Infinity parse, and are no threshold.
    if not value.is_finite():
        raise ValueError(f"{text!r} is not a number")
    return value


def _at_least(minimum: Decimal, maximum: Decimal | None = None) -> Callable[[str], Decimal]:
    def parse(text: str) -> Decimal:
        value = _decimal(text)
        if value < minimum or (maximum is not None and value > maximum):
            bounds = f"at least {minimum}" if maximum is None else f"{minimum} to {maximum}"
            raise ValueError(f"{text} is out of range: {bounds}")
        return value

    return parse


def _whole(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"{text!r} is not a whole number")
        value = int(text)
        if not minimum <= value <= maximum:
            raise ValueError(f"{text} is out of range: {minimum} to {maximum}")
        return value

    return parse


_count: Final = _whole(1, MAX_COUNT)


def _points(text: str) -> Decimal:
    value = _decimal(text)
    if value == 0:
        raise ValueError("a move of 0 points is every position: give a positive or negative one")
    if abs(value) > 100:
        raise ValueError(f"{text} is out of range: -100 to 100")
    return value


def _boolean(text: str) -> bool:
    if text not in ("true", "false"):
        raise ValueError(f"{text!r} is not true or false")
    return text == "true"


def _one_of(options: Sequence[str], what: str) -> Callable[[str], str]:
    def parse(text: str) -> str:
        if text not in options:
            raise ValueError(f"unknown {what} {text!r}: choose from {', '.join(options)}")
        return text

    return parse


def _slug(text: str) -> str:
    # Slugs are minted lowercase, letters, digits and hyphens: anything else
    # names no investor, and is refused here rather than looked up.
    if not text or len(text) > 200 or not all(c.isalnum() or c == "-" for c in text):
        raise ValueError(f"{text!r} is not an investor's slug")
    return text.lower()


# --- the fragments --------------------------------------------------------------

_change = PositionChange
#: A change has a previous period to be against: not a filer's first.
_A_CHANGE: Final = _change.prev_period_of_report.is_not(None)


def _period(value: PeriodEnd, scope: Scope) -> ColumnElement[bool]:
    return _change.period_of_report == value


def _any_of(values: frozenset[str]) -> Any:
    """``= ANY(:array)``'s array: one parameter rather than IN's one per value,
    as the activity's actions are, so every set of values is one statement."""
    return any_(cast(literal(sorted(values)), ARRAY(Text)))


def _actions(value: frozenset[str], scope: Scope) -> ColumnElement[bool]:
    return and_(_change.action == _any_of(value), _A_CHANGE)


def _min_holders(value: int, scope: Scope) -> ColumnElement[bool]:
    consensus = CONSENSUS_HOLDINGS
    return _change.security_id.in_(
        select(consensus.c.security_id).where(
            consensus.c.period_of_report == scope.period, consensus.c.holder_count >= value
        )
    )


def _min_value(value: Decimal, scope: Scope) -> ColumnElement[bool]:
    return _change.value_usd >= value


def _min_weight(value: Decimal, scope: Scope) -> ColumnElement[bool]:
    return _change.weight_pct >= value


def _weight_change(value: Decimal, scope: Scope) -> ColumnElement[bool]:
    moved = _change.weight_delta >= value if value > 0 else _change.weight_delta <= value
    return and_(moved, _A_CHANGE)


def _filer_ids(condition: ColumnElement[bool]) -> ColumnElement[bool]:
    return _change.filer_id.in_(select(Filer.id).where(condition))


def _filer_category(value: frozenset[str], scope: Scope) -> ColumnElement[bool]:
    return _filer_ids(Filer.category == _any_of(value))


def _filer_slug(value: frozenset[str], scope: Scope) -> ColumnElement[bool]:
    return _filer_ids(Filer.slug == _any_of(value))


def _net_flow(value: str, scope: Scope) -> ColumnElement[bool]:
    flows = QUARTER_FLOWS
    net = flows.c.net_value_usd
    return _change.security_id.in_(
        select(flows.c.security_id).where(
            flows.c.period_of_report == scope.period,
            net > 0 if value == Direction.BUY.value else net < 0,
        )
    )


def _new_position(value: bool, scope: Scope) -> ColumnElement[bool]:
    new = ChangeAction.NEW.value
    return and_(_change.action == new if value else _change.action != new, _A_CHANGE)


def _added_quarters(value: int, scope: Scope) -> ColumnElement[bool]:
    """An ``add`` this period, and in each of the ``value - 1`` before it, one
    probe of ``position_change``'s key each, back through ``prev_period_of_report``."""
    add = ChangeAction.ADD.value
    later: Any = _change
    chain: list[ColumnElement[bool]] = []
    for n in range(1, value):
        earlier = aliased(PositionChange, name=f"added_{n}")
        chain += [
            earlier.filer_id == later.filer_id,
            earlier.period_of_report == later.prev_period_of_report,
            earlier.security_id == later.security_id,
            earlier.action == add,
        ]
        later = earlier
    # An add has previous shares, so a previous period: no first period passes.
    return and_(_change.action == add, exists().where(*chain))


def _min_exits(value: int, scope: Scope) -> ColumnElement[bool]:
    flows = QUARTER_FLOWS
    return _change.security_id.in_(
        select(flows.c.security_id).where(
            flows.c.period_of_report == scope.period, flows.c.exits >= value
        )
    )


def clustered_securities(scope: Scope) -> CompoundSelect[Any]:
    """Every security with an insider cluster episode in the period's quarter.

    The default grid point of ``/v1/insiders/clusters`` (30 days, 3 insiders),
    any episode overlapping the quarter. An episode is the issuer's, not one
    security's (:mod:`app.derived.insider_cluster`): it is each security linked
    to it, and each security whose current ticker SEC lists under the
    issuer's CIK, as ``/v1/insiders/clusters?ticker=`` finds them.
    """
    cluster = InsiderCluster
    episodes = [
        cluster.window_days == DEFAULT_WINDOW,
        cluster.min_insiders == DEFAULT_MIN_INSIDERS,
        # In SQL rather than quarter_start(), so a bound period plans the same.
        cluster.window_end >= cast(func.date_trunc("quarter", scope.period), Date),
        cluster.window_start <= scope.period,
    ]
    if scope.insiders_from is not None:
        episodes.append(cluster.window_start >= scope.insiders_from)
    linked = select(cluster.security_id).where(*episodes, cluster.security_id.is_not(None))
    by_issuer = (
        select(Security.id)
        .join(SecCompanyTicker, SecCompanyTicker.ticker == func.replace(Security.ticker, ".", "-"))
        .join(cluster, cluster.issuer_cik == SecCompanyTicker.cik)
        .where(*episodes)
    )
    return union(linked, by_issuer)


def _insider_cluster(value: bool, scope: Scope) -> ColumnElement[bool]:
    clustered = clustered_securities(scope)
    return _change.security_id.in_(clustered) if value else _change.security_id.not_in(clustered)


def insider_bought_securities(minimum: int, scope: Scope) -> CompoundSelect[Any]:
    """Every security whose company had at least ``minimum`` insiders buy in the
    period's quarter, by the lines a cluster counts (:func:`counted_purchases`).

    Distinct insiders, each once however often they bought, within the
    quarter, and from ``insiders_from`` on. Per issuer, as a cluster is: each
    security a counted line is linked to, and each whose current ticker SEC
    lists under the issuer's CIK.
    """
    line = counted_purchases()
    in_quarter = [
        line.c.transaction_date >= cast(func.date_trunc("quarter", scope.period), Date),
        line.c.transaction_date <= scope.period,
    ]
    if scope.insiders_from is not None:
        in_quarter.append(line.c.transaction_date >= scope.insiders_from)
    issuers = (
        select(line.c.issuer_cik)
        .where(*in_quarter)
        .group_by(line.c.issuer_cik)
        .having(func.count(distinct(line.c.insider_id)) >= minimum)
    )
    linked = select(line.c.security_id).where(
        *in_quarter, line.c.security_id.is_not(None), line.c.issuer_cik.in_(issuers)
    )
    by_issuer = (
        select(Security.id)
        .join(SecCompanyTicker, SecCompanyTicker.ticker == func.replace(Security.ticker, ".", "-"))
        .where(SecCompanyTicker.cik.in_(issuers))
    )
    return union(linked, by_issuer)


def _insider_buyers(value: int, scope: Scope) -> ColumnElement[bool]:
    return _change.security_id.in_(insider_bought_securities(value, scope))


#: The filters, in the order a client lists them. Nothing beyond these.
VOCABULARY: Final[tuple[Filter, ...]] = (
    Filter(
        key="period",
        label="Quarter",
        type=FilterType.PERIOD,
        description="The quarter, as `2026Q1` or `2026-03-31`. Required.",
        source="position_change.period_of_report",
        parser=parse_period,
        fragment=_period,
        required=True,
    ),
    Filter(
        key="action",
        label="What happened",
        type=FilterType.CHOICES,
        description=(
            "Any of `new`, `add`, `trim`, `hold`, `exit`, against the investor's previous "
            "published period. Without it, the positions held at the period end: an exit "
            "is a row only when `exit` is asked for."
        ),
        source="position_change.action",
        parser=_one_of([a.value for a in ChangeAction], "action"),
        fragment=_actions,
        options=tuple(a.value for a in ChangeAction),
    ),
    Filter(
        key="min_holders",
        label="How many investors",
        type=FilterType.INTEGER,
        description="Only stocks held by at least this many investors at the period end.",
        source="mv_consensus_holdings.holder_count",
        parser=_count,
        fragment=_min_holders,
        minimum=Decimal(1),
        maximum=Decimal(MAX_COUNT),
    ),
    Filter(
        key="min_value",
        label="Position size",
        type=FilterType.DOLLARS,
        description="Only positions worth at least this many dollars at the period end.",
        source="position_snapshot.value_usd",
        parser=_at_least(Decimal(0), MAX_DOLLARS),
        fragment=_min_value,
        minimum=Decimal(0),
        maximum=MAX_DOLLARS,
    ),
    Filter(
        key="min_weight",
        label="Share of their portfolio",
        type=FilterType.PERCENT,
        description=(
            "Only positions at least this percentage of the investor's common-stock "
            "portfolio, 0 to 100."
        ),
        source="position_snapshot.weight_pct",
        parser=_at_least(Decimal(0), Decimal(100)),
        fragment=_min_weight,
        minimum=Decimal(0),
        maximum=Decimal(100),
    ),
    Filter(
        key="weight_change",
        label="How much the stake moved",
        type=FilterType.POINTS,
        description=(
            "Percentage points of the portfolio since the previous period, signed: `2` is "
            "up by 2 points or more, `-2` down by 2 or more. Moves with the price, not "
            "only with trading."
        ),
        source="position_change.weight_delta",
        parser=_points,
        fragment=_weight_change,
        minimum=Decimal(-100),
        maximum=Decimal(100),
    ),
    Filter(
        key="sector",
        label="Sector",
        type=FilterType.CHOICES,
        description=(
            "No source of sectors is loaded, so no stock has one, and asking for one is "
            "a 422 rather than an empty page."
        ),
        source="security.sector",
        # Nothing to parse or write until there is a column to read.
        parser=str,
        fragment=lambda value, scope: true(),
        unavailable="no source of sectors is loaded, so no stock has one. Leave it out.",
    ),
    Filter(
        key="filer_category",
        label="Kind of investor",
        type=FilterType.CHOICES,
        description=(
            "Any of the investment styles investors are filed under. An investor with "
            "none is in no category."
        ),
        source="filer.category",
        parser=_one_of([c.value for c in FilerCategory], "category"),
        fragment=_filer_category,
        options=tuple(c.value for c in FilerCategory),
    ),
    Filter(
        key="filer_slug",
        label="Specific investors",
        type=FilterType.SLUGS,
        description=(
            f"Any of these investors, by slug, at most {MAX_SLUGS}. A slug that names no "
            "investor is a 422."
        ),
        source="filer.slug",
        parser=_slug,
        fragment=_filer_slug,
    ),
    Filter(
        key="net_flow",
        label="Bought or sold overall",
        type=FilterType.CHOICE,
        description=(
            "`buy`: stocks the investors bought more of than they sold in the quarter, "
            "in traded dollars. `sell`: sold more than bought."
        ),
        source="mv_quarter_flows.net_value_usd",
        parser=_one_of([d.value for d in Direction], "direction"),
        fragment=_net_flow,
        options=tuple(d.value for d in Direction),
    ),
    Filter(
        key="min_exits",
        label="Investors who sold out",
        type=FilterType.INTEGER,
        description=(
            "Only stocks at least this many investors sold their whole position in, in the quarter."
        ),
        source="mv_quarter_flows.exits",
        parser=_count,
        fragment=_min_exits,
        minimum=Decimal(1),
        maximum=Decimal(MAX_COUNT),
    ),
    Filter(
        key="is_new_position",
        label="First time holding it",
        type=FilterType.BOOLEAN,
        description=(
            "`true`: opened this quarter, not held in the investor's previous published "
            "period (one sold out of and bought back is new again). `false`: held then "
            "too. Never a position in an investor's first period, either way."
        ),
        source="position_change.action = 'new'",
        parser=_boolean,
        fragment=_new_position,
    ),
    Filter(
        key="added_quarters",
        label="Added to, quarters running",
        type=FilterType.INTEGER,
        description=(
            f"Positions the investor added to this quarter and in each of its previous "
            f"published periods before it, at least this many in a row, 2 to "
            f"{MAX_ADDED_QUARTERS}. An add is more shares, so a stock that only rose is "
            f"not one, nor is a split; opening a position is `new`, not an add."
        ),
        source="position_change.action = 'add', back through prev_period_of_report",
        parser=_whole(2, MAX_ADDED_QUARTERS),
        fragment=_added_quarters,
        minimum=Decimal(2),
        maximum=Decimal(MAX_ADDED_QUARTERS),
    ),
    Filter(
        key="insider_cluster",
        label="Insiders buying too",
        type=FilterType.BOOLEAN,
        description=(
            f"`true`: stocks whose company had a cluster buy in the quarter: at least "
            f"{DEFAULT_MIN_INSIDERS} officers and directors buying on the open market "
            f"within {DEFAULT_WINDOW} days (`GET /v1/insiders/clusters`). `false`: none."
        ),
        source="insider_cluster, by security or by the issuer's CIK",
        parser=_boolean,
        fragment=_insider_cluster,
        insiders=True,
    ),
    Filter(
        key="insider_buyers",
        label="Insiders who bought",
        type=FilterType.INTEGER,
        description=(
            "Only stocks whose company had at least this many of its officers and "
            "directors buy shares on the open market in the quarter, by the rules a "
            "cluster counts purchases by: no 10b5-1 plan, employee or dividend plan "
            "purchases. `1` is any such purchase."
        ),
        source="insider_transaction, by security or by the issuer's CIK",
        parser=_count,
        fragment=_insider_buyers,
        minimum=Decimal(1),
        maximum=Decimal(MAX_COUNT),
        insiders=True,
    ),
)

FILTERS: Final = {f.key: f for f in VOCABULARY}
assert len(FILTERS) == len(VOCABULARY), "two filters with one key"


@dataclass(frozen=True, slots=True)
class Applied:
    """One filter and the value it was given."""

    key: str
    value: Any

    @property
    def filter(self) -> Filter:
        return FILTERS[self.key]

    def display(self) -> list[str]:
        """The value as a client would send it, one string per value."""
        values = sorted(self.value) if isinstance(self.value, frozenset) else [self.value]
        return [str(v).lower() if isinstance(v, bool) else str(v) for v in values]


@dataclass(frozen=True, slots=True)
class Screen:
    """Every filter of one request but the period, in vocabulary order."""

    applied: tuple[Applied, ...] = field(default=())

    def value(self, key: str) -> Any:
        return next((a.value for a in self.applied if a.key == key), None)

    def conditions(self, scope: Scope) -> list[ColumnElement[bool]]:
        """The ``WHERE``: the period's, each filter's, and, without an
        ``action``, no exits."""
        conditions = [FILTERS["period"].to_sql(scope.period, scope)]
        conditions += [a.filter.to_sql(a.value, scope) for a in self.applied]
        if self.value("action") is None:
            conditions.append(_change.action != ChangeAction.EXIT.value)
        return conditions


async def unknown_slugs(session: AsyncSession, slugs: frozenset[str]) -> list[str]:
    """The slugs that name no investor, sorted."""
    known = set(await session.scalars(select(Filer.slug).where(Filer.slug.in_(sorted(slugs)))))
    return sorted(slugs - known)


# --- the query ------------------------------------------------------------------


class ScreenSort(StrEnum):
    """What the screen is ordered by."""

    VALUE = "value"
    WEIGHT = "weight"
    WEIGHT_CHANGE = "weight_change"
    TRADED = "traded"


#: The dollars a change bought or sold at the period-end price, as the flows
#: count them (:func:`traded_usd`), to the cent. Null in a filer's first
#: period, which bought nothing: those positions are only the first we have.
_TRADED: Final = case((_A_CHANGE, func.round(traded_usd(), 2)), else_=None)


#: Beyond every real weight or move, either way: where a row with none sorts,
#: so it is last whichever way the screen runs.
_BEYOND: Final = Decimal(1000)


def screen_keyset(sort: ScreenSort, order: SortOrder) -> Keyset:
    """The sort, then the position's value, then the filer and the security:
    a total order, every key one way, so a page boundary is one row comparison."""
    descending = order is SortOrder.DESC
    missing = literal(-_BEYOND if descending else _BEYOND, Numeric)
    missing_dollars = literal(-1 if descending else MAX_DOLLARS, Numeric)
    lead = {
        ScreenSort.VALUE: [],
        ScreenSort.WEIGHT: [
            SortKey("weight_key", func.coalesce(_change.weight_pct, missing), Decimal, descending)
        ],
        ScreenSort.WEIGHT_CHANGE: [
            SortKey(
                "weight_change_key",
                func.coalesce(_change.weight_delta, missing),
                Decimal,
                descending,
            )
        ],
        ScreenSort.TRADED: [
            SortKey("traded_key", func.coalesce(_TRADED, missing_dollars), Decimal, descending)
        ],
    }[sort]
    return Keyset(
        f"screener.{sort.value}.{order.value}",
        (
            *lead,
            SortKey("value_usd", _change.value_usd.expression, Decimal, descending),
            SortKey("filer_id", _change.filer_id.expression, int, descending),
            SortKey("security_id", _change.security_id.expression, int, descending),
        ),
    )


def screen_query(
    screen: Screen,
    scope: Scope,
    page: PageParams | None,
    *,
    sort: ScreenSort = ScreenSort.VALUE,
    order: SortOrder = SortOrder.DESC,
) -> tuple[Select[Any], Keyset]:
    """One page of the positions passing every filter, named, with the stock's
    holders and net flow at the period and the estimates (:mod:`app.api.estimates`),
    and the keyset."""
    keyset = screen_keyset(sort, order)
    lead = [key.expression.label(key.name) for key in keyset.keys[:-3]]
    rows = select(
        _change.filer_id,
        _change.security_id,
        _change.prev_period_of_report,
        _change.action,
        _change.shares,
        _change.shares_delta,
        _change.shares_delta_pct,
        _change.value_usd,
        _change.weight_pct,
        _change.weight_delta,
        _change.is_split,
        _change.split_ratio,
        _TRADED.label("traded_value_usd"),
        *lead,
    ).where(*screen.conditions(scope))
    page_rows = page_statement(rows, keyset, page).cte("page")

    consensus, flows = CONSENSUS_HOLDINGS, QUARTER_FLOWS
    returns = estimated_return(page_rows.c.security_id, scope.period)
    statement = (
        select(
            page_rows,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            Filer.category,
            Security.cusip,
            *ticker_labels(scope.period),
            Security.name.label("issuer_name"),
            func.coalesce(consensus.c.holder_count, 0).label("holder_count"),
            flows.c.net_value_usd,
            *estimate_columns(returns),
        )
        .select_from(page_rows)
        .join(Filer, Filer.id == page_rows.c.filer_id)
        .join(Security, Security.id == page_rows.c.security_id)
        .outerjoin(
            consensus,
            and_(
                consensus.c.period_of_report == scope.period,
                consensus.c.security_id == page_rows.c.security_id,
            ),
        )
        .outerjoin(
            flows,
            and_(
                flows.c.period_of_report == scope.period,
                flows.c.security_id == page_rows.c.security_id,
            ),
        )
        .outerjoin(returns, true())
        .order_by(*keyset.order_by(on=page_rows))
    )
    return statement, keyset
