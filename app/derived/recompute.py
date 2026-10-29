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

Splits
------
``security_split`` is on the prices' axis, and is read from them, so it is
rebuilt after them (:mod:`app.derived.security_split`). On the same keys, and
on the quarter after each, since that quarter's detection compares its shares
and price with this one's: splits do walk forward, one quarter.

A split is a fact about every holder of the security, so when one appears,
disappears or changes its ratio, the changes of every filer across it are
wrong, and not only those of the filers in scope. The rebuild finds those
``(filer, period)`` pairs in ``position_change`` (their previous period before
the split, their own on or after it) and rebuilds their changes with the rest.
Only the pairs across a split that moved: one filing that leaves every split
as it was rebuilds no other filer's changes. So the changes are rebuilt last,
after the splits they are adjusted by.

``recompute --all`` detects every split and rebuilds every change, so it runs
``detect-splits --all`` and then ``recompute --all`` in one go, which is the
run order the splits need: snapshot, then prices, then splits, then changes.

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

Alerts
------
What a rebuild publishes is somebody's news: a filing, and the positions it
changed. So each rebuild names the pairs it touched, its scope, the period
after each and the pairs across a split that moved, to
:func:`app.alerts.dispatch.after_commit`, which sends them to the alerts task
once the caller's transaction commits, and forgets them if it rolls back. The
task reads the events from the tables as they are then (:mod:`app.alerts`).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.dispatch import after_commit
from app.db.models.position_snapshot import PositionSnapshot
from app.db.queries.periods import FILER_PERIOD
from app.derived.implied_price import (
    PriceRebuild,
    SecurityPeriod,
    held_in,
    recompute_implied_price,
)
from app.derived.position_change import ChangeRebuild, recompute_position_change
from app.derived.position_snapshot import SnapshotRebuild, recompute_position_snapshot
from app.derived.scope import Pair, Scope
from app.derived.security_split import (
    SplitRebuild,
    crossing_pairs,
    recompute_security_split,
    split_keys,
)

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
    """``position_change`` for :attr:`scope`, :attr:`following` and :attr:`crossing`."""
    following: frozenset[Pair]
    """The next published period after each one in :attr:`scope`, outside it,
    whose changes start from a period this rebuilt."""
    prices: PriceRebuild
    """``implied_price`` for every security held in :attr:`scope`, before the
    rebuild or after it, in its period there."""
    splits: SplitRebuild
    """``security_split`` at the prices' keys, and the quarter after each."""
    crossing: frozenset[Pair]
    """Pairs outside :attr:`scope` and :attr:`following` whose changes cross a
    split that appeared, disappeared or changed its ratio."""

    @property
    def touched(self) -> frozenset[Pair] | None:
        """Every pair whose rows this rebuilt: ``None`` for every pair there is."""
        if self.scope.pairs is None:
            return None
        return self.scope.pairs | self.following | self.crossing


@dataclass(frozen=True, slots=True)
class Redetected:
    """What one :func:`detect_splits` found, and the changes it rebuilt."""

    splits: SplitRebuild
    crossing: frozenset[Pair]
    """Every pair with a change across a split that moved: the ones rebuilt."""
    changes: ChangeRebuild
    """``position_change`` for :attr:`crossing`."""


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
    """Rebuild ``position_snapshot`` for ``scope``, then the prices, the splits and the changes.

    The snapshot first, since everything else is read from it. The prices of
    the securities held in ``scope``, before or after, in its periods. The
    splits of the same securities in the same periods, and the quarter after
    each. The changes last, adjusted for the splits: for ``scope``, for the
    next published period after each pair in it, and for every pair across a
    split that moved. All in the caller's transaction, which commits them
    together: a reader sees every table rebuilt, or none. Once it commits, the
    pairs it touched go to the alerts task (:attr:`Recomputed.touched`).

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
    held = None if held_before is None else held_before | await held_in(session, scope)
    prices = await recompute_implied_price(session, held)
    splits = await recompute_security_split(session, None if held is None else split_keys(held))
    # Before the changes are rebuilt: read from the changes as they are.
    crossing = (
        frozenset()
        if scope.everything
        else await crossing_pairs(session, splits.moved) - (scope.pairs or frozenset()) - following
    )
    changes = await recompute_position_change(
        session, scope | Scope.of(following) | Scope.of(crossing)
    )
    rebuilt = Recomputed(
        scope=scope,
        snapshot=snapshot,
        changes=changes,
        following=following,
        prices=prices,
        splits=splits,
        crossing=crossing,
    )
    after_commit(session, rebuilt.touched)
    return rebuilt


async def detect_splits(session: AsyncSession, *, period: date | None = None) -> Redetected:
    """Rebuild ``security_split`` for one period or every one, and the changes across what moved.

    ``recompute`` does both for what it rebuilds. This is for when the
    detection changes without the snapshot changing: new thresholds, a
    fraction added, the first run after the table was created. Only the
    changes across a split that appeared, disappeared or changed its ratio
    are rebuilt, so a second run rebuilds nothing.

    :param period: Only the splits dated this quarter end. ``None`` for all.
    """
    await hold_recompute_lock(session)
    keys: frozenset[SecurityPeriod] | None = None
    if period is not None:
        rows = await session.execute(
            select(PositionSnapshot.security_id, PositionSnapshot.period_of_report)
            .where(PositionSnapshot.period_of_report == period)
            .distinct()
        )
        keys = frozenset(cast(Iterable[SecurityPeriod], rows.tuples()))
    splits = await recompute_security_split(session, keys)
    crossing = await crossing_pairs(session, splits.moved)
    changes = (
        await recompute_position_change(session, Scope.of(crossing))
        if crossing
        else ChangeRebuild(new=0, add=0, trim=0, hold=0, exit=0)
    )
    after_commit(session, crossing)
    return Redetected(splits=splits, crossing=crossing, changes=changes)


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
