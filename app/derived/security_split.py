"""``security_split``: stock splits, detected from what the holders filed.

A split changes every holder's share count by the same ratio, and the price by
its inverse, with nobody trading. Nothing in a 13F says a split happened, and
there is no corporate-action feed. But a quarter's filings, read together, show
it: the holders' share counts all moved by about the same common ratio, and the
implied price (:mod:`app.derived.implied_price`) moved the other way. This
module finds those quarters, and ``position_change`` adjusts across them.

The evidence, per ``(security, period)``
----------------------------------------
Each filer that held the security with shares in this quarter and the one
before is a **holder**, and its **shares ratio** is this quarter's shares over
last quarter's. :func:`split_candidates` computes, in one query:

1. The median holder's ratio, snapped to the nearest of
   :data:`SPLIT_FRACTIONS` if one is within :data:`AGREEMENT_TOLERANCE` of it.
   Nearest, so a 5.4 is 5-for-1 and a 25.3 is 25-for-1. The median, so that a
   few holders who doubled their position cannot pick the ratio. With none
   near enough, there is no candidate.
2. The holders **agreeing**: those whose ratio is within the tolerance of the
   fraction. Not exactly on it: hedge funds trade every quarter, and of AMZN's
   35 holders across its 20-for-1, two moved by exactly 20 and nineteen by
   16 to 25.
3. The holders **exact to the share**: shares now within one share of the
   shares before times the fraction. A holder that did not trade. Rebalancing
   lands on the ratio by chance, but not to the share.
4. The **price ratio**: the implied price now over the one a quarter before.

:func:`judge` decides from those, in Python, where the rule can be read.

The rule
--------
At least :data:`MIN_AGREEING` holders agree, so one holder can never make a
split, and at least :data:`MIN_AGREEING_SHARE` of all of them, so two holders
out of eleven cannot either. Then the price has to corroborate the ratio. ``fraction * price_ratio``
is one plus the stock's return over the quarter, and has to be within
:data:`PRODUCT_BAND`. It also has to be nearer one than the fraction is: the
price is better explained by the split and an ordinary quarter than by no split
and an extraordinary one.

That is enough for a large ratio. For a small one it is not, because ordinary
trading imitates it. A manager who keeps a stock at a fixed weight trims a
quarter of the position when the price rises a third, and that is a shares
ratio of 0.75 with a price ratio that corroborates 2-for-3. On five years of
100 hedge funds, the rule above found over 900 splits of 3-for-2, 2-for-1 and
their reverses, almost none of them real. So the smaller the ratio, the more it
asks (:data:`EXACT_NOT_REQUIRED_FROM`, :data:`MAJORITY_ENOUGH_FROM`):

* 4-for-1 or more, 1-for-4 or less: the rule above.
* 3-for-1, 1-for-3: a holder exact to the share, or at least three holders
  agreeing and a majority of them.
* 3-for-2, 2-for-1 and their reverses: a holder exact to the share.

Spin-offs, mergers, ADR ratio changes and stock dividends of an uncommon ratio
move share counts without being one of these splits. They are not adjusted
for. Where the holders' shares and the price moved apart by
:data:`OUTLIER_FACTOR` or more and no split was detected, the quarter is an
**outlier**, which ``detect-splits`` lists for a person to look at.

What a rebuild touches
----------------------
A split belongs to a ``(security, period)``, as an implied price does, and is
decided by every holder together. So it is rebuilt on the price's keys, and on
the quarter after each: the detection at Q+1 compares Q+1 with Q, so a filing
at Q moves it. :func:`split_keys` widens the keys by that quarter. When a split
appears, goes or changes its ratio, every holder's change across it is wrong,
not only the changes of the filer whose filing moved it.
:func:`crossing_pairs` finds those ``(filer, period)`` pairs, for ``recompute``
to rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, cast

from sqlalchemy import (
    ColumnElement,
    Date,
    Integer,
    Numeric,
    Select,
    SQLColumnExpression,
    and_,
    column,
    delete,
    func,
    insert,
    literal,
    select,
    true,
    values,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.periods import is_quarter_end, next_quarter_end
from app.db.models.implied_price import ImpliedPrice
from app.db.models.position_change import PositionChange
from app.db.models.position_snapshot import PositionSnapshot
from app.db.models.security_split import SecuritySplit, SplitConfidence
from app.derived.implied_price import SecurityPeriod, key_table, keyed
from app.derived.scope import Pair

_FORWARD: Final = (
    Fraction(3, 2),
    Fraction(2),
    Fraction(3),
    Fraction(4),
    Fraction(5),
    Fraction(10),
    Fraction(20),
    Fraction(25),
    Fraction(50),
)

#: Every ratio a split can be detected as: the common forward splits, and the
#: reverse of each. 25-for-1 and 50-for-1 are there for BKNG and CMG. A split of
#: any other ratio is not detected, and shows up as an outlier.
SPLIT_FRACTIONS: Final = tuple(sorted((*_FORWARD, *(1 / fraction for fraction in _FORWARD))))

#: How far a holder's shares ratio may be from a fraction, as a factor either
#: way, and still agree with it: 1.25 takes 16 to 25 for 20-for-1. The median
#: ratio has to be as near for the fraction to be a candidate at all.
AGREEMENT_TOLERANCE: Final = Decimal("1.25")

#: Holders that must agree before anything is a split. Two, so that no one
#: holder's filing, however strange, makes one.
MIN_AGREEING: Final = 2

#: The share of all the holders that must agree. A third: CMG's 50-for-1 has 4
#: of 11, BKNG's 25-for-1 3 of 9. Two holders out of eleven agreeing with a 2-for-3
#: are two that happened to trim a third.
MIN_AGREEING_SHARE: Final = Fraction(1, 3)

#: How far apart, in shares, a holder's count may be from the count before times
#: the fraction and still be exact. One, for the fraction of a share a reverse
#: split pays out in cash.
EXACT_SHARES: Final = 1

#: The band ``fraction * price_ratio``, which is one plus the quarter's return,
#: must fall in. Wide, because a split can come in any quarter: SHOP's 10-for-1
#: came in 2022Q2, when it fell 54%, and DXCM's 4-for-1 when it fell 42%. The
#: tighter test is that the product is nearer one than the fraction is.
PRODUCT_BAND: Final = (Decimal("0.4"), Decimal("1.6"))

#: Ratios at least this far from one, either way, need no holder exact to the
#: share, only the agreement and the price: 4-for-1, 1-for-4 and beyond.
#: Ordinary trading does not multiply the median holder's shares by four.
EXACT_NOT_REQUIRED_FROM: Final = Fraction(4)

#: Ratios at least this far from one, and short of
#: :data:`EXACT_NOT_REQUIRED_FROM`, may have a majority of at least three
#: holders agreeing instead of an exact one: 3-for-1 and 1-for-3, where ISRG's
#: nine agreeing holders include none exact. Anything nearer one, 3-for-2,
#: 2-for-1 and their reverses, needs the exact holder: rebalancing makes those.
MAJORITY_ENOUGH_FROM: Final = Fraction(3)

#: A median shares ratio this far from one, either way, with the price moving
#: as far the other way, is a corporate action of some kind: CTAS's 4-for-1 with
#: one holder of five agreeing, an ADR ratio change, a 15-for-1. Reported as an
#: outlier when it is not detected as a split.
OUTLIER_FACTOR: Final = Decimal("2.5")


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the holders' filings say about one security across one quarter."""

    security_id: int
    period_of_report: date
    """The quarter end after the split, if there was one."""
    holders: int
    shares_ratio: Decimal
    """The median holder's shares now over its shares a quarter before."""
    fraction: Fraction | None
    """:attr:`shares_ratio` snapped to the nearest of :data:`SPLIT_FRACTIONS`,
    or ``None`` when none is within :data:`AGREEMENT_TOLERANCE`."""
    agreeing: int
    exact: int
    price_ratio: Decimal | None
    """The implied price now over the one a quarter before. ``None`` when
    either has no price."""

    @property
    def key(self) -> SecurityPeriod:
        return (self.security_id, self.period_of_report)

    @property
    def product(self) -> Decimal | None:
        """``fraction * price_ratio``: one plus the stock's return, if it split."""
        if self.fraction is None or self.price_ratio is None:
            return None
        return self.price_ratio * self.fraction.numerator / self.fraction.denominator


