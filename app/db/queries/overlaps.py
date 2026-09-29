"""Evidence for a filer's overlap policy, from the holdings themselves.

When two of a filer's CIKs both report a period, ``filer.overlap`` decides
whether one of them counts or both do (see
:class:`~app.db.models.filer.OverlapPolicy`). The policy is an editorial call;
this module checks it against the filings.

The test is **matching share counts**, not shared CUSIPs. Two books from the
same shop overlap heavily in *names* — two quant advisers both hold the S&P —
but independent books almost never report the same number of shares of the
same security. One book filed twice reports them identically. So, per pair:
the share of value sitting in positions both filings report with share counts
within :data:`SHARE_TOLERANCE` of each other.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filer import Filer, FilerCik, OverlapPolicy
from app.db.models.holding import Holding
from app.db.queries.effective import EFFECTIVE_FILING_BY_CIK

#: Share counts this close are "the same position". Not zero: a book refiled by
#: a new entity sometimes differs by a rounding lot or a late-booked trade.
SHARE_TOLERANCE: Final = Decimal("0.005")

#: At or above this, the pair is one book filed twice.
SAME_BOOK_AT: Final = 0.9

#: At or below this, the pair is two independent books.
SEPARATE_BOOKS_AT: Final = 0.1

_Position = tuple[str, str | None, str]
"""``(cusip, put_call, sshprnamt_type)``: the holding's natural key, minus the filing."""


class Verdict(StrEnum):
    SAME_BOOK = "same book"
    SEPARATE_BOOKS = "separate books"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class _Book:
    positions: dict[_Position, tuple[Decimal, Decimal]]
    """Position -> (shares, value_usd), summed over the CIK's effective filings."""

    @property
    def value(self) -> Decimal:
        return sum((value for _, value in self.positions.values()), start=Decimal(0))


@dataclass(frozen=True, slots=True)
class OverlapFinding:
    """One period in which two of a filer's CIKs both filed, compared."""

    slug: str
    period: date
    policy: OverlapPolicy
    primary_cik: str
    """The CIK the ``successor`` policy keeps: the highest priority."""
    other_cik: str
    primary_positions: int
    other_positions: int
    primary_value: Decimal
    other_value: Decimal
    identical_share: float
    """Fraction of the pair's combined value in positions both report with the
    same share count. Near 1: one book twice. Near 0: two books."""

    @property
    def verdict(self) -> Verdict:
        if self.identical_share >= SAME_BOOK_AT:
            return Verdict.SAME_BOOK
        if self.identical_share <= SEPARATE_BOOKS_AT:
            return Verdict.SEPARATE_BOOKS
        return Verdict.UNCLEAR

    @property
    def conflict(self) -> str | None:
        """What the current policy gets wrong for this period, if the verdict is clear."""
        if self.verdict is Verdict.SAME_BOOK and self.policy is OverlapPolicy.SUM:
            return "policy sums one book filed twice — the period is double counted"
        if self.verdict is Verdict.SEPARATE_BOOKS and self.policy is OverlapPolicy.SUCCESSOR:
            return (
                f"policy keeps {self.primary_cik} only — drops ${self.other_value:,.0f} "
                "reported by the other CIK; consider overlap: sum"
            )
        return None


async def audit_overlaps(session: AsyncSession, *, slug: str | None = None) -> list[OverlapFinding]:
    """Every overlapping ``(filer, period)``, compared pair by pair.

    Reads :data:`EFFECTIVE_FILING_BY_CIK` — after amendments, before the
    overlap policy — so each CIK is represented by what it would contribute on
    its own. With three or more CIKs in one period, each is compared against the
    primary.
    """
    view = EFFECTIVE_FILING_BY_CIK
    overlapping = (
        select(view.c.filer_id, view.c.period_of_report)
        .group_by(view.c.filer_id, view.c.period_of_report)
        .having(func.count(view.c.cik.distinct()) > 1)
        .subquery()
    )
    statement = (
        select(
            Filer.slug,
            Filer.overlap,
            view.c.period_of_report,
            view.c.cik,
            func.coalesce(FilerCik.priority, -1).label("priority"),
            Holding.cusip,
            Holding.put_call,
            Holding.sshprnamt_type,
            func.sum(Holding.shares).label("shares"),
            func.sum(Holding.value_usd).label("value_usd"),
        )
        .join(
            overlapping,
            (overlapping.c.filer_id == view.c.filer_id)
            & (overlapping.c.period_of_report == view.c.period_of_report),
        )
        .join(Filer, Filer.id == view.c.filer_id)
        .outerjoin(FilerCik, (FilerCik.cik == view.c.cik) & (FilerCik.filer_id == view.c.filer_id))
        .join(Holding, Holding.filing_id == view.c.filing_id)
        .group_by(
            Filer.slug,
            Filer.overlap,
            view.c.period_of_report,
            view.c.cik,
            FilerCik.priority,
            Holding.cusip,
            Holding.put_call,
            Holding.sshprnamt_type,
        )
    )
    if slug is not None:
        statement = statement.where(Filer.slug == slug)

    books: dict[tuple[str, str, date], dict[tuple[int, str], _Book]] = defaultdict(dict)
    for row in await session.execute(statement):
        periods = books[(row.slug, row.overlap, row.period_of_report)]
        book = periods.setdefault((row.priority, row.cik), _Book(positions={}))
        book.positions[(row.cusip, row.put_call, row.sshprnamt_type)] = (row.shares, row.value_usd)

    findings = []
    for (filer_slug, overlap, period), by_cik in sorted(books.items()):
        # Same order the effective_filing view ranks by: priority, then CIK.
        primary_key, *others = sorted(by_cik, reverse=True)
        primary_cik = primary_key[1]
        primary = by_cik[primary_key]
        for key in others:
            other = by_cik[key]
            findings.append(
                OverlapFinding(
                    slug=filer_slug,
                    period=period,
                    policy=OverlapPolicy(overlap),
                    primary_cik=primary_cik,
                    other_cik=key[1],
                    primary_positions=len(primary.positions),
                    other_positions=len(other.positions),
                    primary_value=primary.value,
                    other_value=other.value,
                    identical_share=_identical_share(primary, other),
                )
            )
    return findings


def _identical_share(a: _Book, b: _Book) -> float:
    """Value in positions both books report with matching shares, over their union.

    The union takes each position at the larger of its two values, so a
    position only one book holds counts fully against the match and a matched
    position is not counted twice.
    """
    union = Decimal(0)
    identical = Decimal(0)
    for position in a.positions.keys() | b.positions.keys():
        shares_a, value_a = a.positions.get(position, (Decimal(0), Decimal(0)))
        shares_b, value_b = b.positions.get(position, (Decimal(0), Decimal(0)))
        larger = max(value_a, value_b)
        union += larger
        if (
            shares_a
            and shares_b
            and abs(shares_a - shares_b) <= SHARE_TOLERANCE * max(shares_a, shares_b)
        ):
            identical += larger
    return float(identical / union) if union else 0.0
