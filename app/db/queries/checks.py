"""``check-data``: what to look at before publishing, across filings.

The guards in :mod:`app.ingestion.normalisation` judge one filing against its
own cover page. These judge filings against each other: a period against the
quarter before it, a position against the portfolio it sits in, a filer's
quarters against the calendar. None of them can run at parse time, because each
one needs a filing other than the one being parsed.

Unlike the guards, most of these fire legitimately. A big enough stock split
looks exactly like a manager buying a hundred times more, until you look at the
price. A
holding company really does keep nine tenths of its book in one name. A
manager really does drop below the $100M threshold for a year. A finding is not
a verdict. It says someone should look, and the command's exit code is what
makes sure someone does.

What each check reads
---------------------
The concentration and position-jump checks read
:func:`~app.derived.position_snapshot.snapshot_positions`, live: exactly what
the next ``recompute`` would publish, so a finding is about data someone is
about to be shown. The suspect-period check lists the periods that query
withholds. The gap check reads ``filing``, because a gap is about what was
filed rather than what was published, and a quarter whose only filing is
suspect is a suspect period, not a missing one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, Integer, cast, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filer import Filer
from app.db.models.filing import LOADED_STATUSES, Filing, ParseStatus
from app.db.models.security import Security
from app.db.queries.effective import EFFECTIVE_FILING
from app.derived.position_snapshot import snapshot_positions

#: Above this share of its period's value, one position is the period. Fires for
#: a holding company, for a fund that reports one stake, and for a period in
#: which most of the portfolio went missing — which is the one worth catching.
CONCENTRATION_THRESHOLD: Final = Decimal("0.9")

#: A quarter-on-quarter change in shares above this — 10,000%, as a fraction —
#: is a finding. Splits are why it exists: shares multiply and the price
#: divides, so a split looks exactly like a position bought many times over
#: until you look at the price. Only an extreme one crosses this line, though —
#: 4-for-1 is +300% and 20-for-1 is +1,900% — and what does cross it looks the
#: same: a share count read from the wrong column, a stub position rebuilt. The
#: finding shows the price on both sides, and the price is what tells them apart.
MAX_QUARTERLY_CHANGE: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class SuspectFiling:
    """One suspect filing that counts toward its period."""

    accession_no: str
    form_type: str
    failed: tuple[str, ...]
    """The guards that failed, each once, in the order its notes were written."""


@dataclass(frozen=True, slots=True)
class SuspectPeriod:
    """A ``(filer, period)`` that a suspect filing counts toward.

    Exactly the periods ``position_snapshot`` withholds. A suspect filing that
    no longer counts is not one of them: once a later restatement has replaced
    it, nothing reads it, and the period is published from the restatement.
    """

    slug: str
    period: date
    filings: tuple[SuspectFiling, ...]


@dataclass(frozen=True, slots=True)
class ConcentratedPeriod:
    """A ``(filer, period)`` in which one position is most of the portfolio."""

    slug: str
    period: date
    cusip: str
    name: str | None
    sshprnamt_type: str
    value_usd: Decimal
    weight: Decimal
    period_value: Decimal
    """The period's total, option lines left out, as the weight was computed."""
    positions: int


@dataclass(frozen=True, slots=True)
class PositionJump:
    """A position whose share count grew by more than :data:`MAX_QUARTERLY_CHANGE`."""

    slug: str
    period: date
    previous_period: date
    cusip: str
    name: str | None
    sshprnamt_type: str
    put_call: str | None
    shares_before: Decimal
    shares_after: Decimal
    value_before: Decimal
    value_after: Decimal

    @property
    def change(self) -> Decimal:
        """``(after - before) / before``: 100 is 10,000%."""
        return (self.shares_after - self.shares_before) / self.shares_before

    @property
    def price_before(self) -> Decimal | None:
        return _price(self.value_before, self.shares_before)

    @property
    def price_after(self) -> Decimal | None:
        return _price(self.value_after, self.shares_after)


@dataclass(frozen=True, slots=True)
class FilingGap:
    """Consecutive quarters with no loaded 13F, between two that have one."""

    slug: str
    missing: tuple[str, ...]
    """The quarters, ``2023Q1`` and on, in order."""
    after: str
    """The last quarter before the gap with a loaded filing."""
    before: str
    """The first quarter after it with one."""
    unloaded: int
    """13Fs on file for the missing quarters that are ``pending`` or ``failed``.

    Nonzero means the filings were found and did not load, so the next step is
    ``backfill``. Zero means nothing was found, so the next step is EDGAR.
    """


@dataclass(frozen=True, slots=True)
class DataCheckReport:
    """Everything ``check-data`` found. Clean only when every check found nothing."""

    suspect_periods: tuple[SuspectPeriod, ...]
    concentrated: tuple[ConcentratedPeriod, ...]
    jumps: tuple[PositionJump, ...]
    gaps: tuple[FilingGap, ...]
    include_suspect: bool
    """Whether the snapshot checks read suspect periods too, as
    ``recompute --include-suspect`` would publish them."""

    @property
    def findings(self) -> int:
        return len(self.suspect_periods) + len(self.concentrated) + len(self.jumps) + len(self.gaps)

    @property
    def clean(self) -> bool:
        return self.findings == 0


