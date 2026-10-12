"""How much of what the tracked filers hold has a ticker: ``cusip-coverage``.

Two measures, and the second is the one that matters:

* **By row**: securities with a ticker over all securities. Easy to read and
  easy to misread. Most CUSIPs are small positions in obscure instruments, so
  this stays low long after everything that matters is resolved.
* **By value**: the aggregate holdings value of securities with a ticker over
  everyone's. This is the headline, and the gate: below
  :data:`DEFAULT_THRESHOLD_PCT`, the aggregate rankings are missing securities
  that are big enough to place in them, and nothing on the page says so.

Value is ``SUM(holding.value_usd)``, the same weight ``resolve-cusips`` orders
its work by (:func:`exposure_by_security`):
every line of every filing loaded, suspect and superseded ones included. The
published positions (``position_snapshot``) are reported beside it, since they
are what the rankings are actually computed from.

Resolved means a ticker. A security the 13(f) list named is still unresolved:
it renders as a name and a CUSIP, but nothing can look it up by ticker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.holding import Holding
from app.db.models.manual_review import ManualReview, ReviewStatus
from app.db.models.position_snapshot import PositionSnapshot
from app.db.models.security import Security
from app.db.queries.tickers import TickerCollision, ticker_collisions

#: The value coverage below which the rankings are not to be trusted.
DEFAULT_THRESHOLD_PCT: Final = Decimal(95)


@dataclass(frozen=True, slots=True)
class Suggestion:
    ticker: str
    name: str
    similarity: float


@dataclass(frozen=True, slots=True)
class UnresolvedSecurity:
    cusip: str
    name: str | None
    source: str | None
    """``13f_list`` if the list named it, else ``None``."""
    exposure: Decimal
    suggestions: tuple[Suggestion, ...]
    """Pending review rows, most similar first."""


@dataclass(frozen=True, slots=True)
class CusipCoverage:
    securities: int
    resolved: int
    by_source: dict[str | None, int]
    """Securities per ``resolution_source``, ``None`` for those nothing named."""

    value: Decimal
    resolved_value: Decimal
    published_value: Decimal
    published_resolved_value: Decimal
    delisted: int
    """Securities no longer listed (``delisted_by``), whatever their ticker."""

    delisted_value: Decimal
    collisions: tuple[TickerCollision, ...]
    """Tickers two securities hold over overlapping ranges: see
    :func:`~app.db.queries.tickers.ticker_collisions`."""

    unnamed: int
    """Securities with no name at all, which render as their CUSIP alone: no
    filing named them and neither did any source. None, so far."""

    pending_reviews: int
    pending_review_securities: int
    unresolved: tuple[UnresolvedSecurity, ...]
    """The highest-value securities without a ticker: what to resolve next."""

    @property
    def row_pct(self) -> Decimal | None:
        return _pct(Decimal(self.resolved), Decimal(self.securities))

    @property
    def value_pct(self) -> Decimal | None:
        return _pct(self.resolved_value, self.value)

    @property
    def published_value_pct(self) -> Decimal | None:
        return _pct(self.published_resolved_value, self.published_value)

    def passes(self, threshold_pct: Decimal) -> bool:
        """At or above the threshold by value. Nothing held passes: there is no
        ranking to be incomplete."""
        return self.value_pct is None or self.value_pct >= threshold_pct


def exposure_by_security() -> Select[tuple[int, Decimal]]:
    """Aggregate holdings value per security: the weight coverage is measured
    by, and the order ``resolve-cusips`` works in."""
    return select(Holding.security_id, func.sum(Holding.value_usd).label("exposure")).group_by(
        Holding.security_id
    )


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return part * 100 / whole if whole else None


async def cusip_coverage(session: AsyncSession, *, top: int = 10) -> CusipCoverage:
    """Coverage by row and by value, and the ``top`` unresolved by value."""
    exposure = exposure_by_security().subquery()
    resolved = Security.ticker.is_not(None)
    held = func.coalesce(exposure.c.exposure, 0)
    totals = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(resolved),
                func.coalesce(func.sum(held), 0),
                func.coalesce(func.sum(held).filter(resolved), 0),
                func.count().filter(func.coalesce(func.btrim(Security.name), "") == ""),
                func.count().filter(Security.delisted_by.is_not(None)),
                func.coalesce(func.sum(held).filter(Security.delisted_by.is_not(None)), 0),
            )
            .select_from(Security)
            .outerjoin(exposure, exposure.c.security_id == Security.id)
        )
    ).one()
    by_source = dict(
        (
            await session.execute(
                select(Security.resolution_source, func.count()).group_by(
                    Security.resolution_source
                )
            )
        )
        .tuples()
        .all()
    )
    published = (
        await session.execute(
            select(
                func.coalesce(func.sum(PositionSnapshot.value_usd), 0),
                func.coalesce(func.sum(PositionSnapshot.value_usd).filter(resolved), 0),
            ).join(Security, Security.id == PositionSnapshot.security_id)
        )
    ).one()
    pending = ManualReview.status == ReviewStatus.PENDING.value
    reviews = (
        await session.execute(
            select(func.count(), func.count(ManualReview.security_id.distinct())).where(pending)
        )
    ).one()

    rows = (
        await session.execute(
            select(
                Security.id,
                Security.cusip,
                Security.name,
                Security.resolution_source,
                exposure.c.exposure,
            )
            .join(exposure, exposure.c.security_id == Security.id)
            .where(Security.ticker.is_(None))
            .order_by(exposure.c.exposure.desc(), Security.cusip)
            .limit(top)
        )
    ).all()
    suggestions: dict[int, list[Suggestion]] = {}
    if rows:
        for review in (
            await session.execute(
                select(
                    ManualReview.security_id,
                    ManualReview.suggested_ticker,
                    ManualReview.suggested_name,
                    ManualReview.similarity,
                )
                .where(pending, ManualReview.security_id.in_([row.id for row in rows]))
                .order_by(ManualReview.similarity.desc(), ManualReview.id)
            )
        ).all():
            suggestions.setdefault(review.security_id, []).append(
                Suggestion(review.suggested_ticker, review.suggested_name, review.similarity)
            )

    return CusipCoverage(
        securities=totals[0],
        resolved=totals[1],
        by_source=by_source,
        value=Decimal(totals[2]),
        resolved_value=Decimal(totals[3]),
        published_value=Decimal(published[0]),
        published_resolved_value=Decimal(published[1]),
        unnamed=totals[4],
        delisted=totals[5],
        delisted_value=Decimal(totals[6]),
        collisions=tuple(await ticker_collisions(session)),
        pending_reviews=reviews[0],
        pending_review_securities=reviews[1],
        unresolved=tuple(
            UnresolvedSecurity(
                row.cusip,
                row.name,
                row.resolution_source,
                Decimal(row.exposure),
                tuple(suggestions.get(row.id, ())),
            )
            for row in rows
        ),
    )
