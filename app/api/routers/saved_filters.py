"""The signed-in user's saved screener filters.

``GET /v1/saved-filters``
    Every one, oldest first.
``POST /v1/saved-filters``
    Save one: a name and ``GET /v1/flows``'s parameters
    (:mod:`app.api.schemas.saved_filter`). As many as the plan's
    ``screener_saved_filters`` (3 on Free); one more is a 402 naming the plan
    that keeps more (:mod:`app.api.entitlements`), and past
    :data:`~app.accounts.entitlements.MAX_SAVED_FILTERS`, which no plan lifts, a 422.
    Two with one name are two filters: the name is the user's to choose.
``DELETE /v1/saved-filters/{id}``
    Delete one. Idempotent: one already gone, or never the user's, is no change.

Saving needs a verified email, as the watchlist does; reading does not.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.entitlements import MAX_SAVED_FILTERS
from app.api.auth import UNAUTHORIZED, UNVERIFIED, NoStoreDep, VerifiedViewerDep, ViewerDep
from app.api.deps import SessionDep
from app.api.entitlements import PLAN_LIMITED, EntitlementContextDep
from app.api.errors import INVALID, invalid
from app.api.schemas.saved_filter import NewSavedFilter, SavedFilter, SavedFilters, ScreenerQuery
from app.db.models import AppUser
from app.db.models import SavedFilter as SavedFilterRow

router = APIRouter(prefix="/saved-filters", tags=["saved-filters"], dependencies=[NoStoreDep])


def _saved(row: SavedFilterRow) -> SavedFilter:
    return SavedFilter(
        id=str(row.id),
        name=row.name,
        query=ScreenerQuery.model_validate(row.query),
        created_at=row.created_at,
    )


async def _all(session: AsyncSession, user_id: int) -> SavedFilters:
    rows = await session.scalars(
        select(SavedFilterRow).where(SavedFilterRow.user_id == user_id).order_by(SavedFilterRow.id)
    )
    return SavedFilters(data=[_saved(row) for row in rows])


@router.get(
    "",
    operation_id="getSavedFilters",
    response_model=SavedFilters,
    # A parameter left at the screener's default is left out, not null.
    response_model_exclude_none=True,
    summary="The signed-in user's saved screener filters",
    responses=UNAUTHORIZED,
)
async def read_saved_filters(viewer: ViewerDep, session: SessionDep) -> SavedFilters:
    """Every filter saved, oldest first. None saved is an empty list, never a 404."""
    return await _all(session, viewer.user.id)


@router.post(
    "",
    operation_id="saveFilter",
    response_model=SavedFilter,
    response_model_exclude_none=True,
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
    """Saves `query` as `name`, after the others. As many as the plan's
    `screener_saved_filters` allows (3 on Free): one more is a 402
    `plan_limit` naming the plan that keeps more, and one past 100, the most
    any plan keeps, a 422. An account whose email is not verified is a 403
    `email_unverified`."""
    user_id = viewer.user.id
    # One save at a time per account, so two at once cannot both pass the
    # count. The user's row rather than the filters': there may be none.
    await session.execute(select(AppUser.id).where(AppUser.id == user_id).with_for_update())
    mine = SavedFilterRow.user_id == user_id
    count = await session.scalar(select(func.count()).select_from(SavedFilterRow).where(mine)) or 0
    if count >= MAX_SAVED_FILTERS:
        raise invalid("query", f"at most {MAX_SAVED_FILTERS} filters are kept", where="body")
    entitled.require_room("screener_saved_filters", count, what="saved filters")
    row = SavedFilterRow(
        user_id=user_id,
        name=body.name,
        query=body.query.model_dump(mode="json", exclude_none=True),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _saved(row)


FilterId = Annotated[int, Path(ge=1, description="`id` from the list.", examples=[12])]


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