@dataclass(frozen=True, slots=True)
class Verdict:
    """What :func:`judge` made of a :class:`Candidate`."""

    candidate: Candidate
    confidence: SplitConfidence | None
    """``None`` when it is not a split."""
    reason: str
    """Why it is, or why not, in a few words."""

    @property
    def split(self) -> bool:
        return self.confidence is not None


@dataclass(frozen=True, slots=True)
class SplitRebuild:
    """What one :func:`recompute_security_split` found, and how it differs from before."""

    splits: tuple[Verdict, ...]
    """Every split detected at the keys, oldest first."""
    outliers: tuple[Verdict, ...]
    """Share counts and price that moved apart by :data:`OUTLIER_FACTOR` or more,
    and not a split."""
    added: frozenset[SecurityPeriod]
    removed: frozenset[SecurityPeriod]
    rerated: frozenset[SecurityPeriod]
    """Splits that were there before with another ratio."""

    @property
    def moved(self) -> frozenset[SecurityPeriod]:
        """Every key whose changes are now adjusted differently."""
        return self.added | self.removed | self.rerated


def judge(candidate: Candidate) -> Verdict:
    """Whether ``candidate`` is a split, and how sure. The rule is the module docstring's."""

    def no(reason: str) -> Verdict:
        return Verdict(candidate, None, reason)

    fraction, product = candidate.fraction, candidate.product
    if fraction is None:
        return no("no common ratio")
    least = max(Fraction(MIN_AGREEING), candidate.holders * MIN_AGREEING_SHARE)
    if candidate.agreeing < least:
        return no(f"{candidate.agreeing} of {candidate.holders} holders agree")
    if product is None:
        return no("no price on one side")
    size = max(fraction, 1 / fraction)
    if not price_corroborates(fraction, product):
        return no(f"the price moved {_times(candidate.price_ratio)}")
    majority = candidate.agreeing >= 3 and candidate.agreeing * 2 > candidate.holders
    if size < EXACT_NOT_REQUIRED_FROM and not candidate.exact:
        if size < MAJORITY_ENOUGH_FROM:
            return no("no holder exact to the share")
        if not majority:
            return no("no holder exact to the share, and no majority agreeing")
    if candidate.exact and candidate.agreeing >= 3:
        return Verdict(candidate, SplitConfidence.HIGH, "an exact holder and three agreeing")
    if candidate.exact or candidate.agreeing >= 3:
        return Verdict(candidate, SplitConfidence.MEDIUM, "an exact holder or three agreeing")
    return Verdict(candidate, SplitConfidence.LOW, "two holders agree")


