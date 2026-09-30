"""``position_snapshot``: the published portfolio, and the one query that builds it.

``recompute`` writes the table from :func:`snapshot_positions`. ``check-data``
reads the same query live, so that it checks exactly what the next ``recompute``
would publish. That shared query is the point of this module: a check run over
a portfolio derived some other way would be checking something nobody reads.

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

from sqlalchemy import Select, case, delete, distinct, func, insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filing import Filing, ParseStatus
from app.db.models.holding import MONEY, Holding
from app.db.models.position_snapshot import PositionSnapshot
from app.db.queries.effective import EFFECTIVE_FILING

#: :func:`snapshot_positions`'s columns, in order, as ``position_snapshot`` names them.
SNAPSHOT_COLUMNS: Final = (
    "filer_id",
    "period_of_report",
    "security_id",
    "cusip",
    "put_call",
    "sshprnamt_type",
    "shares",
    "value_usd",
    "weight",
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
    """The rows ``position_snapshot`` holds: one per position per ``(filer, period)``.

    :param include_suspect: Keep the periods a suspect filing counts toward.
        Their rows come back with ``suspect`` true; without this they do not
        come back at all.
    :param filer_id: One filer's periods instead of every filer's.
    :returns: A select of :data:`SNAPSHOT_COLUMNS`, unordered.

    Positions are summed on ``holding``'s natural key minus the filing, over
    the filings :data:`~app.db.queries.effective.EFFECTIVE_FILING` says count.
    ``weight`` is a position's share of the period's value with option lines
    left out. An option's value is the notional of the underlying, and adding
    it to the total would shrink every real position's weight by the size of
    the hedge.
    """
    period = _periods(filer_id).subquery("period")
    position = _positions(filer_id).subquery("position")

    total = (
        func.sum(position.c.value_usd)
        .filter(position.c.put_call.is_(None))
        .over(partition_by=(position.c.filer_id, position.c.period_of_report))
    )
    weight = case(
        (
            position.c.put_call.is_(None),
            position.c.value_usd / func.nullif(total, 0, type_=MONEY),
        ),
        else_=None,
    )

    statement = select(
        position.c.filer_id,
        position.c.period_of_report,
        position.c.security_id,
        position.c.cusip,
        position.c.put_call,
        position.c.sshprnamt_type,
        position.c.shares,
        position.c.value_usd,
        weight.label("weight"),
        period.c.suspect,
    ).join(
        period,
        (period.c.filer_id == position.c.filer_id)
        & (period.c.period_of_report == position.c.period_of_report),
    )
    # A WHERE runs before the window, but it removes whole periods, and a
    # period's total is summed within the period — so no other weight moves.
    if not include_suspect:
        statement = statement.where(period.c.suspect.is_(False))
    return statement


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
    period = _periods(filer_id).subquery()
    suspect_periods = await session.scalar(
        select(func.count()).select_from(period).where(period.c.suspect)
    )

    return SnapshotRebuild(
        filers=published[2],
        periods=published[1],
        positions=published[0],
        suspect_periods=suspect_periods or 0,
        include_suspect=include_suspect,
    )


def _periods(filer_id: int | None) -> Select[Any]:
    """Every ``(filer, period)`` with a filing that counts, and whether one is suspect."""
    view = EFFECTIVE_FILING
    statement = (
        select(
            view.c.filer_id,
            view.c.period_of_report,
            func.bool_or(Filing.parse_status == ParseStatus.SUSPECT.value).label("suspect"),
        )
        .select_from(view)
        .join(Filing, Filing.id == view.c.filing_id)
        .group_by(view.c.filer_id, view.c.period_of_report)
    )
    return statement if filer_id is None else statement.where(view.c.filer_id == filer_id)


def _positions(filer_id: int | None) -> Select[Any]:
    """The holdings of the filings that count, summed per position per period."""
    view = EFFECTIVE_FILING
    key = (
        view.c.filer_id,
        view.c.period_of_report,
        Holding.security_id,
        Holding.cusip,
        Holding.put_call,
        Holding.sshprnamt_type,
    )
    statement = (
        select(
            *key,
            func.sum(Holding.shares).label("shares"),
            func.sum(Holding.value_usd).label("value_usd"),
        )
        .select_from(view)
        .join(Holding, Holding.filing_id == view.c.filing_id)
        .group_by(*key)
    )
    return statement if filer_id is None else statement.where(view.c.filer_id == filer_id)
