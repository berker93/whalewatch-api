"""What one recompute rebuilds: a set of ``(filer, period)`` pairs, or all of them.

A derived row belongs to one filer and one period, so a scope is a set of those
pairs, and a recompute deletes and reinserts exactly the rows inside it.
``recompute --all`` is :data:`EVERYTHING`: no pairs listed, no filter in the
SQL, the whole table. ``--filer`` and ``--period`` are filters on the pairs that
exist, resolved by :func:`resolve_scope`. An ingest's scope is the filing's own
pair, from :func:`filing_pairs`.

A pair "exists" when a filing was filed for it or the snapshot has published it.
The second matters as much as the first. A filing re-linked to another filer,
or re-parsed onto a different period, leaves rows under a pair that nothing
filed now counts toward, and a scope read from ``filing`` alone would never
delete them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, Result, SQLColumnExpression, select, tuple_, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filing import Filing
from app.db.models.position_snapshot import PositionSnapshot

Pair = tuple[int, date]
"""``(filer_id, period_of_report)``: the unit a derived row belongs to."""


@dataclass(frozen=True, slots=True)
class Scope:
    """Every ``(filer, period)``, or just :attr:`pairs`."""

    pairs: frozenset[Pair] | None = None
    """``None`` for every pair there is, which the SQL spells as no filter at all."""

    @classmethod
    def of(cls, pairs: Iterable[Pair]) -> Scope:
        return cls(frozenset(pairs))

    @property
    def everything(self) -> bool:
        return self.pairs is None

    @property
    def filer_ids(self) -> frozenset[int] | None:
        """The filers with a pair in scope, ``None`` for every filer."""
        return None if self.pairs is None else frozenset(filer for filer, _ in self.pairs)

    def __or__(self, other: Scope) -> Scope:
        if self.pairs is None or other.pairs is None:
            return EVERYTHING
        return Scope(self.pairs | other.pairs)

    def covers(
        self, filer_id: SQLColumnExpression[Any], period_of_report: SQLColumnExpression[Any]
    ) -> list[ColumnElement[bool]]:
        """A ``WHERE`` for the rows in scope, to splat into ``.where(*...)``.

        Empty for :data:`EVERYTHING`, so that ``--all`` is the unfiltered
        statement it always was, and not one with every pair listed in it.
        """
        if self.pairs is None:
            return []
        return [tuple_(filer_id, period_of_report).in_(sorted(self.pairs))]

    def of_filers(self, filer_id: SQLColumnExpression[Any]) -> list[ColumnElement[bool]]:
        """A ``WHERE`` for every period of the filers in scope.

        For a window over a filer's history, which needs the periods around
        the ones in scope to see their neighbours. Filtering on the window's
        partition key removes whole partitions and leaves the rest intact.
        """
        filers = self.filer_ids
        return [] if filers is None else [filer_id.in_(sorted(filers))]


EVERYTHING: Final = Scope()


async def resolve_scope(
    session: AsyncSession, *, filer_id: int | None = None, period: date | None = None
) -> Scope:
    """The pairs that exist for this filer, this period, or this filer in this period.

    Neither given is every pair there is, which is :data:`EVERYTHING`.

    :param period: A quarter end, ``2026-03-31``, as ``period_of_report`` has it.
    """
    if filer_id is None and period is None:
        return EVERYTHING

    def matching(
        filer: SQLColumnExpression[Any], period_of_report: SQLColumnExpression[Any]
    ) -> list[ColumnElement[bool]]:
        return [
            *([] if filer_id is None else [filer == filer_id]),
            *([] if period is None else [period_of_report == period]),
        ]

    filed = select(Filing.filer_id, Filing.period_of_report).where(
        # Unlinked filings belong to no filer yet, and a Form 4 to no period.
        Filing.filer_id.is_not(None),
        Filing.period_of_report.is_not(None),
        *matching(Filing.filer_id, Filing.period_of_report),
    )
    published = select(PositionSnapshot.filer_id, PositionSnapshot.period_of_report).where(
        *matching(PositionSnapshot.filer_id, PositionSnapshot.period_of_report)
    )
    # Neither column is null in either half: see the WHERE on ``filed``.
    rows: Result[Any] = await session.execute(union(filed, published))
    return Scope.of(cast(list[Pair], rows.all()))


async def filing_pairs(session: AsyncSession, accession_no: str) -> frozenset[Pair]:
    """The pair a filing is filed under now: none while it is unloaded or unlinked.

    Read before a load as well as after it. A re-ingest can move a filing to
    another pair, and the pair it left still holds rows built from it.
    """
    rows = await session.execute(
        select(Filing.filer_id, Filing.period_of_report).where(
            Filing.accession_no == accession_no,
            Filing.filer_id.is_not(None),
            Filing.period_of_report.is_not(None),
        )
    )
    return frozenset(cast(list[Pair], rows.all()))
