"""``position_snapshot``: the published portfolio, and the one query that builds it.

``recompute`` writes the table from :func:`snapshot_positions`. ``check-data``
reads the same query live, so that it checks exactly what the next ``recompute``
would publish. That shared query is the point of this module: a check run over
a portfolio derived some other way would be checking something nobody reads.

The query, in three steps
-------------------------
``holding`` is what was filed. ``position_snapshot`` is what is true. The
difference between them is amendment resolution, and the query does it in
three named CTEs:

1. ``winning_filings``: for each ``(filer, period)``, the filings that count,
   and whether any of them is suspect. Which filings count is read from
   ``effective_filing``, where the rules live: the latest restatement alone, or
   the original plus the new-holdings amendments filed after it, per CIK and
   then by the filer's overlap policy. Restating those rules here would give a
   second answer to which filings count, and the two could drift apart.
2. ``agg``: their holdings summed per ``(filer, period, security)``, common
   stock only, with the latest-filed filing behind each sum.
3. The final select adds ``weight_pct`` with ``SUM(value_usd) OVER (PARTITION
   BY filer_id, period_of_report)``, which puts each period's total on every
   row of the period in the same pass that reads them. A self-join that does
   the same, grouping ``agg`` again by period and joining the totals back on,
   reads ``agg`` twice. It reads from a spooled copy when the CTE is
   materialized, and computes ``agg`` all over again when it is not.

What is withheld, and why the whole period
------------------------------------------
A ``(filer, period)`` is published only when every filing that counts toward it
passed its guards. One suspect filing among them withholds the whole period,
whether it is the original, a restatement, one addition, or one CIK's filing
among several. The rest of the period is not a smaller correct answer. An
original without the new-holdings amendment that followed it is the portfolio
before confidential treatment expired. One CIK of two under a ``sum`` policy is
half the book. Either would read, a quarter later, as a manager who bought.

Withholding is not falling back, either. A suspect restatement does not put the
original it replaced back into the snapshot: the original was restated because
it was wrong.

``--include-suspect`` publishes those periods anyway and marks every row in
them ``suspect``. That way, what was published without a check can always be
told apart from what was published with one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import CTE, Select, delete, distinct, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filing import Filing, ParseStatus
from app.db.models.holding import Holding
from app.db.models.position_snapshot import PositionSnapshot
from app.db.queries.effective import EFFECTIVE_FILING

#: :func:`snapshot_positions`'s columns, in order, as ``position_snapshot`` names them.
SNAPSHOT_COLUMNS: Final = (
    "filer_id",
    "period_of_report",
    "security_id",
    "shares",
    "value_usd",
    "weight_pct",
    "source_filing_id",
    "suspect",
)


@dataclass(frozen=True, slots=True)
class SnapshotRebuild:
    """What one :func:`recompute_position_snapshot` published, and what it held back."""

    filers: int
    periods: int
    positions: int
    suspect_periods: int
    """Periods a suspect filing counts toward: withheld, or with
    :attr:`include_suspect`, published and marked."""
    include_suspect: bool

    @property
    def withheld(self) -> int:
        return 0 if self.include_suspect else self.suspect_periods


def snapshot_positions(
    *, include_suspect: bool = False, filer_id: int | None = None
) -> Select[Any]:
    """The rows ``position_snapshot`` holds: one per security per ``(filer, period)``.

    :param include_suspect: Keep the periods a suspect filing counts toward.
        Their rows come back with ``suspect`` true; without this they do not
        come back at all.
    :param filer_id: One filer's periods instead of every filer's.
    :returns: A select of :data:`SNAPSHOT_COLUMNS`, unordered. ``weight_pct``
        is unrounded here, and rounded to six places when stored.
    """
    winning = _winning_filings(filer_id).cte("winning_filings")
    agg = _agg(winning, include_suspect=include_suspect).cte("agg")

    period_total = func.sum(agg.c.value_usd).over(
        partition_by=(agg.c.filer_id, agg.c.period_of_report)
    )
    return select(
        agg.c.filer_id,
        agg.c.period_of_report,
        agg.c.security_id,
        agg.c.shares,
        agg.c.value_usd,
        # Multiplied before dividing, which is exact in numeric. Null for a
        # period worth nothing, the one period with no total to divide by.
        (agg.c.value_usd * 100 / func.nullif(period_total, 0)).label("weight_pct"),
        agg.c.source_filing_id,
        agg.c.suspect,
    )


async def recompute_position_snapshot(
    session: AsyncSession, *, filer_id: int | None = None, include_suspect: bool = False
) -> SnapshotRebuild:
    """Replace ``position_snapshot`` — all of it, or one filer's rows — from ``holding``.

    Delete and reinsert in the caller's transaction, which also commits it. A
    reader sees the old snapshot until then, and the new one after, and never a
    half-built one in between. ``DELETE`` rather than ``TRUNCATE`` for exactly
    that: ``TRUNCATE`` takes a lock that blocks every reader of the table for as
    long as the rebuild runs.

    :param filer_id: Rebuild this filer's rows and leave every other filer's
        alone, as they were last built.
    :param include_suspect: Publish the periods a suspect filing counts toward,
        marked ``suspect``, instead of withholding them.
    """
    scope = [] if filer_id is None else [PositionSnapshot.filer_id == filer_id]

    await session.execute(delete(PositionSnapshot).where(*scope))
    await session.execute(
        insert(PositionSnapshot).from_select(
            SNAPSHOT_COLUMNS,
            snapshot_positions(include_suspect=include_suspect, filer_id=filer_id),
        )
    )

    published = (
        await session.execute(
            select(
                func.count(),
                func.count(
                    distinct(tuple_(PositionSnapshot.filer_id, PositionSnapshot.period_of_report))
                ),
                func.count(distinct(PositionSnapshot.filer_id)),
            ).where(*scope)
        )
    ).one()
    winning = _winning_filings(filer_id).subquery()
    suspect_periods = await session.scalar(
        select(func.count(distinct(tuple_(winning.c.filer_id, winning.c.period_of_report)))).where(
            winning.c.suspect
        )
    )

    return SnapshotRebuild(
        filers=published[2],
        periods=published[1],
        positions=published[0],
        suspect_periods=suspect_periods or 0,
        include_suspect=include_suspect,
    )


def _winning_filings(filer_id: int | None) -> Select[Any]:
    """Each filing that counts toward its ``(filer, period)``, and whether the period is suspect.

    ``suspect`` is the same on every filing of a period: true when any filing
    that counts toward it is suspect. A window rather than a ``GROUP BY``, to
    keep one row per filing for the holdings to join to. It is decided here,
    before any holding is read, so a suspect filing withholds its period even
    when none of its own lines is common stock.
    """
    view = EFFECTIVE_FILING
    statement = (
        select(
            view.c.filing_id,
            view.c.filer_id,
            view.c.period_of_report,
            Filing.filed_at,
            Filing.accession_no,
            func.bool_or(Filing.parse_status == ParseStatus.SUSPECT.value)
            .over(partition_by=(view.c.filer_id, view.c.period_of_report))
            .label("suspect"),
        )
        .select_from(view)
        .join(Filing, Filing.id == view.c.filing_id)
    )
    return statement if filer_id is None else statement.where(view.c.filer_id == filer_id)


def _agg(winning: CTE, *, include_suspect: bool) -> Select[Any]:
    """The winning filings' common stock, summed per security per period.

    A security held by several of the period's filings is one row: an original
    and the amendment that added to it, or two CIKs under a ``sum`` policy.
    ``source_filing_id`` is the latest filed of them, which is the one that last
    changed the number. Within one filing, lines sharing a CUSIP were already
    summed by the loader, on ``holding``'s natural key.
    """
    latest_filed_first = aggregate_order_by(
        winning.c.filing_id, winning.c.filed_at.desc(), winning.c.accession_no.desc()
    )
    statement = (
        select(
            winning.c.filer_id,
            winning.c.period_of_report,
            Holding.security_id,
            func.sum(Holding.shares).label("shares"),
            func.sum(Holding.value_usd).label("value_usd"),
            func.array_agg(latest_filed_first)[1].label("source_filing_id"),
            func.bool_or(winning.c.suspect).label("suspect"),
        )
        .select_from(winning)
        .join(Holding, Holding.filing_id == winning.c.filing_id)
        # Common stock. An option line's value is the notional of its
        # underlying, and a PRN line counts principal, not shares.
        .where(Holding.put_call.is_(None), Holding.sshprnamt_type == "SH")
        .group_by(winning.c.filer_id, winning.c.period_of_report, Holding.security_id)
    )
    # A WHERE runs before the window, but it removes whole periods, and a
    # period's total is summed within the period, so no other weight moves.
    return statement if include_suspect else statement.where(winning.c.suspect.is_(False))
