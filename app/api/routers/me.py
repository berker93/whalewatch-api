"""``GET /v1/me``: the signed-in user, their plan and their entitlements, in one call.

What an app page needs to decide what to show before it shows anything: who
is signed in, whether their email is verified, and what their plan lets them
do. One indexed read of the user; the session is already in Redis, and the
plan and entitlements are worked out from the user (:mod:`app.accounts.plans`).
"""

from fastapi import APIRouter

from app.accounts.plans import entitlements_for, plan_for
from app.api.auth import UNAUTHORIZED, NoStoreDep, ViewerDep
from app.api.schemas.me import Me, MeEntitlements, MePlan, MeUser

router = APIRouter(prefix="/me", tags=["account"], dependencies=[NoStoreDep])


@router.get(
    "",
    operation_id="getMe",
    response_model=Me,
    summary="The signed-in user, their plan and what it lets them do",
    responses=UNAUTHORIZED,
)
async def read_me(viewer: ViewerDep) -> Me:
    """A 401 `unauthenticated` signed out, `session_expired` when the session
    has ended. `entitlements.save` is false until the email is verified: the
    account reads everything and saves nothing."""
    user = viewer.user
    plan = plan_for(user)
    entitlements = entitlements_for(user)
    return Me(
        user=MeUser(
            email=user.email,
            name=user.name,
            time_zone=user.time_zone,
            email_verified=user.email_verified_at is not None,
            email_verified_at=user.email_verified_at,
            created_at=user.created_at,
            last_seen_at=user.last_seen_at,
        ),
        plan=MePlan(id=plan.id, name=plan.name),
        entitlements=MeEntitlements(
            save=entitlements.save, watchlist_max_items=entitlements.watchlist_max_items
        ),
    )
