"""The signed-in user's saved screens: the screener's filters, under a name.

``GET /v1/saved-filters``
    Every one, oldest first, each read through the screener's vocabulary as
    it is now.
``POST /v1/saved-filters``
    Save one: a name and ``GET /v1/screener``'s filters, sort and order
    (:mod:`app.api.schemas.saved_filter`). As many as the plan's
    ``screener_saved_filters`` (3 on Free); one more is a 402 naming the plan
    that keeps more (:mod:`app.api.entitlements`), and past
    :data:`~app.accounts.entitlements.MAX_SAVED_FILTERS`, which no plan lifts, a 422.
    Two with one name are two filters: the name is the user's to choose.
``PATCH /v1/saved-filters/{id}``
    Rename it, save another screen over it, or both.
``POST /v1/saved-filters/{id}/duplicate``
    Save a copy, as it was saved, under the plan's limit as a new one is.
``DELETE /v1/saved-filters/{id}``
    Delete one. Idempotent: one already gone, or never the user's, is no change.
``GET /v1/saved-filters/{id}/screen``
    Run it: the rows ``GET /v1/screener`` gives for its filters, of the
    latest quarter unless ``?period=`` says otherwise, and ``last_run_at``
    set to now.

Read again, every time
----------------------
A filter is checked against the vocabulary when it is saved, and anything it
does not take is a 422. It is read through the vocabulary again whenever it
is read, since a filter may be renamed, removed or narrowed after: what no
longer reads, a key, a value, an investor's slug that has gone, a sort, is
left out and named in ``ignored``, and the rest is the filter. Never a 500,
and never applied as something it no longer means. The row keeps what was
saved, so should the vocabulary take it again, the filter is whole again; a
``PATCH`` with ``params`` replaces it.

Saving, changing and deleting need a verified email, as the watchlist does;
reading and running do not.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.entitlements import MAX_SAVED_FILTERS
from app.api.auth import UNAUTHORIZED, UNVERIFIED, NoStoreDep, VerifiedViewerDep, ViewerDep
from app.api.deps import PageParamsDep, RedisDep, SessionDep, SettingsDep
from app.api.entitlements import PLAN_LIMITED, EntitlementContext, EntitlementContextDep
from app.api.errors import INVALID, INVALID_CURSOR, invalid, not_found
from app.api.routers.portfolio import SortOrder
from app.api.routers.screener import TIMED_OUT, fixed_screen_parameters, run_screen
from app.api.schemas.saved_filter import (
    IgnoredParam,
    NewSavedFilter,
    SavedFilter,
    SavedFilterChange,
    SavedFilterCopy,
    SavedFilterParams,
    SavedFilters,
    SavedFilterScreenEnvelope,
    SavedFilterScreenMeta,
)
from app.api.schemas.screener import AppliedFilter
from app.api.schemas.types import Period
from app.api.screener import (
    Applied,
    Screen,
    ScreenSort,
    Unread,
    query_string,
    read_filters,
    unknown_slugs,
)
from app.db.models import AppUser, Filer
from app.db.models import SavedFilter as SavedFilterRow
from app.db.models.account import MAX_FILTER_NAME

router = APIRouter(prefix="/saved-filters", tags=["saved-filters"], dependencies=[NoStoreDep])

#: The keys of a row's ``params``. Anything else, a row saved before 0050
#: with ``GET /v1/flows``'s, is ignored.
_PARAMS: Final = frozenset({"filters", "sort", "order"})
_COPY: Final = " (copy)"

_NO_FILTER: Final = not_found("No saved filter with that id is the signed-in user's.")


# --- reading a row --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Read:
    """A row's params, as the vocabulary reads them now."""

    screen: Screen
    sort: ScreenSort
    order: SortOrder
    ignored: tuple[Unread, ...]


def _read(params: dict[str, Any]) -> _Read:
    """What of ``params`` reads, and what does not: every filter's own
    reading, the sort's and order's, without the investors' slugs checked."""
    ignored = [Unread(key, "not part of a saved filter") for key in sorted(set(params) - _PARAMS)]
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
    return _Read(screen, sort, order, tuple(ignored))


def _slugs(read: _Read) -> frozenset[str]:
    slugs: frozenset[str] | None = read.screen.value("filer_slug")
    return slugs or frozenset()


def _without_gone(read: _Read, gone: set[str]) -> _Read:
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


async def _read_all(session: AsyncSession, rows: list[SavedFilterRow]) -> list[_Read]:
    """Every row read, the slugs of all of them looked up in one query."""
    reads = [_read(row.params) for row in rows]
    slugs = frozenset().union(*map(_slugs, reads))
    if not slugs:
        return reads
    known = set(await session.scalars(select(Filer.slug).where(Filer.slug.in_(sorted(slugs)))))
    return [_without_gone(read, set(slugs - known)) for read in reads]