async def check_data(
    session: AsyncSession, *, filer_id: int | None = None, include_suspect: bool = False
) -> DataCheckReport:
    """Run every check, over every filer or one.

    :param include_suspect: Read the snapshot as ``recompute --include-suspect``
        would build it. Suspect periods are listed either way; this decides
        whether their positions are also checked, which is worth doing before
        publishing them.
    """
    return DataCheckReport(
        suspect_periods=tuple(await suspect_periods(session, filer_id=filer_id)),
        concentrated=tuple(
            await concentrated_periods(session, filer_id=filer_id, include_suspect=include_suspect)
        ),
        jumps=tuple(
            await position_jumps(session, filer_id=filer_id, include_suspect=include_suspect)
        ),
        gaps=tuple(await filing_gaps(session, filer_id=filer_id)),
        include_suspect=include_suspect,
    )


async def suspect_periods(
    session: AsyncSession, *, filer_id: int | None = None
) -> list[SuspectPeriod]:
    """Every ``(filer, period)`` a suspect filing counts toward, oldest period first."""
    view = EFFECTIVE_FILING
    statement = (
        select(
            Filer.slug,
            view.c.period_of_report,
            Filing.accession_no,
            Filing.form_type,
            Filing.parse_notes,
        )
        .select_from(view)
        .join(Filing, Filing.id == view.c.filing_id)
        .join(Filer, Filer.id == view.c.filer_id)
        .where(Filing.parse_status == ParseStatus.SUSPECT.value)
        .order_by(Filer.slug, view.c.period_of_report, Filing.filed_at, Filing.accession_no)
    )
    if filer_id is not None:
        statement = statement.where(view.c.filer_id == filer_id)

    periods: dict[tuple[str, date], list[SuspectFiling]] = defaultdict(list)
    for row in await session.execute(statement):
        periods[(row.slug, row.period_of_report)].append(
            SuspectFiling(
                accession_no=row.accession_no,
                form_type=row.form_type,
                failed=_failed(row.parse_notes),
            )
        )
    return [
        SuspectPeriod(slug=slug, period=period, filings=tuple(filings))
        for (slug, period), filings in periods.items()
    ]


async def concentrated_periods(
    session: AsyncSession, *, filer_id: int | None = None, include_suspect: bool = False
) -> list[ConcentratedPeriod]:
    """Every ``(filer, period)`` whose largest position exceeds :data:`CONCENTRATION_THRESHOLD`.

    At most one position per period can, since the weights of a period sum to
    one. Option lines carry no weight, so they are never the finding.
    """
    snapshot = snapshot_positions(include_suspect=include_suspect, filer_id=filer_id).subquery()
    totals = (
        select(
            snapshot.c.filer_id,
            snapshot.c.period_of_report,
            func.sum(snapshot.c.value_usd).filter(snapshot.c.put_call.is_(None)).label("value"),
            func.count().filter(snapshot.c.put_call.is_(None)).label("positions"),
        )
        .group_by(snapshot.c.filer_id, snapshot.c.period_of_report)
        .subquery()
    )
    statement = (
        select(
            Filer.slug,
            snapshot.c.period_of_report,
            snapshot.c.cusip,
            Security.name,
            snapshot.c.sshprnamt_type,
            snapshot.c.value_usd,
            snapshot.c.weight,
            totals.c.value.label("period_value"),
            totals.c.positions,
        )
        .join(
            totals,
            (totals.c.filer_id == snapshot.c.filer_id)
            & (totals.c.period_of_report == snapshot.c.period_of_report),
        )
        .join(Filer, Filer.id == snapshot.c.filer_id)
        .join(Security, Security.id == snapshot.c.security_id)
        .where(snapshot.c.weight > CONCENTRATION_THRESHOLD)
        .order_by(Filer.slug, snapshot.c.period_of_report)
    )
    return [
        ConcentratedPeriod(
            slug=row.slug,
            period=row.period_of_report,
            cusip=row.cusip,
            name=row.name,
            sshprnamt_type=row.sshprnamt_type,
            value_usd=row.value_usd,
            weight=row.weight,
            period_value=row.period_value,
            positions=row.positions,
        )
        for row in await session.execute(statement)
    ]


