"""Every ``(filer, period)`` with more than one filing, and what counts in it.

A period with one filing needs no resolving. A period with two or more — an
original and its restatement, an original and the positions released from
confidential treatment months later, a notice, a second original filed by
mistake, a successor entity's filing — is where the ``effective_filing`` views
earn their keep, and where getting ``amendment_kind`` backwards is silent: a
restatement read as an addition doubles the quarter, and an addition read as a
restatement throws away everything the original reported.

**Whether a filing counts is read from** :data:`EFFECTIVE_FILING`, never worked
out again here. What this module adds is *why*: each filing that does not count
is given the reason, from its own row and from the filings that do. The two
cannot disagree about the total, because only one of them decides it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import AmendmentKind
from app.db.models.filer import Filer, OverlapPolicy
from app.db.models.filing import LOADED_STATUSES, Filing, ParseStatus
from app.db.models.holding import Holding
from app.db.queries.effective import EFFECTIVE_FILING, EFFECTIVE_FILING_BY_CIK

#: The forms that carry an information table, as the views spell them.
_ORIGINAL: Final = "13F-HR"
_AMENDMENT: Final = "13F-HR/A"


class Role(StrEnum):
    """What one filing does to its period, as resolved."""

    WHOLE_PERIOD = "whole period"
    """Counts, and is the whole period: the original, or the latest restatement."""
    ADDS = "adds"
    """Counts, on top of the whole-period filing: a ``new_holdings`` amendment."""
    REPLACED = "replaced"
    """An original or restatement that a later restatement replaced."""
    COVERED = "covered"
    """A ``new_holdings`` amendment filed before a restatement, which covers it."""
    OTHER_CIK = "other cik"
    """Would count for its CIK, but the filer's overlap policy keeps another CIK."""
    UNCLASSIFIED = "unclassified"
    """A ``13F-HR/A`` with no ``<amendmentType>``. Left out, not guessed at."""
    NO_HOLDINGS = "no holdings"
    """A form with no information table, a ``13F-NT`` above all."""
    NOT_LOADED = "not loaded"
    """``pending`` or ``failed``: no holdings in the table to count."""


@dataclass(frozen=True, slots=True)
class PeriodFiling:
    """One filing in a period, with what it contributes and why."""

    accession_no: str
    cik: str
    form_type: str
    filed_at: datetime
    amendment_no: int | None
    amendment_kind: AmendmentKind | None
    parse_status: str
    positions: int
    """Holding rows loaded for this filing, after collapsing on the natural key."""
    value_usd: Decimal
    counts: bool
    """In :data:`EFFECTIVE_FILING`. The view's answer, not this module's."""
    role: Role
    reason: str
    """The role, as a sentence naming the filing it depends on where there is one."""


@dataclass(frozen=True, slots=True)
class PeriodResolution:
    """A ``(filer, period)`` with more than one filing, and how it resolved."""

    slug: str
    period: date
    filings: tuple[PeriodFiling, ...]
    """Every filing for the period, in the order EDGAR accepted them."""

    @property
    def counting(self) -> tuple[PeriodFiling, ...]:
        return tuple(filing for filing in self.filings if filing.counts)

    @property
    def positions(self) -> int:
        """What a read through ``effective_filing`` returns for the period."""
        return sum(filing.positions for filing in self.counting)

    @property
    def value_usd(self) -> Decimal:
        return sum((filing.value_usd for filing in self.counting), start=Decimal(0))

    @property
    def resolution(self) -> str:
        """The outcome in a few words: ``restated, plus 1 addition``."""
        by_cik = _by_cik(self.counting)
        if not by_cik:
            return "nothing counts"
        if len(by_cik) > 1:
            return f"summed across {len(by_cik)} CIKs"
        (filings,) = by_cik.values()
        base = next((f for f in filings if f.role is Role.WHOLE_PERIOD), None)
        additions = sum(f.role is Role.ADDS for f in filings)
        if base is None:
            return f"{_plural(additions, 'addition')} only"
        label = "original" if base.amendment_kind is None else "restated"
        return label if not additions else f"{label}, plus {_plural(additions, 'addition')}"

    @property
    def concerns(self) -> tuple[str, ...]:
        """What in this period deserves a person's attention before it is published.

        Not every multi-filing period: a restatement, or an original plus the
        positions released from it, is the system working. These are the shapes
        where the answer is right by the rules and may still be wrong by the
        facts.
        """
        found: list[str] = []
        for filing in self.filings:
            if filing.role is Role.UNCLASSIFIED:
                found.append(
                    f"{filing.accession_no} is a 13F-HR/A with no amendmentType, so it "
                    "is left out: if it restates or adds to the period, the period is wrong"
                )
        for cik, filings in _by_cik(self.counting).items():
            if all(filing.role is Role.ADDS for filing in filings):
                found.append(
                    f"no original or restatement is loaded for CIK {cik}: "
                    "its additions are counting on their own"
                )
        for cik, filings in _by_cik(self.filings).items():
            originals = sum(filing.form_type.upper() == _ORIGINAL for filing in filings)
            if originals > 1:
                found.append(
                    f"CIK {cik} filed {originals} originals for this period: "
                    "only the latest counts, as if it restated the others"
                )
            numbers = {f.amendment_no for f in filings if f.amendment_no is not None}
            missing = sorted(set(range(1, max(numbers, default=0) + 1)) - numbers)
            if missing:
                listed = ", ".join(f"no. {number}" for number in missing)
                found.append(
                    f"amendment {listed} for CIK {cik} is not among the parsed filings: "
                    "not discovered, or not loaded yet"
                )
        return tuple(found)