def _saved(row: SavedFilterRow, read: _Read) -> SavedFilter:
    return SavedFilter(
        id=str(row.id),
        name=row.name,
        params=SavedFilterParams(
            filters={a.key: a.display() for a in read.screen.applied},
            sort=read.sort,
            order=read.order,
        ),
        filters=[
            AppliedFilter(key=a.key, label=a.filter.label, values=a.display())
            for a in read.screen.applied
        ],
        query=query_string(read.screen, read.sort, read.order),
        ignored=[IgnoredParam(key=u.key, reason=u.reason) for u in read.ignored],
        created_at=row.created_at,
        last_run_at=row.last_run_at,
    )


async def _one(session: AsyncSession, row: SavedFilterRow) -> SavedFilter:
    [read] = await _read_all(session, [row])
    return _saved(row, read)


async def _mine(session: AsyncSession, user_id: int, filter_id: int) -> SavedFilterRow:
    row = await session.scalar(
        select(SavedFilterRow).where(
            SavedFilterRow.id == filter_id, SavedFilterRow.user_id == user_id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No saved filter with that id is yours.")
    return row


# --- writing one ----------------------------------------------------------------


async def _checked(session: AsyncSession, params: SavedFilterParams) -> dict[str, Any]:
    """``params`` as a row keeps them: each filter as the vocabulary reads it.

    :raises RequestValidationError: 422 naming every filter it does not take,
        and every slug that names no investor.
    """
    screen, unread = read_filters(params.filters)
    errors = [(u.key, u.reason) for u in unread]
    if (slugs := screen.value("filer_slug")) and (unknown := await unknown_slugs(session, slugs)):
        errors.append(("filer_slug", f"no investor has the slug {', '.join(map(repr, unknown))}"))
    if errors:
        raise RequestValidationError(
            [
                {"type": "value_error", "loc": ("body", "params", "filters", key), "msg": msg}
                for key, msg in errors
            ]
        )
    return {
        "filters": {a.key: a.display() for a in screen.applied},
        "sort": params.sort.value,
        "order": params.order.value,
    }


async def _make_room(session: AsyncSession, entitled: EntitlementContext, user_id: int) -> None:
    """Refuse one more past the plan's ``screener_saved_filters`` (402) or
    :data:`MAX_SAVED_FILTERS` (422), one save at a time per account."""
    # So two at once cannot both pass the count. The user's row rather than
    # the filters': there may be none.
    await session.execute(select(AppUser.id).where(AppUser.id == user_id).with_for_update())
    mine = SavedFilterRow.user_id == user_id
    count = await session.scalar(select(func.count()).select_from(SavedFilterRow).where(mine)) or 0
    if count >= MAX_SAVED_FILTERS:
        raise invalid("name", f"at most {MAX_SAVED_FILTERS} filters are kept", where="body")
    entitled.require_room("screener_saved_filters", count, what="saved filters")


async def _add(
    session: AsyncSession, user_id: int, name: str, params: dict[str, Any]
) -> SavedFilter:
    row = SavedFilterRow(user_id=user_id, name=name, params=params)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _one(session, row)


def _copy_name(name: str) -> str:
    """``name (copy)``, the original cut short to make room."""
    return name[: MAX_FILTER_NAME - len(_COPY)].rstrip() + _COPY


# --- the routes -----------------------------------------------------------------


FilterId = Annotated[int, Path(ge=1, description="`id` from the list.", examples=[12])]


@router.get(
    "",
    operation_id="getSavedFilters",
    response_model=SavedFilters,
    summary="The signed-in user's saved screener filters",
    responses=UNAUTHORIZED,
)
async def read_saved_filters(viewer: ViewerDep, session: SessionDep) -> SavedFilters:
    """Every filter saved, oldest first, each read through the screener's
    filters as they are now: what no longer reads is in its `ignored`, and
    left out of its `params`. None saved is an empty list, never a 404."""
    rows = list(
        await session.scalars(
            select(SavedFilterRow)
            .where(SavedFilterRow.user_id == viewer.user.id)
            .order_by(SavedFilterRow.id)
        )
    )
    reads = await _read_all(session, rows)
    return SavedFilters(data=[_saved(row, read) for row, read in zip(rows, reads, strict=True)])


@router.post(
    "",
    operation_id="saveFilter",
    response_model=SavedFilter,
    status_code=status.HTTP_201_CREATED,
    summary="Save a screener filter",
    responses={**UNAUTHORIZED, **PLAN_LIMITED, **UNVERIFIED, **INVALID},
)
async def save_filter(
    body: NewSavedFilter,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
) -> SavedFilter:
    """Saves `params` as `name`, after the others. A filter `GET /v1/screener`
    does not take, a value it would refuse, `period`, or a slug that names no
    investor is a 422 naming it. As many as the plan's `screener_saved_filters`
    allows (3 on Free): one more is a 402 `plan_limit` naming the plan that
    keeps more, and one past 100, the most any plan keeps, a 422. An account
    whose email is not verified is a 403 `email_unverified`."""
    params = await _checked(session, body.params)
    await _make_room(session, entitled, viewer.user.id)
    return await _add(session, viewer.user.id, body.name, params)


@router.patch(
    "/{filter_id}",
    operation_id="changeSavedFilter",
    response_model=SavedFilter,
    summary="Rename a saved screener filter, or save another screen over it",
    responses={**UNAUTHORIZED, **UNVERIFIED, **_NO_FILTER, **INVALID},
)
async def change_saved_filter(
    filter_id: FilterId, body: SavedFilterChange, viewer: VerifiedViewerDep, session: SessionDep
) -> SavedFilter:
    """Sets `name`, `params` or both, and leaves the rest. `params` is the whole
    screen, checked as a new one is, and replaces what was saved, `ignored`
    included. Never a 402: it is not one more. Another user's is a 404."""
    row = await _mine(session, viewer.user.id, filter_id)
    if body.params is not None:
        row.params = await _checked(session, body.params)
    if body.name is not None:
        row.name = body.name
    await session.commit()
    await session.refresh(row)
    return await _one(session, row)


@router.post(
    "/{filter_id}/duplicate",
    operation_id="duplicateSavedFilter",
    response_model=SavedFilter,
    status_code=status.HTTP_201_CREATED,
    summary="Save a copy of a saved screener filter",
    responses={**UNAUTHORIZED, **PLAN_LIMITED, **UNVERIFIED, **_NO_FILTER, **INVALID},
)
async def duplicate_saved_filter(
    filter_id: FilterId,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
    body: SavedFilterCopy | None = None,
) -> SavedFilter:
    """A new filter, after the others, of what the original saved, what it
    ignores included, named `name` or the original's with " (copy)". Counted
    as a new one is: past the plan's `screener_saved_filters` a 402. Never
    run, so `last_run_at` is null. Another user's is a 404."""
    user_id = viewer.user.id
    await _make_room(session, entitled, user_id)
    original = await _mine(session, user_id, filter_id)
    name = body.name if body is not None and body.name is not None else _copy_name(original.name)
    return await _add(session, user_id, name, dict(original.params))


@router.delete(
    "/{filter_id}",
    operation_id="deleteSavedFilter",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a saved screener filter",
    responses={**UNAUTHORIZED, **UNVERIFIED, **INVALID},
)
async def delete_saved_filter(
    filter_id: FilterId, viewer: VerifiedViewerDep, session: SessionDep
) -> None:
    """Deletes it. One already deleted, or another user's, is no change: 204 either way."""
    await session.execute(
        delete(SavedFilterRow).where(
            SavedFilterRow.id == filter_id, SavedFilterRow.user_id == viewer.user.id
        )
    )
    await session.commit()


@router.get(
    "/{filter_id}/screen",
    operation_id="screenSavedFilter",
    response_model=SavedFilterScreenEnvelope,
    summary="Run a saved screener filter, on the latest quarter or another",
    dependencies=[Depends(fixed_screen_parameters)],
    responses={
        **UNAUTHORIZED,
        **not_found(
            "No saved filter with that id is the signed-in user's, or nothing is "
            "published for the period."
        ),
        **PLAN_LIMITED,
        **INVALID_CURSOR,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            **INVALID[status.HTTP_422_UNPROCESSABLE_CONTENT],
            "description": (
                "`period` is malformed, or a parameter other than `period`, `limit` and "
                "`cursor` was given. `detail.errors` says which."
            ),
        },
        **TIMED_OUT,
    },
)
async def screen_saved_filter(
    filter_id: FilterId,
    viewer: ViewerDep,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    page: PageParamsDep,
    entitled: EntitlementContextDep,
    period: Annotated[
        Period | None,
        Query(
            description=(
                "The quarter, as `2026Q1` or `2026-03-31`. The latest with positions "
                "inside the plan's window when left out."
            )
        ),
    ] = None,
) -> SavedFilterScreenEnvelope:
    """The positions `GET /v1/screener` gives for the filter's `query` in
    `period`: the same rows, and the same page for the same cursor. What it
    `ignored` is not applied, and `meta.filters` is what was. Sets its
    `last_run_at`, on every page, and `meta.saved_filter` is the filter with
    it set. Held to the plan's window as the screener is: a 402 for a quarter
    before it. Another user's is a 404."""
    row = await _mine(session, viewer.user.id, filter_id)
    [read] = await _read_all(session, [row])
    rows, meta, page_info = await run_screen(
        session, redis, settings, page, entitled, period, read.screen, read.sort, read.order
    )
    row.last_run_at = datetime.now(UTC)
    await session.commit()
    return SavedFilterScreenEnvelope(
        data=rows,
        meta=SavedFilterScreenMeta(**meta.model_dump(), saved_filter=_saved(row, read)),
        page=page_info,
    )