async def position_jumps(
    session: AsyncSession, *, filer_id: int | None = None, include_suspect: bool = False
) -> list[PositionJump]:
    """Every position whose shares grew by more than :data:`MAX_QUARTERLY_CHANGE` in a quarter.

    Compared against the calendar quarter immediately before, and only when
    the filer has a position there: a new position has no percentage change,
    and a comparison across a gap is not quarter-on-quarter — the gap check
    reports the gap. Like with like, on the natural key, so shares are never
    compared with a principal amount, nor an option with its underlying.

    Growth only. A position can fall by at most 100%, so no fall exceeds the
    threshold. A reverse split is a fall, and so is a manager selling down to a
    stub.
    """
    snapshot = snapshot_positions(include_suspect=include_suspect, filer_id=filer_id).cte()
    now = snapshot.alias("now")
    before = snapshot.alias("before")
    statement = (
        select(
            Filer.slug,
            now.c.period_of_report,
            before.c.period_of_report.label("previous_period"),
            now.c.cusip,
            Security.name,
            now.c.sshprnamt_type,
            now.c.put_call,
            before.c.shares.label("shares_before"),
            now.c.shares.label("shares_after"),
            before.c.value_usd.label("value_before"),
            now.c.value_usd.label("value_after"),
        )
        .select_from(now)
        .join(
            before,
            (before.c.filer_id == now.c.filer_id)
            & (before.c.cusip == now.c.cusip)
            & before.c.put_call.is_not_distinct_from(now.c.put_call)
            & (before.c.sshprnamt_type == now.c.sshprnamt_type)
            & (
                _quarter_index(before.c.period_of_report)
                == _quarter_index(now.c.period_of_report) - 1
            ),
        )
        .join(Filer, Filer.id == now.c.filer_id)
        .join(Security, Security.id == now.c.security_id)
        .where(before.c.shares > 0)
        .where(now.c.shares - before.c.shares > before.c.shares * MAX_QUARTERLY_CHANGE)
        .order_by(Filer.slug, now.c.period_of_report, now.c.cusip)
    )
    return [
        PositionJump(
            slug=row.slug,
            period=row.period_of_report,
            previous_period=row.previous_period,
            cusip=row.cusip,
            name=row.name,
            sshprnamt_type=row.sshprnamt_type,
            put_call=row.put_call,
            shares_before=row.shares_before,
            shares_after=row.shares_after,
            value_before=row.value_before,
            value_after=row.value_after,
        )
        for row in await session.execute(statement)
    ]


async def filing_gaps(session: AsyncSession, *, filer_id: int | None = None) -> list[FilingGap]:
    """Every run of quarters with no loaded 13F, between a filer's first and last.

    Any 13F form counts, a ``13F-NT`` included: a manager whose holdings are
    reported by someone else has still filed. Quarters before a filer's first
    loaded filing and after its last are not gaps. The first is where the
    history starts, and the second is either a manager who stopped filing or a
    quarter that is not due yet.
    """
    statement = (
        select(Filer.slug, Filing.period_of_report, Filing.parse_status)
        .join(Filer, Filer.id == Filing.filer_id)
        .where(Filing.form_type.startswith("13F"), Filing.period_of_report.is_not(None))
        .order_by(Filer.slug)
    )
    if filer_id is not None:
        statement = statement.where(Filing.filer_id == filer_id)

    loaded: dict[str, set[int]] = defaultdict(set)
    unloaded: dict[str, defaultdict[int, int]] = defaultdict(lambda: defaultdict(int))
    for row in await session.execute(statement):
        quarter = _quarter_of(row.period_of_report)
        if row.parse_status in LOADED_STATUSES:
            loaded[row.slug].add(quarter)
        else:
            unloaded[row.slug][quarter] += 1

    gaps: list[FilingGap] = []
    for slug, quarters in loaded.items():
        # The last quarter always has a filing, so every run ends at one.
        run: list[int] = []
        for quarter in range(min(quarters), max(quarters) + 1):
            if quarter not in quarters:
                run.append(quarter)
            elif run:
                gaps.append(
                    FilingGap(
                        slug=slug,
                        missing=tuple(_quarter_label(missing) for missing in run),
                        after=_quarter_label(run[0] - 1),
                        before=_quarter_label(run[-1] + 1),
                        unloaded=sum(unloaded[slug][missing] for missing in run),
                    )
                )
                run = []
    return gaps


def _failed(notes: list[dict[str, Any]] | None) -> tuple[str, ...]:
    """The kinds of a filing's ``parse_notes`` that are not warnings, each once.

    A note written before severity was stored has none, and is counted: every
    kind but ``dropped_row`` was an error then too, and one ``dropped_row`` too
    many in a summary line costs nothing.
    """
    failed: dict[str, None] = {}
    for note in notes or ():
        if note.get("severity") != "warning":
            failed.setdefault(str(note.get("kind")), None)
    return tuple(failed)


def _price(value: Decimal, shares: Decimal) -> Decimal | None:
    return value / shares if shares and value else None


def _quarter_of(period: date) -> int:
    """Quarters since year 0, so that consecutive quarters differ by one."""
    return period.year * 4 + (period.month - 1) // 3


def _quarter_label(quarter: int) -> str:
    """The inverse of :func:`_quarter_of`, spelled as ``filing.quarter`` is: ``2024Q1``."""
    return f"{quarter // 4}Q{quarter % 4 + 1}"


def _quarter_index(period: ColumnElement[date]) -> ColumnElement[int]:
    """:func:`_quarter_of`, in SQL."""
    return (
        cast(extract("year", period), Integer) * 4 + cast(extract("quarter", period), Integer) - 1
    )