def price_corroborates(fraction: Fraction, product: Decimal) -> bool:
    """Whether ``product``, ``fraction`` times the price ratio, is a split's:
    within :data:`PRODUCT_BAND`, and nearer one than the fraction is."""
    low, high = PRODUCT_BAND
    # Nearer one: product² < fraction going up, and product² > fraction going
    # down. Exact, in Fraction.
    squared = Fraction(product) ** 2
    return low <= product <= high and (squared < fraction if fraction > 1 else squared > fraction)


def nearest_fraction(shares_ratio: Decimal) -> Fraction | None:
    """The one of :data:`SPLIT_FRACTIONS` nearest ``shares_ratio``, as a factor,
    if it is within :data:`AGREEMENT_TOLERANCE`: what the median is snapped to."""
    if shares_ratio <= 0:
        return None
    ratio = Fraction(shares_ratio)
    nearest = min(SPLIT_FRACTIONS, key=lambda fraction: max(ratio / fraction, fraction / ratio))
    near = max(ratio / nearest, nearest / ratio) <= Fraction(AGREEMENT_TOLERANCE)
    return nearest if near else None


def looks_like_split(prev_shares: Decimal, shares: Decimal, price_ratio: Decimal | None) -> bool:
    """Whether one holder's change could be a split nobody has detected yet.

    :func:`judge` needs :data:`MIN_AGREEING` holders, so the first to file a
    quarter after a split publishes its shares as filed, unadjusted: a holder
    of 100 that has 1,000 after a 10-for-1 is an ``add`` of 900%. This is the
    rule for that one holder, by the detector's own tiers. Its shares moved by
    a fraction and the price corroborates it, and below
    :data:`MAJORITY_ENOUGH_FROM`, where trading imitates a split, its shares
    are exact to the share. With no price ratio, exact to the share alone.

    Or the price moved by :data:`OUTLIER_FACTOR` or more, either way, however
    the shares moved. A holder that traded across a split is no fraction:
    across NVDA's 10-for-1, Appaloosa's shares rose 56% and its price fell to
    a seventh, a sale of five sixths of the position filed as an add; across
    AVGO's, Caxton's fell 12%, a sale of nine tenths filed as a small trim.
    The price is all that gives either away.

    Leans the other way from :func:`judge`, since one holder can be no
    majority. A 3-for-1 without a holder exact to the share is a split here.
    A manager who traded a stock that lost three fifths of its price in a
    quarter, or gained one and a half times it, is indistinguishable from one,
    and is not told about until the split is detected, or ever, if there was
    none.

    :param price_ratio: The price the holder reported now, over the one before.
    """
    if prev_shares <= 0 or shares <= 0:
        return False
    shares_ratio = shares / prev_shares
    if price_ratio is not None and not 1 / OUTLIER_FACTOR < price_ratio < OUTLIER_FACTOR:
        return price_ratio > 0
    fraction = nearest_fraction(shares_ratio)
    if fraction is None:
        return False
    exact = abs(shares - prev_shares * fraction.numerator / fraction.denominator) <= EXACT_SHARES
    if price_ratio is None:
        return exact
    product = price_ratio * fraction.numerator / fraction.denominator
    if not price_corroborates(fraction, product):
        return False
    return exact or max(fraction, 1 / fraction) >= MAJORITY_ENOUGH_FROM


