"""The signed-in user's API keys.

``GET /v1/api-keys``
    The keys that work, oldest first: name, prefix, made, last used. Never
    the key itself, which is not kept.
``POST /v1/api-keys``
    Make one, and answer with the key, the only time it is shown. Needs a
    verified email and a plan with API calls (``api_calls_month`` above 0): on
    one without, a 402 naming the cheapest plan with some. At most
    :data:`~app.accounts.api_keys.MAX_API_KEYS` at once, which no plan lifts: one
    more is a 422.
``DELETE /v1/api-keys/{id}``
    Revoke one: it stops working at once. Idempotent: one already revoked, or
    never the user's, is no change. Works unverified, as withdrawing anything does.

Every one of these wants the session cookie: an API key cannot make, list or
revoke keys (:mod:`app.accounts.api_keys`).
"""

from typing import Annotated

from fastapi import APIRouter, Path, Response, status
from sqlalchemy import select

from app.accounts import api_keys, entitlements
from app.accounts.api_keys import MAX_API_KEYS
from app.api.auth import UNAUTHORIZED, UNVERIFIED, NoStoreDep, VerifiedViewerDep, ViewerDep
from app.api.deps import SessionDep
from app.api.entitlements import PLAN_LIMITED, plan_limit_error
from app.api.errors import INVALID, invalid
from app.api.schemas.usage import ApiKey, ApiKeys, CreatedApiKey, NewApiKey
from app.db.models import ApiKey as ApiKeyRow
from app.db.models import AppUser

router = APIRouter(prefix="/api-keys", tags=["api-keys"], dependencies=[NoStoreDep])


def _key(row: ApiKeyRow) -> ApiKey:
    return ApiKey(
        id=str(row.id),
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


@router.get(
    "",
    operation_id="getApiKeys",
    response_model=ApiKeys,
    summary="The signed-in user's API keys",
    responses=UNAUTHORIZED,
)
async def read_api_keys(viewer: ViewerDep, session: SessionDep) -> ApiKeys:
    """Every key that works, oldest first. None is an empty list, never a 404.
    Revoked keys are not listed."""
    return ApiKeys(data=[_key(row) for row in await api_keys.live_keys(session, viewer.user.id)])


@router.post(
    "",
    operation_id="createApiKey",
    response_model=CreatedApiKey,
    status_code=status.HTTP_201_CREATED,
    summary="Make an API key",
    responses={**UNAUTHORIZED, **PLAN_LIMITED, **UNVERIFIED, **INVALID},
)
async def create_api_key(
    body: NewApiKey, viewer: VerifiedViewerDep, session: SessionDep
) -> CreatedApiKey:
    """Makes a key called `name` and answers with it in `key`: copy it now,
    it is never shown again. Send it as `Authorization: Bearer <key>`. A plan
    with no API calls is a 402 `plan_limit`; more than 10 keys at once, a 422;
    an account whose email is not verified, a 403 `email_unverified`."""
    user_id = viewer.user.id
    if entitlements.limit(viewer, "api_calls_month") == 0:
        raise plan_limit_error(
            "api_calls_month", allowed=0, needed=1, message="Your plan has no API calls."
        )
    # One at a time per account, so two at once cannot both pass the count.
    await session.execute(select(AppUser.id).where(AppUser.id == user_id).with_for_update())
    if await api_keys.count_live(session, user_id) >= MAX_API_KEYS:
        raise invalid(
            "name", f"at most {MAX_API_KEYS} keys work at once: revoke one first", where="body"
        )
    row, token = await api_keys.create_key(session, user_id, body.name)
    await session.commit()
    await session.refresh(row)
    return CreatedApiKey(**_key(row).model_dump(), key=token)


KeyId = Annotated[int, Path(ge=1, description="`id` from the list.", examples=[7])]


@router.delete(
    "/{key_id}",
    operation_id="revokeApiKey",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke an API key",
    responses={**UNAUTHORIZED, **INVALID},
)
async def revoke_api_key(key_id: KeyId, viewer: ViewerDep, session: SessionDep) -> None:
    """It stops working at once. One already revoked, or another user's, is
    no change: 204 either way."""
    await api_keys.revoke_key(session, viewer.user.id, key_id)
    await session.commit()
