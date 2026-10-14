"""``recompute``: the derived tables rebuilt for a scope, and the period after it.

Ingesting one filing must not mean rebuilding five years of everything, so a
rebuild has a :class:`~app.derived.scope.Scope`: the ``(filer, period)`` pairs
whose rows it deletes and inserts again. ``ingest-filing`` and ``backfill``
rebuild the pair a filing is filed under, in the transaction that loads it.
``recompute --all`` rebuilds every pair, and stays the backstop for anything
that changes the derived tables without loading a filing: a new ``recompute``
query, a CIK moved to another filer, an overlap policy changed.

The period after
----------------
``position_snapshot`` for a period depends on that period's filings alone.
``position_change`` does not. A period's changes are its positions against the
filer's previous published period, and its exits are that previous period's
positions gone from it. So when a manager amends 2024Q3, Q3's snapshot is wrong,
and so are Q4's changes, which were computed against the old Q3. The rebuild
walks forward one period.

One period, and the filer's next *published* one, the way ``position_change``
defines previous. A quarter with nothing loaded, or one withheld for a suspect
filing, is stepped over, and the change across it is dated the period after.
That is also what makes one period enough: a change depends on its own period
and the one before, and nothing else, so the change two periods on cannot see
the rebuilt one. When the rebuilt period appears or disappears, as when a
suspect amendment withholds it, the next period's previous one moves, and that
is the same next period.

Only the changes walk forward. The next period's snapshot does not depend on
this one, and rebuilding it would withhold it again if it was published with
``--include-suspect``.

Implied prices
--------------
``implied_price`` is the third table, and it is on another axis: a price
belongs to a ``(security, period)``, and is computed across every filer that
held the security. So the rebuild finds the securities the pairs in scope held,
before the snapshot is rebuilt and after it, and rebuilds the price of each of
those in that period, and of nothing else
(:mod:`app.derived.implied_price`). A filing of forty positions rebuilds forty
prices.

Prices do not walk forward. A price depends on its own period's snapshot rows,
and the period after keeps the snapshot it had, so its prices cannot have
moved. Rebuilding them would be the changes' cascade applied where nothing
cascades.

One rebuild at a time
---------------------
Each rebuild deletes and inserts in its own transaction, and two of them over
the same pairs would collide. The second's ``DELETE`` cannot see the first's
uncommitted rows, so its ``INSERT`` fails on the key, or it wins and builds Q4's
changes from a Q3 snapshot the other is replacing. Backfill loads several
filings at once, often of the same filer. So every rebuild takes
:data:`RECOMPUTE_LOCK` first, and holds it until it commits: Postgres advisory
locks are per transaction here, and released by the ``COMMIT`` or ``ROLLBACK``.
Each statement after the lock sees every rebuild before it, committed.

A load takes the lock before it writes anything, not just before it rebuilds.
The rebuild's ``INSERT`` takes a ``KEY SHARE`` lock on each ``filing`` row a
snapshot row points at. A load that upserted that row first, and then waited
for the lock, would hold the row against the rebuild holding the lock: a
deadlock. Taking this lock first, always, rules that out.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.queries.periods import FILER_PERIOD
from app.derived.implied_price import PriceRebuild, held_in, recompute_implied_price
from app.derived.position_change import ChangeRebuild, recompute_position_change
from app.derived.position_snapshot import SnapshotRebuild, recompute_position_snapshot
from app.derived.scope import Pair, Scope

#: The advisory lock every rebuild holds until it commits. Any 64-bit number no
#: other lock in this database uses; these are the bytes of ``wwrecomp``.
RECOMPUTE_LOCK: Final = int.from_bytes(b"wwrecomp", "big")


@dataclass(frozen=True, slots=True)
class Recomputed:
    """What one :func:`recompute` rebuilt."""

    scope: Scope
    snapshot: SnapshotRebuild
    """``position_snapshot`` for :attr:`scope`."""
    changes: ChangeRebuild
    """``position_change`` for :attr:`scope` and :attr:`following`."""
    following: frozenset[Pair]
    """The next published period after each one in :attr:`scope`, outside it,
    whose changes start from a period this rebuilt."""
    prices: PriceRebuild
    """``implied_price`` for every security held in :attr:`scope`, before the
    rebuild or after it, in its period there."""


async def hold_recompute_lock(session: AsyncSession) -> None:
    """Wait until no other rebuild is running, and keep the next one waiting until this commits.

    :func:`recompute` takes it itself. A transaction that writes ``filing`` or
    ``holding`` before it rebuilds takes it before the first write, for the
    deadlock the module docstring describes. Taking it twice in one
    transaction is harmless.
    """
    await session.execute(select(func.pg_advisory_xact_lock(RECOMPUTE_LOCK)))


async def recompute(
    session: AsyncSession, scope: Scope, *, include_suspect: bool = False
) -> Recomputed:
    """Rebuild ``position_snapshot`` for ``scope``, then ``position_change`` and ``implied_price``.

    The snapshot first, since the other two are read from it. The changes for
    ``scope`` and for the next published period after each pair in it. The
    prices of the securities held in ``scope``, before or after, in its
    periods. All in the caller's transaction, which commits them together: a
    reader sees every table rebuilt, or none.

    :param include_suspect: Publish the periods in scope that a suspect filing
        counts toward, marked ``suspect``, instead of withholding them.
    """
    await hold_recompute_lock(session)
    # Before the snapshot too: a security the rebuild drops from a period
    # still has a price there that counted it.
    held_before = None if scope.everything else await held_in(session, scope)
    snapshot = await recompute_position_snapshot(session, scope, include_suspect=include_suspect)
    # After the snapshot: a period it now publishes, or withholds, is or is
    # not a "next period" for the one before it.
    following = await _following(session, scope)
    changes = await recompute_position_change(session, scope | Scope.of(following))
    prices = await recompute_implied_price(
        session, None if held_before is None else held_before | await held_in(session, scope)
    )
    return Recomputed(
        scope=scope, snapshot=snapshot, changes=changes, following=following, prices=prices
    )


async def _following(session: AsyncSession, scope: Scope) -> frozenset[Pair]:
    """Each filer's next published period after each of its periods in scope, if it is not in scope.

    Read from the published periods of the filers in scope, a few dozen rows
    apiece, and paired up here.
    """
    if scope.pairs is None:
        return frozenset()
    rows = await session.execute(
        select(FILER_PERIOD.c.filer_id, FILER_PERIOD.c.period_of_report)
        .where(*scope.of_filers(FILER_PERIOD.c.filer_id))
        .order_by(FILER_PERIOD.c.filer_id, FILER_PERIOD.c.period_of_report)
    )
    published: defaultdict[int, list[date]] = defaultdict(list)
    for filer_id, period in rows.tuples():
        published[filer_id].append(period)

    following: set[Pair] = set()
    for filer_id, period in scope.pairs:
        periods = published[filer_id]
        after = bisect_right(periods, period)
        if after < len(periods):
            following.add((filer_id, periods[after]))
    return frozenset(following - scope.pairs)
