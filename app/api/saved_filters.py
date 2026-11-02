"""A saved filter's row, read through the screener's vocabulary as it is now.

A row keeps what was saved (``saved_filter.params``: ``{"filters": {key:
[value, ...]}, "sort": ..., "order": ...}``), checked when it was saved. The
vocabulary may have changed since: a filter renamed, removed or narrowed, an
investor's slug gone. So a row is read again every time it is used, by the
routes (:mod:`app.api.routers.saved_filters`) and by the alerts that run it
(:mod:`app.alerts.events`) alike, and what no longer reads is left out and
named, never applied as something it no longer means, and never a 500.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.portfolio import SortOrder
from app.api.screener import Applied, Screen, ScreenSort, Unread, query_string, read_filters
from app.db.models import Filer

#: The keys of a row's ``params``. Anything else, a row saved before 0050
#: with ``GET /v1/flows``'s, is ignored.
PARAMS: Final = frozenset({"filters", "sort", "order"})


@dataclass(frozen=True, slots=True)
class SavedRead:
    """A row's params, as the vocabulary reads them now."""

    screen: Screen
    sort: ScreenSort
    order: SortOrder
    ignored: tuple[Unread, ...]
    """What was saved and does not read, left out of :attr:`screen`."""

    @property
    def query(self) -> str:
        """``GET /v1/screener``'s query string for it, without the period."""
        return query_string(self.screen, self.sort, self.order)


def read_params(params: Mapping[str, Any]) -> SavedRead:
    """What of ``params`` reads, and what does not: every filter's own
    reading, the sort's and order's, without the investors' slugs checked
    (:func:`read_saved` checks them)."""
    ignored = [Unread(key, "not part of a saved filter") for key in sorted(set(params) - PARAMS)]
    kept = params.get("filters", {})
    if not isinstance(kept, dict):
        ignored.append(Unread("filters", "not an object of filters"))
        kept = {}
    screen, unread = read_filters(kept)
    ignored += unread
    sort, order = ScreenSort.VALUE, SortOrder.DESC
    try:
        sort = ScreenSort(params.get("sort", sort))
    except ValueError:
        ignored.append(Unread("sort", f"unknown sort {params['sort']!r}: sorted by value"))
    try:
        order = SortOrder(params.get("order", order))
    except ValueError:
        ignored.append(Unread("order", f"unknown order {params['order']!r}: largest first"))
    return SavedRead(screen, sort, order, tuple(ignored))


def _slugs(read: SavedRead) -> frozenset[str]:
    slugs: frozenset[str] | None = read.screen.value("filer_slug")
    return slugs or frozenset()


def _without_gone(read: SavedRead, gone: set[str]) -> SavedRead:
    """``read`` without the investors whose slugs name no one now. One with
    none left is no filter at all, and said so: an empty "any of" would be
    every investor, or none, and neither is what was saved."""
    slugs = _slugs(read)
    if not slugs & gone:
        return read
    left = slugs - gone
    applied = tuple(
        Applied(a.key, left) if a.key == "filer_slug" else a
        for a in read.screen.applied
        if a.key != "filer_slug" or left
    )
    names = ", ".join(map(repr, sorted(slugs & gone)))
    reason = f"no investor has the slug {names} now"
    if not left:
        reason += ", and no other was named: the filter is left out"
    return replace(
        read,
        screen=Screen(applied),
        ignored=(*read.ignored, Unread("filer_slug", reason)),
    )


async def read_saved(session: AsyncSession, params: Sequence[Mapping[str, Any]]) -> list[SavedRead]:
    """Every row's ``params`` read, in order, the slugs of all of them looked
    up in one query."""
    reads = [read_params(one) for one in params]
    slugs = frozenset().union(*map(_slugs, reads))
    if not slugs:
        return reads
    known = set(await session.scalars(select(Filer.slug).where(Filer.slug.in_(sorted(slugs)))))
    return [_without_gone(read, set(slugs - known)) for read in reads]
