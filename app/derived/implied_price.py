"""``implied_price``: each security's quarter-end price, read off its holders' filings.

``recompute`` writes the table from :func:`implied_prices`, straight after the
snapshot, in the same transaction. Every ``position_snapshot`` row with shares
is an observation of ``value_usd / shares``. A security's observations in a
period are grouped, and their median is the price.

The query, in three steps
-------------------------
1. ``observations``: one per snapshot row with shares, its price, and whether
   it is priced at all (worth more than $0).
2. ``medians``: per ``(security, period)``, the counts and the median priced
   observation, with ``percentile_disc(0.5)``. ``_disc``, not ``_cont``, so the
   median is always one of the observations, in exact ``numeric``:
   ``percentile_cont`` interpolates in double precision, and between a filer in
   dollars and one in thousands it would invent a price nobody reported.
3. The final select counts the observations within :data:`AGREEMENT_PCT` of the
   median, which is a second pass over ``observations`` joined to it. An
   ordered-set aggregate cannot be a window, so the median cannot be put on
   each observation in the first pass. Then it rounds the price and grades the
   agreement.

A different axis from the rest of ``recompute``
-----------------------------------------------
The snapshot and the changes belong to a ``(filer, period)``. A price belongs
to a ``(security, period)``, and is computed across filers. So a rebuild of one
filer's period touches the price of every security that filer held then, or
holds now, and those prices draw on every other filer's rows as well.
:func:`held_in` reads those keys from the snapshot, before the rebuild and after
it: a security an amendment removes from the period still has a price that
counted it. Only those keys are rebuilt. ``recompute --all`` has no keys, and
rebuilds every row.

The keys go to Postgres as two arrays, unnested back into pairs, not as a
``(security_id, period) IN (...)`` list. One filer's every period is tens of
thousands of keys, and two bind parameters per key would pass asyncpg's limit
of 32,767.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Date,
    Select,
    SQLColumnExpression,
    and_,
    case,
    delete,
    func,
    insert,
    literal,
    null,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.implied_price import ImpliedPrice, PriceConfidence
from app.db.models.position_snapshot import PositionSnapshot
from app.derived.scope import Scope

SecurityPeriod = tuple[int, date]
"""``(security_id, period_of_report)``: the unit an implied price belongs to."""

#: How far from the median, in percent either way, an observation may be and
#: still agree with it. A 13F value before 2023 is in thousands, rounded, so a
#: $50,000 position can be off by 1% from rounding alone. Two percent lets that
#: through, and nothing like a split, a scale error or a stale mark.
AGREEMENT_PCT: Final = Decimal(2)

#: The places a price is rounded to, as the column stores it.
PRICE_PLACES: Final = 8

#: :func:`implied_prices`'s columns, in order, as ``implied_price`` names them.
IMPLIED_PRICE_COLUMNS: Final = (
    "security_id",
    "period_of_report",
    "price",
    "observation_count",
    "unpriced_count",
    "agreeing_count",
    "confidence",
    "suspect",
)


@dataclass(frozen=True, slots=True)
class PriceRebuild:
    """What one :func:`recompute_implied_price` wrote, by confidence."""

    high: int
    medium: int
    low: int
    unpriced: int
    """Rows with no price: every observation worth $0."""

    @property
    def prices(self) -> int:
        return self.high + self.medium + self.low + self.unpriced


def implied_prices(keys: frozenset[SecurityPeriod] | None = None) -> Select[Any]:
    """The rows ``implied_price`` holds: one per security per period it is held with shares.

    :param keys: Only these ``(security, period)`` pairs, or every one there is.
        A pair's row depends on that security's snapshot rows in that period
        alone, so it is the same either way.
    :returns: A select of :data:`IMPLIED_PRICE_COLUMNS`, unordered.
    """
    snapshot = PositionSnapshot
    observations = (
        select(
            snapshot.security_id,
            snapshot.period_of_report,
            (snapshot.value_usd / snapshot.shares).label("price"),
            (snapshot.value_usd > 0).label("priced"),
            (snapshot.value_usd == 0).label("unpriced"),
            snapshot.suspect,
        )
        .where(snapshot.shares > 0, *_keyed(keys, snapshot.security_id, snapshot.period_of_report))
        .cte("observations")
    )
    priced = observations.c.priced
    medians = (
        select(
            observations.c.security_id,
            observations.c.period_of_report,
            func.count().label("observation_count"),
            func.count().filter(observations.c.unpriced).label("unpriced_count"),
            func.percentile_disc(0.5)
            .within_group(observations.c.price)
            .filter(priced)
            .label("median"),
            func.bool_or(observations.c.suspect).label("suspect"),
        )
        .group_by(observations.c.security_id, observations.c.period_of_report)
        .cte("medians")
    )

    # Multiplied rather than divided, in exact numeric. A median is null only
    # when nothing is priced, and then nothing is counted, by the FILTER.
    agreeing = func.count().filter(
        priced,
        func.abs(observations.c.price - medians.c.median) * 100 <= medians.c.median * AGREEMENT_PCT,
    )
    rounded = func.nullif(func.round(medians.c.median, PRICE_PLACES), 0)
    candidates = medians.c.observation_count - medians.c.unpriced_count
    return (
        select(
            medians.c.security_id,
            medians.c.period_of_report,
            rounded.label("price"),
            medians.c.observation_count,
            medians.c.unpriced_count,
            agreeing.label("agreeing_count"),
            case(
                (rounded.is_(None), null()),
                (and_(agreeing >= 3, agreeing * 2 > candidates), PriceConfidence.HIGH.value),
                (and_(agreeing >= 2, agreeing * 2 > candidates), PriceConfidence.MEDIUM.value),
                else_=PriceConfidence.LOW.value,
            ).label("confidence"),
            medians.c.suspect,
        )
        .select_from(medians)
        .join(
            observations,
            and_(
                observations.c.security_id == medians.c.security_id,
                observations.c.period_of_report == medians.c.period_of_report,
            ),
        )
        .group_by(*medians.c)
    )


async def recompute_implied_price(
    session: AsyncSession, keys: frozenset[SecurityPeriod] | None = None
) -> PriceRebuild:
    """Replace ``implied_price`` — all of it, or the rows at ``keys`` — from ``position_snapshot``.

    Delete and reinsert in the caller's transaction, for the snapshot's
    reasons: a reader never sees a half-built table, and a security no longer
    held in a period must lose its row, which an upsert would leave.

    :param keys: Rebuild these ``(security, period)`` pairs and leave every
        other row alone. ``None`` rebuilds every row.
    """
    if keys is not None and not keys:
        return PriceRebuild(high=0, medium=0, low=0, unpriced=0)
    in_scope = _keyed(keys, ImpliedPrice.security_id, ImpliedPrice.period_of_report)

    await session.execute(delete(ImpliedPrice).where(*in_scope))
    await session.execute(
        insert(ImpliedPrice).from_select(IMPLIED_PRICE_COLUMNS, implied_prices(keys))
    )

    def graded(level: PriceConfidence) -> ColumnElement[int]:
        return func.count().filter(ImpliedPrice.confidence == level.value)

    written = (
        await session.execute(
            select(
                graded(PriceConfidence.HIGH),
                graded(PriceConfidence.MEDIUM),
                graded(PriceConfidence.LOW),
                func.count().filter(ImpliedPrice.price.is_(None)),
            ).where(*in_scope)
        )
    ).one()
    return PriceRebuild(high=written[0], medium=written[1], low=written[2], unpriced=written[3])


async def held_in(session: AsyncSession, scope: Scope) -> frozenset[SecurityPeriod]:
    """Every ``(security, period)`` the snapshot holds in ``scope``'s ``(filer, period)`` pairs.

    Read before a rebuild and after it, and the two together are the prices
    the rebuild can have changed. Not for :data:`~app.derived.scope.EVERYTHING`,
    where that is every price, and :func:`recompute_implied_price` takes
    ``None`` instead of a list of all of them.
    """
    rows = await session.execute(
        select(PositionSnapshot.security_id, PositionSnapshot.period_of_report)
        .where(*scope.covers(PositionSnapshot.filer_id, PositionSnapshot.period_of_report))
        .distinct()
    )
    return frozenset(cast(Iterable[SecurityPeriod], rows.tuples()))


def _keyed(
    keys: frozenset[SecurityPeriod] | None,
    security_id: SQLColumnExpression[Any],
    period_of_report: SQLColumnExpression[Any],
) -> list[ColumnElement[bool]]:
    """A ``WHERE`` for the rows at ``keys``, to splat into ``.where(*...)``. Empty for every row."""
    if keys is None:
        return []
    ordered = sorted(keys)
    pairs = (
        func.unnest(
            literal([security for security, _ in ordered], ARRAY(BigInteger)),
            literal([period for _, period in ordered], ARRAY(Date)),
        )
        .table_valued("security_id", "period_of_report")
        .render_derived(name="keys")
    )
    return [
        tuple_(security_id, period_of_report).in_(
            select(pairs.c.security_id, pairs.c.period_of_report)
        )
    ]
