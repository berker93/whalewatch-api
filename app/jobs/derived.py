"""The publishing jobs: the derived tables rebuilt, and the views refreshed from them.

``recompute`` and ``refresh-views`` are these with a summary; the
``recompute_positions`` and ``refresh_matviews`` tasks are these in
``asyncio.run``, and are the second and third links of the chain an ingest
starts (app/jobs/tasks.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.periods import quarter_label
from app.db.session import session_scope
from app.derived.recompute import Recomputed, recompute
from app.derived.scope import Pair, Scope, resolve_scope
from app.derived.views import Refreshed, refresh_order, refresh_views
from app.ingestion.discovery import tracked_filer_ids
from app.jobs.cache import invalidate_cache
from app.jobs.tracking import TrackedRun, tracked_task

logger = get_logger(__name__)


def _recompute_context(_: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """The pairs counted, not listed: after a full backfill they are thousands."""
    pairs: Collection[Pair] | None = arguments.get("pairs")
    return {**arguments, "pairs": None if pairs is None else len(pairs)}


@tracked_task("recompute", context=_recompute_context)
async def recompute_positions(
    run: TrackedRun,
    settings: Settings,
    *,
    filer: str | None = None,
    period: date | None = None,
    pairs: Collection[Pair] | None = None,
    include_suspect: bool = False,
    after_run_id: uuid.UUID | None = None,
) -> Recomputed:
    """Rebuild the snapshot, prices, splits and changes, in one transaction.

    For ``pairs`` when given; otherwise for the pairs ``filer`` and ``period``
    pick out, together or alone, and every pair there is when neither is given
    — the CLI's ``--all``, which it makes a person say.

    Then drops the API's cache, whether or not it published anything: a
    rebuild may delete rows that were being served. The views are not
    refreshed here; that is :func:`refresh_matviews`, a run of its own.

    :param pairs: ``(filer_id, period)`` pairs, from the ingest run before it
        in a chain. Not with ``filer`` or ``period``.
    :param after_run_id: The run whose loads this follows, kept in the row's
        ``context``; ``None`` when run on its own.
    :raises UnknownFilerError: ``filer`` names no filer.
    """
    if pairs is not None and (filer is not None or period is not None):
        raise ValueError("pairs is a scope of its own: not with filer or period")
    async with session_scope(settings) as session:
        if pairs is not None:
            scope = Scope.of(pairs)
        else:
            filer_id = await filer_id_of(session, filer)
            scope = await resolve_scope(session, filer_id=filer_id, period=period)
        rebuilt = await recompute(session, scope, include_suspect=include_suspect)
    # Now, not only after a refresh: portfolios are read from the rebuilt
    # tables, not the views, and the refresh may never come.
    run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    rebuild, changes = rebuilt.snapshot, rebuilt.changes
    # Periods, not positions, so that the gap between the two is the periods
    # withheld for a suspect filing.
    run.items_seen = rebuild.periods + rebuild.withheld
    run.items_written = rebuild.periods

    described = {"filer": filer, "period": None if period is None else quarter_label(period)}
    logger.info(
        "position_snapshot.recomputed",
        **described,
        positions=rebuild.positions,
        periods=rebuild.periods,
        suspect_periods=rebuild.suspect_periods,
        include_suspect=include_suspect,
    )
    logger.info(
        "position_change.recomputed",
        **described,
        following_periods=len(rebuilt.following),
        new=changes.new,
        add=changes.add,
        trim=changes.trim,
        hold=changes.hold,
        exit=changes.exit,
    )
    logger.info(
        "implied_price.recomputed",
        **described,
        prices=rebuilt.prices.prices,
        high=rebuilt.prices.high,
        medium=rebuilt.prices.medium,
        low=rebuilt.prices.low,
        unpriced=rebuilt.prices.unpriced,
    )
    logger.info(
        "security_split.recomputed",
        **described,
        splits=len(rebuilt.splits.splits),
        added=len(rebuilt.splits.added),
        removed=len(rebuilt.splits.removed),
        rerated=len(rebuilt.splits.rerated),
        crossing_pairs=len(rebuilt.crossing),
        split_changes=changes.split,
    )
    return rebuilt


async def filer_id_of(session: AsyncSession, slug: str | None) -> int | None:
    """The filer ``slug`` names, or ``None`` for every filer.

    :raises UnknownFilerError: No filer has the slug. Raised, not treated as a
        filer with nothing loaded: for a command whose clean result means "safe
        to publish", a typo would otherwise pass every check.
    """
    if slug is None:
        return None
    (filer_id,) = await tracked_filer_ids(session, slug=slug)
    return filer_id


@dataclass(frozen=True, slots=True)
class RefreshedViews:
    """Each view refreshed, in the order it was."""

    views: list[Refreshed]


@tracked_task("refresh-views")
async def refresh_matviews(
    run: TrackedRun,
    settings: Settings,
    *,
    view: str | None = None,
    concurrent: bool = True,
    after_run_id: uuid.UUID | None = None,
) -> RefreshedViews:
    """Refresh the materialised views, each after any view it reads.

    Always a run of its own, never part of the run before it. That one has
    committed, and its row says how it went. A refresh that fails after it is
    this row's failure, not a reason to report a finished rebuild as failed.

    :param view: Only this view, and any view that reads it.
    :param concurrent: Let reads of each view go on while it is refreshed.
    :param after_run_id: The run whose publishing this follows, kept in
        ``context``. ``None`` when run on its own.
    """
    async with session_scope(settings) as session:
        views = await refresh_order(session, only=view)
        run.items_seen = len(views)
        refreshed = await refresh_views(session, views, concurrently=concurrent, run_id=run.id)
    run.items_written = len(refreshed)
    run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    run.metrics["views"] = {
        done.name: {
            "seconds": round(done.seconds, 3),
            "rows": done.rows,
            "concurrently": done.concurrently,
        }
        for done in refreshed
    }
    # After the commit, so a line never reports a refresh that rolled back.
    for done in refreshed:
        logger.info(
            "materialised_view.refreshed",
            view=done.name,
            rows=done.rows,
            concurrently=done.concurrently,
            seconds=round(done.seconds, 3),
        )
    return RefreshedViews(views=refreshed)
