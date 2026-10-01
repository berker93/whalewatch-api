"""``reconcile``: the derived tables, checked against each other and against what they came from.

``check-data`` reads the filings before they are published. This reads what was
published: ``position_snapshot``, ``position_change`` and the materialised views,
as they are stored. Each invariant is a query for the rows that break it. A
clean reconciliation is every one of them coming back empty, and a failing one
says which rows to look at.

The same queries run in two places, and both matter. ``test_reconcile`` runs them
over a fixture small enough that every number in it was worked out by hand, and
breaks each invariant on purpose to see it caught. That proves the queries can
tell right from wrong. ``make reconcile`` runs them over the real dataset, where
nobody knows the right answer, and where the cases nobody thought to put in a
fixture are.

The invariants
--------------
In the order :data:`INVARIANTS` runs them:

``weights_sum_to_100``
    Every ``(filer, period)``'s weights sum to 100, within
    :data:`WEIGHT_TOLERANCE_PCT`. A period worth nothing has no weights at all.
``snapshot_traces_to_filing``
    Every snapshot row's ``source_filing_id`` counts toward the row's own
    ``(filer, period)``, holds the security as common stock, and is not suspect,
    and neither is any other filing the period was built from.
``changes_match_snapshot``
    Every change but an exit has the snapshot row at its key, with the same
    figures, and every snapshot row has its change. An exit has none, since the
    position is gone.
``changes_follow_previous_period``
    Every change is against the filer's previous *published* period. Its
    previous figures are the snapshot's in that period, a ``new`` position was
    not held then, and every position the filer sold out of has its ``exit``.
``new_and_exit_rows``
    ``new`` rows, and only those, have no previous shares. ``exit`` rows hold
    nothing: no shares, no value, no weight.
``value_deltas_add_up``
    A ``(filer, period)``'s ``value_delta`` sums to the change in its
    portfolio's value since the filer's previous published period.
``nothing_negative``
    No snapshot row has negative shares or a negative value.
``views_match_live``
    Every materialised view holds exactly the rows its live query returns now.

Exact where it can be
---------------------
Only the weights get a tolerance, because each one is rounded to six places when
stored. ``value_deltas_add_up`` allows nothing, where a tolerance would be easy
to reach for. Every position the previous period held is a change row here,
either held on or exited, and a new one counts from zero, so the sum telescopes
to the difference of the two totals. It is computed in numeric, with nothing to
round, so any difference at all is a wrong row, and a tolerance could only let
one through. What it would let through is a ``value_delta`` wrong by less than
the tolerance on a row whose other figures are right. A missing exit, whatever
its size, is ``changes_follow_previous_period``'s to catch.

Overlap with the schema
-----------------------
``new_and_exit_rows`` and ``nothing_negative`` restate ``CHECK`` constraints
(``new_when_not_held_before``, ``an_exit_holds_nothing``, and ``holding``'s
non-negative columns, which every snapshot row is a sum of). They are here so
that the list is the whole contract in one place. A constraint dropped or added
``NOT VALID`` by a later migration would otherwise stop being checked without
anyone noticing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    Date,
    FunctionElement,
    Select,
    SQLColumnExpression,
    Text,
    and_,
    case,
    cast,
    column,
    except_,
    func,
    literal,
    null,
    or_,
    select,
    table,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.filer import Filer
from app.db.models.filing import Filing, ParseStatus
from app.db.models.holding import Holding
from app.db.models.position_change import ChangeAction, PositionChange
from app.db.models.position_snapshot import PositionSnapshot
from app.db.models.security import Security
from app.db.queries.effective import EFFECTIVE_FILING
from app.db.queries.periods import FILER_PERIOD
from app.derived.views import MATERIALISED_VIEWS, MaterialisedView

#: How far a period's stored weights may sum from 100. Each weight is rounded to
#: six places, so a period of n positions can be off by n / 2,000,000: 0.01 at
#: 20,000 positions, which no 13F in the universe comes near.
WEIGHT_TOLERANCE_PCT: Final = Decimal("0.01")

#: How many failing rows a reconciliation keeps per invariant, by default.
#: Enough to see the pattern. The count is always of all of them.
SAMPLE: Final = 5

#: Every invariant's query returns these columns, in this order. A period-level
#: violation has no ``security_id``, and a per-security view's has no ``filer_id``.
VIOLATION_COLUMNS: Final = ("filer_id", "period_of_report", "security_id", "problem", "detail")

_EXIT: Final = ChangeAction.EXIT.value
_NEW: Final = ChangeAction.NEW.value
_SUSPECT: Final = ParseStatus.SUSPECT.value


@dataclass(frozen=True, slots=True)
class Published:
    """How the tables were published, and the state of the views: what the invariants allow."""

    include_suspect: bool = False
    """The tables were rebuilt with ``recompute --include-suspect``. A period a
    suspect filing counts toward is then allowed, as long as every row of it
    says so."""
    unpopulated: frozenset[str] = frozenset()
    """Materialised views that refuse reads, so cannot be compared with anything."""


@dataclass(frozen=True, slots=True)
class Invariant:
    """One thing that must hold of the published tables, and a query for the rows breaking it."""

    name: str
    rule: str
    """The invariant as one sentence, for the ``reconcile`` output."""
    violations: Callable[[Published], Select[Any]]
    """The rows that break it, as :data:`VIOLATION_COLUMNS`. Empty when it holds."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One row that breaks an invariant, with its filer and security by name."""

    slug: str | None
    period: date | None
    """Null only for a view reported as a whole."""
    cusip: str | None
    problem: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class Checked:
    """One invariant, checked: how many rows break it, and some of them."""

    invariant: Invariant
    violations: int
    sample: tuple[Violation, ...]
    """The first of them, in a fixed order: by problem, filer, period, security."""

    @property
    def holds(self) -> bool:
        return self.violations == 0


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Every invariant, checked, and how much there was to check them on."""

    checked: tuple[Checked, ...]
    positions: int
    changes: int
    periods: int
    include_suspect: bool

    @property
    def failed(self) -> tuple[Checked, ...]:
        return tuple(checked for checked in self.checked if not checked.holds)

    @property
    def clean(self) -> bool:
        return not self.failed

    @property
    def violations(self) -> int:
        return sum(checked.violations for checked in self.checked)


async def reconcile(
    session: AsyncSession, *, include_suspect: bool = False, sample: int | None = SAMPLE
) -> Reconciliation:
    """Check every invariant against the tables as they are in ``session``'s transaction.

    Read-only. Each invariant is one statement, so run this in a ``REPEATABLE
    READ`` transaction, as ``reconcile`` does, for every one of them to see the
    same tables. In ``READ COMMITTED``, a rebuild that commits halfway through
    shows up as a disagreement between two invariants' reads.

    :param include_suspect: The tables were rebuilt with ``--include-suspect``.
    :param sample: How many violating rows to keep per invariant. ``None`` keeps
        every one, which is for tests, where there are few.
    """
    published = Published(
        include_suspect=include_suspect, unpopulated=await _unpopulated_views(session)
    )
    checked = [
        await _check(session, invariant, published, sample=sample) for invariant in INVARIANTS
    ]
    positions, changes, periods = (
        await session.execute(
            select(
                select(func.count()).select_from(PositionSnapshot).scalar_subquery(),
                select(func.count()).select_from(PositionChange).scalar_subquery(),
                select(func.count()).select_from(FILER_PERIOD).scalar_subquery(),
            )
        )
    ).one()
    return Reconciliation(
        checked=tuple(checked),
        positions=positions,
        changes=changes,
        periods=periods,
        include_suspect=include_suspect,
    )


async def _check(
    session: AsyncSession, invariant: Invariant, published: Published, *, sample: int | None
) -> Checked:
    """Count the rows breaking ``invariant`` and keep the first ``sample`` of them.

    One pass: the count is a window over every violating row, computed before
    the ``LIMIT`` takes the first few.
    """
    found = invariant.violations(published).subquery("found")
    statement = (
        select(
            Filer.slug,
            found.c.period_of_report,
            Security.cusip,
            found.c.problem,
            found.c.detail,
            func.count().over().label("violations"),
        )
        .select_from(found)
        .outerjoin(Filer, Filer.id == found.c.filer_id)
        .outerjoin(Security, Security.id == found.c.security_id)
        .order_by(found.c.problem, Filer.slug, found.c.period_of_report, Security.cusip)
        .limit(sample)
    )
    rows = (await session.execute(statement)).all()
    return Checked(
        invariant=invariant,
        violations=rows[0].violations if rows else 0,
        sample=tuple(
            Violation(
                slug=row.slug,
                period=row.period_of_report,
                cusip=row.cusip,
                problem=row.problem,
                detail=row.detail,
            )
            for row in rows
        ),
    )


#: The catalog's list of materialised views, for whether each is populated.
_PG_MATVIEWS: Final = table(
    "pg_matviews",
    column("schemaname", Text),
    column("matviewname", Text),
    column("ispopulated", Boolean),
)


async def _unpopulated_views(session: AsyncSession) -> frozenset[str]:
    """The materialised views a read would fail on: refreshed ``WITH NO DATA``."""
    rows = await session.scalars(
        select(_PG_MATVIEWS.c.matviewname).where(
            _PG_MATVIEWS.c.schemaname == func.current_schema(),
            _PG_MATVIEWS.c.matviewname.in_([view.name for view in MATERIALISED_VIEWS]),
            _PG_MATVIEWS.c.ispopulated.is_(False),
        )
    )
    return frozenset(rows.all())


# --- the shape every invariant returns ------------------------------------------


def _violation(
    *,
    filer_id: SQLColumnExpression[Any] | None,
    period: SQLColumnExpression[Any],
    security_id: SQLColumnExpression[Any] | None,
    problem: SQLColumnExpression[Any] | str,
    detail: SQLColumnExpression[Any] | None,
) -> list[ColumnElement[Any]]:
    """:data:`VIOLATION_COLUMNS`, labelled, with typed nulls for the ones that do not apply.

    Typed, because the invariants with several cases are a ``UNION ALL``, and
    Postgres types an untyped null in one branch as ``text``.
    """
    return [
        (cast(null(), BigInteger) if filer_id is None else filer_id).label("filer_id"),
        cast(period, Date).label("period_of_report"),
        (cast(null(), BigInteger) if security_id is None else security_id).label("security_id"),
        (literal(problem, Text) if isinstance(problem, str) else problem).label("problem"),
        (cast(null(), Text) if detail is None else detail).label("detail"),
    ]


def _text(template: str, *values: SQLColumnExpression[Any]) -> ColumnElement[str]:
    """Postgres ``format()``: each ``%s`` is a value as text, and a null is empty."""
    return func.format(literal(template, Text), *values, type_=Text)


def _found(statement: Select[Any]) -> Select[Any]:
    """The rows of ``statement`` whose ``problem`` is not null.

    For the invariants that work out, in a ``CASE``, which of several things is
    wrong with a row, and leave ``problem`` null on a row with nothing wrong.
    """
    rows = statement.subquery()
    return select(*(rows.c[name] for name in VIOLATION_COLUMNS)).where(rows.c.problem.is_not(None))


def _periods() -> Select[Any]:
    """Each filer's published periods, with the one before and the one after, from ``filer_period``.

    The same notion of previous that ``position_change`` uses, written out again
    here, so that the checks are against the rule, not the query that applies it.
    """
    view = FILER_PERIOD

    def over_periods(adjacent: FunctionElement[Any]) -> ColumnElement[Any]:
        return adjacent.over(partition_by=view.c.filer_id, order_by=view.c.period_of_report)

    return select(
        view.c.filer_id,
        view.c.period_of_report,
        over_periods(func.lag(view.c.period_of_report)).label("prev_period_of_report"),
        over_periods(func.lead(view.c.period_of_report)).label("next_period_of_report"),
    )


# --- the invariants -------------------------------------------------------------


def weights_sum_to_100(published: Published) -> Select[Any]:
    """Each ``(filer, period)`` whose weights do not sum to 100 within :data:`WEIGHT_TOLERANCE_PCT`.

    A period worth nothing has no total to divide by, so its weights are all
    null. Any weight there is wrong, and so is a null weight anywhere else.
    """
    snapshot = PositionSnapshot
    value = func.sum(snapshot.value_usd)
    weight = func.sum(snapshot.weight_pct)
    weighed, positions = func.count(snapshot.weight_pct), func.count()
    worthless = value == 0
    return (
        select(
            *_violation(
                filer_id=snapshot.filer_id,
                period=snapshot.period_of_report,
                security_id=None,
                problem=case(
                    (worthless, "a period worth nothing has weights"),
                    (weighed < positions, "a position in a period worth something has no weight"),
                    else_="weights do not sum to 100",
                ),
                detail=_text(
                    "weights sum to %s over %s positions worth $%s", weight, positions, value
                ),
            )
        )
        .group_by(snapshot.filer_id, snapshot.period_of_report)
        .having(
            or_(
                and_(worthless, weighed > 0),
                and_(
                    ~worthless,
                    or_(weighed < positions, func.abs(weight - 100) > WEIGHT_TOLERANCE_PCT),
                ),
            )
        )
    )


def snapshot_traces_to_filing(published: Published) -> Select[Any]:
    """Each snapshot row whose source filing does not account for it, or is suspect.

    The source must be one of the filings that count toward the row's own
    ``(filer, period)`` in ``effective_filing``, and must hold the security as
    common stock. Neither it nor any other filing the period was built from may
    be suspect. With :attr:`Published.include_suspect` they may be, as long as
    the row says so. Either way, a row's ``suspect`` must say whether one is.
    """
    snapshot = PositionSnapshot
    source = aliased(Filing, name="source")
    counted = EFFECTIVE_FILING.alias("counted")
    behind = (
        select(
            EFFECTIVE_FILING.c.filer_id,
            EFFECTIVE_FILING.c.period_of_report,
            func.bool_or(Filing.parse_status == _SUSPECT).label("suspect"),
        )
        .join(Filing, Filing.id == EFFECTIVE_FILING.c.filing_id)
        .group_by(EFFECTIVE_FILING.c.filer_id, EFFECTIVE_FILING.c.period_of_report)
        .subquery("behind")
    )
    # Which filings hold which securities as stock. The GROUP BY changes no
    # rows, since stock is one line per CUSIP on holding's natural key, and is
    # there for the plan. A plain subquery is flattened into the join, which
    # Postgres then runs as an index probe on ix_holding_security_id per
    # snapshot row: 471,032 probes reading 400 rows apiece, 94 s on the real
    # data. Grouped, it cannot be probed, and is one hash join: under a second.
    stock = (
        select(Holding.filing_id, Holding.security_id)
        .where(Holding.put_call.is_(None), Holding.sshprnamt_type == "SH")
        .group_by(Holding.filing_id, Holding.security_id)
        .subquery("stock")
    )

    cases: list[tuple[ColumnElement[bool], str]] = [
        (counted.c.filing_id.is_(None), "its source filing does not count toward its period"),
        (stock.c.filing_id.is_(None), "its source filing does not hold the security as stock"),
    ]
    if not published.include_suspect:
        cases += [
            (source.parse_status == _SUSPECT, "its source filing is suspect"),
            (behind.c.suspect, "another filing its period was built from is suspect"),
        ]
    cases.append((snapshot.suspect.is_distinct_from(behind.c.suspect), "its suspect flag is wrong"))

    return _found(
        select(
            *_violation(
                filer_id=snapshot.filer_id,
                period=snapshot.period_of_report,
                security_id=snapshot.security_id,
                problem=case(*cases),
                detail=_text(
                    "source %s, %s, for %s",
                    source.accession_no,
                    source.parse_status,
                    source.period_of_report,
                ),
            )
        )
        .select_from(snapshot)
        .join(source, source.id == snapshot.source_filing_id)
        .outerjoin(
            counted,
            and_(
                counted.c.filing_id == snapshot.source_filing_id,
                counted.c.filer_id == snapshot.filer_id,
                counted.c.period_of_report == snapshot.period_of_report,
            ),
        )
        .outerjoin(
            stock,
            and_(
                stock.c.filing_id == snapshot.source_filing_id,
                stock.c.security_id == snapshot.security_id,
            ),
        )
        .outerjoin(
            behind,
            and_(
                behind.c.filer_id == snapshot.filer_id,
                behind.c.period_of_report == snapshot.period_of_report,
            ),
        )
    )


def changes_match_snapshot(published: Published) -> Select[Any]:
    """Each change without the snapshot row it describes, and each snapshot row without its change.

    Every change but an exit is a snapshot row with its previous period's
    figures beside it, so the two tables have the same keys and the change
    repeats the row's shares, value and weight. An exit is the one change whose
    key the snapshot does not have.
    """
    change, snapshot = PositionChange, PositionSnapshot
    same_key = and_(
        snapshot.filer_id == change.filer_id,
        snapshot.period_of_report == change.period_of_report,
        snapshot.security_id == change.security_id,
    )
    exited = change.action == _EXIT
    same_figures = and_(
        snapshot.shares == change.shares,
        snapshot.value_usd == change.value_usd,
        snapshot.weight_pct.is_not_distinct_from(change.weight_pct),
    )

    from_changes = _found(
        select(
            *_violation(
                filer_id=change.filer_id,
                period=change.period_of_report,
                security_id=change.security_id,
                problem=case(
                    (exited & snapshot.security_id.is_not(None), "an exit still in the snapshot"),
                    (~exited & snapshot.security_id.is_(None), "a change with no snapshot row"),
                    (~exited & ~same_figures, "a change whose figures are not the snapshot's"),
                ),
                detail=_text(
                    "%s: %s shares, $%s, %s%% against the snapshot's %s shares, $%s, %s%%",
                    change.action,
                    change.shares,
                    change.value_usd,
                    change.weight_pct,
                    snapshot.shares,
                    snapshot.value_usd,
                    snapshot.weight_pct,
                ),
            )
        )
        .select_from(change)
        .outerjoin(snapshot, same_key)
    )
    from_snapshot = (
        select(
            *_violation(
                filer_id=snapshot.filer_id,
                period=snapshot.period_of_report,
                security_id=snapshot.security_id,
                problem="a snapshot row with no change",
                detail=_text("%s shares, $%s", snapshot.shares, snapshot.value_usd),
            )
        )
        .select_from(snapshot)
        .outerjoin(change, same_key)
        .where(change.security_id.is_(None))
    )
    return union_all(from_changes, from_snapshot).subquery().select()


def changes_follow_previous_period(published: Published) -> Select[Any]:
    """Each change not taken against the filer's previous published period, and each missing exit.

    The previous period is the one ``filer_period`` has before this one, with
    gaps and withheld quarters stepped over. A change's previous figures are the
    snapshot's row for the security there, and a ``new`` position has none
    there. A position whose security is not in the filer's next published period
    has an ``exit`` dated that period.
    """
    change, snapshot = PositionChange, PositionSnapshot
    periods = _periods().subquery("periods")
    then = aliased(PositionSnapshot, name="then")
    had = change.prev_shares.is_not(None)
    same_figures = and_(
        then.shares == change.prev_shares,
        then.value_usd == change.prev_value_usd,
        then.weight_pct.is_not_distinct_from(change.prev_weight_pct),
    )

    compared = _found(
        select(
            *_violation(
                filer_id=change.filer_id,
                period=change.period_of_report,
                security_id=change.security_id,
                problem=case(
                    (periods.c.period_of_report.is_(None), "a change in a period not published"),
                    (
                        change.prev_period_of_report.is_distinct_from(
                            periods.c.prev_period_of_report
                        ),
                        "a change against a period other than the filer's previous published one",
                    ),
                    (~had & then.security_id.is_not(None), "new, but held in the previous period"),
                    (had & then.security_id.is_(None), "previous figures with no snapshot row"),
                    (had & ~same_figures, "previous figures that are not the snapshot's"),
                ),
                detail=_text(
                    "%s against %s: %s shares, $%s; the snapshot there has %s shares, $%s; "
                    "the previous published period is %s",
                    change.action,
                    change.prev_period_of_report,
                    change.prev_shares,
                    change.prev_value_usd,
                    then.shares,
                    then.value_usd,
                    periods.c.prev_period_of_report,
                ),
            )
        )
        .select_from(change)
        .outerjoin(
            periods,
            and_(
                periods.c.filer_id == change.filer_id,
                periods.c.period_of_report == change.period_of_report,
            ),
        )
        .outerjoin(
            then,
            and_(
                then.filer_id == change.filer_id,
                then.period_of_report == change.prev_period_of_report,
                then.security_id == change.security_id,
            ),
        )
    )

    later = aliased(PositionSnapshot, name="later")
    gone = (
        select(
            *_violation(
                filer_id=snapshot.filer_id,
                period=periods.c.next_period_of_report,
                security_id=snapshot.security_id,
                problem="sold out of with no exit",
                detail=_text(
                    "%s shares, $%s in %s",
                    snapshot.shares,
                    snapshot.value_usd,
                    snapshot.period_of_report,
                ),
            )
        )
        .select_from(snapshot)
        .join(
            periods,
            and_(
                periods.c.filer_id == snapshot.filer_id,
                periods.c.period_of_report == snapshot.period_of_report,
            ),
        )
        .outerjoin(
            later,
            and_(
                later.filer_id == snapshot.filer_id,
                later.period_of_report == periods.c.next_period_of_report,
                later.security_id == snapshot.security_id,
            ),
        )
        .outerjoin(
            change,
            and_(
                change.filer_id == snapshot.filer_id,
                change.period_of_report == periods.c.next_period_of_report,
                change.security_id == snapshot.security_id,
            ),
        )
        .where(
            periods.c.next_period_of_report.is_not(None),
            later.security_id.is_(None),
            change.security_id.is_(None),
        )
    )
    return union_all(compared, gone).subquery().select()


def new_and_exit_rows(published: Published) -> Select[Any]:
    """Each ``new`` row with previous shares, other row without, and ``exit`` holding anything."""
    change = PositionChange
    new, exited = change.action == _NEW, change.action == _EXIT
    return _found(
        select(
            *_violation(
                filer_id=change.filer_id,
                period=change.period_of_report,
                security_id=change.security_id,
                problem=case(
                    (new & change.prev_shares.is_not(None), "a new position with previous shares"),
                    (~new & change.prev_shares.is_(None), f"no previous shares, but not {_NEW}"),
                    (exited & (change.shares != 0), "an exit with shares"),
                    (
                        exited & or_(change.value_usd != 0, change.weight_pct.is_distinct_from(0)),
                        "an exit with value or weight",
                    ),
                ),
                detail=_text(
                    "%s: %s shares, $%s, %s%%; previously %s shares",
                    change.action,
                    change.shares,
                    change.value_usd,
                    change.weight_pct,
                    change.prev_shares,
                ),
            )
        )
    )


def value_deltas_add_up(published: Published) -> Select[Any]:
    """Each ``(filer, period)`` whose ``value_delta`` does not sum to the change in portfolio value.

    The change is this period's total less the filer's previous published
    period's, or less nothing in its first. Exactly, for the reason in the
    module docstring. Either side missing is a violation too: changes in a
    period not published, or a published period with no changes.
    """
    snapshot, change = PositionSnapshot, PositionChange
    periods = _periods().subquery("periods")
    book = (
        select(
            snapshot.filer_id,
            snapshot.period_of_report,
            func.sum(snapshot.value_usd).label("value_usd"),
        )
        .group_by(snapshot.filer_id, snapshot.period_of_report)
        .subquery("book")
    )
    now, before = book.alias("now"), book.alias("before")
    moved = (
        select(
            periods.c.filer_id,
            periods.c.period_of_report,
            now.c.value_usd.label("value_usd"),
            before.c.value_usd.label("prev_value_usd"),
            (now.c.value_usd - func.coalesce(before.c.value_usd, 0)).label("moved"),
        )
        .join(
            now,
            and_(
                now.c.filer_id == periods.c.filer_id,
                now.c.period_of_report == periods.c.period_of_report,
            ),
        )
        .outerjoin(
            before,
            and_(
                before.c.filer_id == periods.c.filer_id,
                before.c.period_of_report == periods.c.prev_period_of_report,
            ),
        )
        .subquery("moved")
    )
    deltas = (
        select(
            change.filer_id,
            change.period_of_report,
            func.sum(change.value_delta).label("value_delta"),
        )
        .group_by(change.filer_id, change.period_of_report)
        .subquery("deltas")
    )
    return (
        select(
            *_violation(
                filer_id=func.coalesce(moved.c.filer_id, deltas.c.filer_id),
                period=func.coalesce(moved.c.period_of_report, deltas.c.period_of_report),
                security_id=None,
                problem=case(
                    (moved.c.filer_id.is_(None), "value deltas in a period not published"),
                    (deltas.c.filer_id.is_(None), "a published period with no changes"),
                    else_="value deltas do not add up to the change in portfolio value",
                ),
                detail=_text(
                    "deltas sum to $%s; the portfolio went from $%s to $%s, a change of $%s",
                    deltas.c.value_delta,
                    moved.c.prev_value_usd,
                    moved.c.value_usd,
                    moved.c.moved,
                ),
            )
        )
        .select_from(moved)
        .outerjoin(
            deltas,
            and_(
                deltas.c.filer_id == moved.c.filer_id,
                deltas.c.period_of_report == moved.c.period_of_report,
            ),
            full=True,
        )
        .where(deltas.c.value_delta.is_distinct_from(moved.c.moved))
    )


def nothing_negative(published: Published) -> Select[Any]:
    """Each snapshot row with negative shares or a negative value."""
    snapshot = PositionSnapshot
    return select(
        *_violation(
            filer_id=snapshot.filer_id,
            period=snapshot.period_of_report,
            security_id=snapshot.security_id,
            problem=case((snapshot.shares < 0, "negative shares"), else_="a negative value"),
            detail=_text("%s shares, $%s", snapshot.shares, snapshot.value_usd),
        )
    ).where(or_(snapshot.shares < 0, snapshot.value_usd < 0))


def views_match_live(published: Published) -> Select[Any]:
    """Each materialised view's row that its live query does not return, and the reverse.

    A row that differs is in both directions of the ``EXCEPT``, and is reported
    once, by its key. A view that refuses reads is reported as a whole: the API
    cannot read it either.
    """
    branches: list[Select[Any]] = [
        select(
            *_violation(
                filer_id=None,
                period=cast(null(), Date),
                security_id=None,
                problem=f"{name} is not populated: refresh-views fills it",
                detail=None,
            )
        )
        for name in sorted(published.unpopulated)
    ]
    branches += [
        _disagreements(view)
        for view in MATERIALISED_VIEWS
        if view.name not in published.unpopulated
    ]
    return union_all(*branches).subquery().select()


def _disagreements(view: MaterialisedView) -> Select[Any]:
    """``view``'s keys whose row it and its live query do not agree on.

    The live query is built once and used on both sides of the ``EXCEPT``. Each
    call builds its CTEs afresh, and two copies would be two CTEs of one name.
    """
    stored, live = select(view.handle), view.live()
    only_stored = except_(stored, live).subquery()
    only_live = except_(live, stored).subquery()
    sides = union_all(
        select(*(only_stored.c[key] for key in view.key), literal("view", Text).label("side")),
        select(*(only_live.c[key] for key in view.key), literal("live", Text).label("side")),
    ).subquery("sides")
    keyed = {key: sides.c[key] for key in view.key}
    return select(
        *_violation(
            filer_id=keyed.get("filer_id"),
            period=keyed["period_of_report"],
            security_id=keyed.get("security_id"),
            problem=case(
                (func.count() > 1, f"{view.name} has a row that differs from its live query"),
                (func.min(sides.c.side) == "view", f"{view.name} has a row its live query lacks"),
                else_=f"{view.name} is missing a row its live query has",
            ),
            detail=None,
        )
    ).group_by(*keyed.values())


#: Every invariant, in the order ``reconcile`` checks and prints them.
INVARIANTS: Final = (
    Invariant(
        "weights_sum_to_100",
        "every (filer, period)'s weights sum to 100 ± 0.01",
        weights_sum_to_100,
    ),
    Invariant(
        "snapshot_traces_to_filing",
        "every position traces to a non-suspect filing that counts toward its period",
        snapshot_traces_to_filing,
    ),
    Invariant(
        "changes_match_snapshot",
        "every change but an exit is a snapshot row, and every snapshot row has its change",
        changes_match_snapshot,
    ),
    Invariant(
        "changes_follow_previous_period",
        "every change is against the filer's previous published period, exits included",
        changes_follow_previous_period,
    ),
    Invariant(
        "new_and_exit_rows",
        "new rows alone have no previous shares, and exit rows hold nothing",
        new_and_exit_rows,
    ),
    Invariant(
        "value_deltas_add_up",
        "every (filer, period)'s value deltas sum to its change in portfolio value, exactly",
        value_deltas_add_up,
    ),
    Invariant(
        "nothing_negative",
        "no position has negative shares or value",
        nothing_negative,
    ),
    Invariant(
        "views_match_live",
        "every materialised view holds what its live query returns now",
        views_match_live,
    ),
)