async def audit_amendments(
    session: AsyncSession, *, slug: str | None = None
) -> list[PeriodResolution]:
    """Every ``(filer, period)`` with more than one 13F, each filing explained.

    Every 13F form and every parse status is listed, because a period's
    filings that do not count are half of the answer to how it resolved. Only
    filings linked to a filer: one whose CIK is unresolved has no holdings and
    no filer to resolve a period for.
    """
    shared = (
        select(Filing.filer_id, Filing.period_of_report)
        .where(Filing.filer_id.is_not(None), Filing.form_type.startswith("13F"))
        .group_by(Filing.filer_id, Filing.period_of_report)
        .having(func.count() > 1)
        .subquery()
    )
    in_shared = (Filing.filer_id == shared.c.filer_id) & (
        Filing.period_of_report == shared.c.period_of_report
    )
    held = (
        select(
            Holding.filing_id,
            func.count().label("positions"),
            func.sum(Holding.value_usd).label("value_usd"),
        )
        .join(Filing, Filing.id == Holding.filing_id)
        .join(shared, in_shared)
        .group_by(Holding.filing_id)
        .subquery()
    )
    statement = (
        select(
            Filer.slug,
            Filer.overlap,
            Filing.period_of_report,
            Filing.accession_no,
            Filing.cik,
            Filing.form_type,
            Filing.filed_at,
            Filing.amendment_no,
            Filing.amendment_kind,
            Filing.parse_status,
            func.coalesce(held.c.positions, 0).label("positions"),
            func.coalesce(held.c.value_usd, 0).label("value_usd"),
            EFFECTIVE_FILING_BY_CIK.c.filing_id.is_not(None).label("counts_for_cik"),
            EFFECTIVE_FILING.c.filing_id.is_not(None).label("counts"),
        )
        .join(shared, in_shared)
        .join(Filer, Filer.id == Filing.filer_id)
        .outerjoin(held, held.c.filing_id == Filing.id)
        .outerjoin(EFFECTIVE_FILING_BY_CIK, EFFECTIVE_FILING_BY_CIK.c.filing_id == Filing.id)
        .outerjoin(EFFECTIVE_FILING, EFFECTIVE_FILING.c.filing_id == Filing.id)
        .where(Filing.form_type.startswith("13F"))
        .order_by(Filer.slug, Filing.period_of_report, Filing.filed_at, Filing.accession_no)
    )
    if slug is not None:
        statement = statement.where(Filer.slug == slug)

    periods: dict[tuple[str, date], list[_Row]] = defaultdict(list)
    policies: dict[str, OverlapPolicy] = {}
    for row in await session.execute(statement):
        policies[row.slug] = OverlapPolicy(row.overlap)
        periods[(row.slug, row.period_of_report)].append(
            _Row(
                accession_no=row.accession_no,
                cik=row.cik,
                form_type=row.form_type,
                filed_at=row.filed_at,
                amendment_no=row.amendment_no,
                amendment_kind=row.amendment_kind,
                parse_status=row.parse_status,
                positions=row.positions,
                value_usd=Decimal(row.value_usd),
                counts_for_cik=row.counts_for_cik,
                counts=row.counts,
            )
        )

    return [
        PeriodResolution(
            slug=filer_slug,
            period=period,
            filings=_explain(rows, policies[filer_slug]),
        )
        for (filer_slug, period), rows in periods.items()
    ]