def is_outlier(verdict: Verdict) -> bool:
    """Not a split, but the share counts and the price moved apart like one.

    The median holder's shares by :data:`OUTLIER_FACTOR` or more, either way,
    and the price as far the other way.
    """
    candidate = verdict.candidate
    if verdict.split or candidate.price_ratio is None or candidate.holders < MIN_AGREEING:
        return False
    shares, price = candidate.shares_ratio, candidate.price_ratio
    if shares < 1:
        shares, price = 1 / shares, 1 / price
    return shares >= OUTLIER_FACTOR and price * OUTLIER_FACTOR <= 1


def split_keys(held: Iterable[SecurityPeriod]) -> frozenset[SecurityPeriod]:
    """``held``, and each key's quarter after: the splits a rebuild of ``held`` can move."""
    keys = set(held)
    keys |= {
        (security_id, next_quarter_end(period))
        for security_id, period in keys
        if is_quarter_end(period)
    }
    return frozenset(keys)


def split_candidates(keys: frozenset[SecurityPeriod] | None = None) -> Select[Any]:
    """One row per ``(security, period)`` with two or more holders across the quarter before.

    Each with the columns of :class:`Candidate`, the fraction as
    ``numerator`` and ``denominator``. Only the rows :func:`judge` can call a
    split, and those :func:`is_outlier` can report: a candidate fraction with
    enough holders agreeing, or a median ratio of :data:`OUTLIER_FACTOR` or
    more either way.

    :param keys: Only these ``(security, period)``s, the period being the
        quarter after the split. ``None`` for every one.
    """
    now = aliased(PositionSnapshot, name="now")
    before = aliased(PositionSnapshot, name="before")
    # The quarter end before: the day before this quarter began.
    previous = sql_cast(func.date_trunc("quarter", now.period_of_report), Date) - literal(1)
    ratios = (
        select(
            now.security_id,
            now.period_of_report,
            before.period_of_report.label("previous"),
            now.shares,
            before.shares.label("shares_before"),
            (_numeric(now.shares) / before.shares).label("ratio"),
        )
        .join(
            before,
            and_(
                before.filer_id == now.filer_id,
                before.security_id == now.security_id,
                before.period_of_report == previous,
            ),
        )
        .where(
            now.shares > 0,
            before.shares > 0,
            *keyed(keys, now.security_id, now.period_of_report),
        )
        .cte("ratios")
    )
    medians = (
        select(
            ratios.c.security_id,
            ratios.c.period_of_report,
            ratios.c.previous,
            func.count().label("holders"),
            func.percentile_disc(0.5).within_group(ratios.c.ratio).label("shares_ratio"),
        )
        .group_by(ratios.c.security_id, ratios.c.period_of_report, ratios.c.previous)
        .having(func.count() >= MIN_AGREEING)
        .cte("medians")
    )

    fractions = values(
        column("numerator", Integer), column("denominator", Integer), name="fractions"
    ).data([(fraction.numerator, fraction.denominator) for fraction in SPLIT_FRACTIONS])
    # How far the median is from a fraction, as a factor of one or more.
    scaled = _numeric(medians.c.shares_ratio) * fractions.c.denominator / fractions.c.numerator
    distance = func.greatest(scaled, _number(1) / scaled)
    snapped = (
        select(fractions.c.numerator, fractions.c.denominator)
        .where(distance <= _number(AGREEMENT_TOLERANCE))
        .order_by(distance)
        .limit(1)
        .lateral("snapped")
    )
    candidates = (
        select(
            medians.c.security_id,
            medians.c.period_of_report,
            medians.c.previous,
            medians.c.holders,
            medians.c.shares_ratio,
            snapped.c.numerator,
            snapped.c.denominator,
        )
        .select_from(medians)
        .outerjoin(snapped, true())
        .cte("candidates")
    )

    # Multiplied out, in exact numeric: within the tolerance of the fraction,
    # and within EXACT_SHARES of the shares before times it.
    numerator, denominator = candidates.c.numerator, candidates.c.denominator
    agreeing = func.count().filter(
        ratios.c.ratio * denominator * _number(AGREEMENT_TOLERANCE) >= numerator,
        ratios.c.ratio * denominator <= numerator * _number(AGREEMENT_TOLERANCE),
    )
    exact = func.count().filter(
        func.abs(ratios.c.shares * denominator - ratios.c.shares_before * numerator)
        <= denominator * EXACT_SHARES
    )
    counted = (
        select(
            candidates.c.security_id,
            candidates.c.period_of_report,
            candidates.c.previous,
            candidates.c.holders,
            candidates.c.shares_ratio,
            numerator,
            denominator,
            agreeing.label("agreeing"),
            exact.label("exact"),
        )
        .select_from(candidates)
        .join(
            ratios,
            and_(
                ratios.c.security_id == candidates.c.security_id,
                ratios.c.period_of_report == candidates.c.period_of_report,
            ),
        )
        .group_by(*candidates.c)
        .cte("counted")
    )

    price_now = aliased(ImpliedPrice, name="price_now")
    price_before = aliased(ImpliedPrice, name="price_before")
    big = _numeric(counted.c.shares_ratio)
    return (
        select(
            counted.c.security_id,
            counted.c.period_of_report,
            counted.c.holders,
            counted.c.shares_ratio,
            counted.c.numerator,
            counted.c.denominator,
            counted.c.agreeing,
            counted.c.exact,
            (_numeric(price_now.price) / price_before.price).label("price_ratio"),
        )
        .select_from(counted)
        .outerjoin(
            price_now,
            and_(
                price_now.security_id == counted.c.security_id,
                price_now.period_of_report == counted.c.period_of_report,
            ),
        )
        .outerjoin(
            price_before,
            and_(
                price_before.security_id == counted.c.security_id,
                price_before.period_of_report == counted.c.previous,
            ),
        )
        .where(
            (counted.c.agreeing >= MIN_AGREEING)
            | (big >= _number(OUTLIER_FACTOR))
            | (big * _number(OUTLIER_FACTOR) <= 1)
        )
    )


async def judge_candidates(
    session: AsyncSession, keys: frozenset[SecurityPeriod] | None = None
) -> list[Verdict]:
    """Every candidate at ``keys``, judged, oldest first. Writes nothing."""
    rows = await session.execute(split_candidates(keys))
    verdicts = [
        judge(
            Candidate(
                security_id=row.security_id,
                period_of_report=row.period_of_report,
                holders=row.holders,
                shares_ratio=row.shares_ratio,
                fraction=(
                    None if row.numerator is None else Fraction(row.numerator, row.denominator)
                ),
                agreeing=row.agreeing,
                exact=row.exact,
                price_ratio=row.price_ratio,
            )
        )
        for row in rows
    ]
    return sorted(verdicts, key=lambda verdict: verdict.candidate.key[::-1])


async def recompute_security_split(
    session: AsyncSession, keys: frozenset[SecurityPeriod] | None = None
) -> SplitRebuild:
    """Replace ``security_split`` — all of it, or the rows at ``keys`` — from the holders' filings.

    Reads ``position_snapshot`` and ``implied_price``, so run it after both are
    rebuilt, in the same transaction. Delete and reinsert, for the snapshot's
    reasons.

    :param keys: Detect at these ``(security, period)`` pairs and leave every
        other row alone. ``None`` detects everywhere.
    """
    if keys is not None and not keys:
        return SplitRebuild((), (), frozenset(), frozenset(), frozenset())
    in_scope = keyed(keys, SecuritySplit.security_id, SecuritySplit.period_of_report)
    stored: dict[SecurityPeriod, Fraction] = {
        (security_id, period): Fraction(numerator, denominator)
        for security_id, period, numerator, denominator in (
            await session.execute(
                select(
                    SecuritySplit.security_id,
                    SecuritySplit.period_of_report,
                    SecuritySplit.numerator,
                    SecuritySplit.denominator,
                ).where(*in_scope)
            )
        ).tuples()
    }

    verdicts = await judge_candidates(session, keys)
    splits = tuple(verdict for verdict in verdicts if verdict.split)
    detected = {
        verdict.candidate.key: cast(Fraction, verdict.candidate.fraction) for verdict in splits
    }

    await session.execute(delete(SecuritySplit).where(*in_scope))
    if splits:
        await session.execute(
            insert(SecuritySplit),
            [
                {
                    "security_id": verdict.candidate.security_id,
                    "period_of_report": verdict.candidate.period_of_report,
                    "numerator": detected[verdict.candidate.key].numerator,
                    "denominator": detected[verdict.candidate.key].denominator,
                    "confidence": cast(SplitConfidence, verdict.confidence).value,
                    "holders": verdict.candidate.holders,
                    "holders_agreeing": verdict.candidate.agreeing,
                    "holders_exact": verdict.candidate.exact,
                    "shares_ratio": verdict.candidate.shares_ratio,
                    "price_ratio": verdict.candidate.price_ratio,
                }
                for verdict in splits
            ],
        )

    both = stored.keys() & detected.keys()
    return SplitRebuild(
        splits=splits,
        outliers=tuple(verdict for verdict in verdicts if is_outlier(verdict)),
        added=frozenset(detected.keys() - stored.keys()),
        removed=frozenset(stored.keys() - detected.keys()),
        rerated=frozenset(key for key in both if stored[key] != detected[key]),
    )