@dataclass(frozen=True, slots=True)
class _Row:
    """A filing as read, before it is explained."""

    accession_no: str
    cik: str
    form_type: str
    filed_at: datetime
    amendment_no: int | None
    amendment_kind: AmendmentKind | None
    parse_status: str
    positions: int
    value_usd: Decimal
    counts_for_cik: bool
    counts: bool

    @property
    def whole_period(self) -> bool:
        """An original, or a restatement: a filing that is the period on its own."""
        return (
            self.form_type.upper() == _ORIGINAL or self.amendment_kind is AmendmentKind.RESTATEMENT
        )


def _explain(rows: list[_Row], policy: OverlapPolicy) -> tuple[PeriodFiling, ...]:
    """Give every filing its role, taking whether it counts from the views.

    The views have already decided; the order of the tests below is only the
    order in which a reason is looked for. A filing that counts is either the
    period or an addition to it. One that counts for its CIK but not for the
    filer lost to the overlap policy. Everything else was never a candidate,
    or was one and lost to a later filing from its own CIK.
    """
    # The whole-period filing each CIK's additions stack on, per the by-CIK view.
    bases = {row.cik: row for row in rows if row.counts_for_cik and row.whole_period}
    counting_ciks = sorted({row.cik for row in rows if row.counts})

    explained = []
    for row in rows:
        base = bases.get(row.cik)
        role, reason = _role(row, base=base, policy=policy, counting_ciks=counting_ciks)
        explained.append(
            PeriodFiling(
                accession_no=row.accession_no,
                cik=row.cik,
                form_type=row.form_type,
                filed_at=row.filed_at,
                amendment_no=row.amendment_no,
                amendment_kind=row.amendment_kind,
                parse_status=row.parse_status,
                positions=row.positions,
                value_usd=row.value_usd,
                counts=row.counts,
                role=role,
                reason=reason,
            )
        )
    return tuple(explained)


def _role(
    row: _Row, *, base: _Row | None, policy: OverlapPolicy, counting_ciks: list[str]
) -> tuple[Role, str]:
    if row.counts:
        if row.whole_period:
            return Role.WHOLE_PERIOD, "counts: the whole period"
        if base is None:
            return Role.ADDS, "counts: an addition, with nothing loaded to add to"
        return Role.ADDS, f"counts: adds to {base.accession_no}"
    if row.counts_for_cik:
        return Role.OTHER_CIK, (
            f"overlap policy {policy.value}: CIK {', '.join(counting_ciks)} counts instead"
        )
    if row.parse_status not in LOADED_STATUSES:
        if row.parse_status == ParseStatus.FAILED.value:
            return Role.NOT_LOADED, "parse failed: no holdings"
        return Role.NOT_LOADED, "not parsed yet"
    if row.form_type.upper() not in (_ORIGINAL, _AMENDMENT):
        return Role.NO_HOLDINGS, f"a {row.form_type} reports no holdings"
    if row.amendment_kind is None and not row.whole_period:
        return Role.UNCLASSIFIED, "no amendmentType on the cover page: left out, not guessed"
    later = base.accession_no if base is not None else "a later restatement"
    if row.whole_period:
        return Role.REPLACED, f"replaced by {later}"
    return Role.COVERED, f"filed before {later}, which restates the whole period"


def _by_cik(filings: Iterable[PeriodFiling]) -> dict[str, list[PeriodFiling]]:
    grouped: dict[str, list[PeriodFiling]] = defaultdict(list)
    for filing in filings:
        grouped[filing.cik].append(filing)
    return dict(grouped)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