async def crossing_pairs(session: AsyncSession, keys: frozenset[SecurityPeriod]) -> frozenset[Pair]:
    """The ``(filer, period)`` pairs with a change across a split at one of ``keys``.

    Read from ``position_change`` as it is, before it is rebuilt: a change
    crosses the split at ``(security, period)`` when it is that security's,
    its previous period is before the period, and its own is on or after it.
    A filer's previous period is its own, which a split detected or dropped
    does not move, so the pairs are the same before the rebuild and after it.
    """
    if not keys:
        return frozenset()
    split = key_table(keys, name="moved")
    change = PositionChange
    rows = await session.execute(
        select(change.filer_id, change.period_of_report)
        .join(
            split,
            and_(
                change.security_id == split.c.security_id,
                change.prev_period_of_report < split.c.period_of_report,
                change.period_of_report >= split.c.period_of_report,
            ),
        )
        .distinct()
    )
    return frozenset(cast(Iterable[Pair], rows.tuples()))


def _numeric(expression: SQLColumnExpression[Any]) -> ColumnElement[Decimal]:
    """``expression`` as ``numeric`` of no fixed scale.

    Without it, SQLAlchemy types arithmetic on a share count as the column's
    ``numeric(20, 4)`` and casts operands to it: a ratio of 0.00004 becomes
    0.0000, and its reciprocal a division by zero.
    """
    return sql_cast(expression, Numeric())


def _number(value: Decimal | int) -> ColumnElement[Decimal]:
    """A constant, as ``numeric`` of no fixed scale, for :func:`_numeric`'s reason."""
    return literal(value, Numeric())


def ratio_label(fraction: Fraction) -> str:
    """``20:1``, ``3:2``, ``1:10``: new shares to old."""
    return f"{fraction.numerator}:{fraction.denominator}"


def _times(ratio: Decimal | None) -> str:
    return "?" if ratio is None else f"x{ratio:.3f}"
