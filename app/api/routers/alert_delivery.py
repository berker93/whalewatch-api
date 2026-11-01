"""``/v1/me/alert-delivery``: when the signed-in user's alert email may go.

``GET``
    Their quiet hours and the zone they are kept in, and the rules every
    alert email goes by: the batch window, the daily cap and the digest's
    hour (:mod:`app.alerts.outbox`).
``PATCH``
    Change the zone or the quiet hours. Applies to the next email: one
    waiting goes by the new hours.

Not the account's ``time_zone`` (``/v1/account``): that is the clock dates
are shown in, the market's unless the user changes it. This is the one they
sleep in, which the browser said at signup.
"""

from datetime import time

from fastapi import APIRouter

from app.accounts import rules
from app.alerts.outbox import BATCH_WINDOW, DIGEST_AT, EMAILS_PER_DAY
from app.api.auth import UNAUTHORIZED, NoStoreDep, ViewerDep
from app.api.deps import SessionDep
from app.api.errors import INVALID, invalid
from app.api.schemas.alert_delivery import AlertDelivery, AlertDeliveryPatch
from app.db.models import AppUser

router = APIRouter(prefix="/me/alert-delivery", tags=["alert-rules"], dependencies=[NoStoreDep])


def _clock(value: time) -> str:
    return f"{value:%H:%M}"


def _delivery(user: AppUser) -> AlertDelivery:
    return AlertDelivery(
        time_zone=user.quiet_time_zone,
        quiet_start=_clock(user.quiet_start),
        quiet_end=_clock(user.quiet_end),
        batch_minutes=int(BATCH_WINDOW.total_seconds() // 60),
        emails_per_day=EMAILS_PER_DAY,
        digest_at=_clock(DIGEST_AT),
    )


@router.get(
    "",
    operation_id="getAlertDelivery",
    response_model=AlertDelivery,
    summary="When alert email may go",
    responses=UNAUTHORIZED,
)
async def read_alert_delivery(viewer: ViewerDep) -> AlertDelivery:
    """The quiet hours, their zone, and the batch window, daily cap and digest
    hour every alert email goes by."""
    return _delivery(viewer.user)


@router.patch(
    "",
    operation_id="updateAlertDelivery",
    response_model=AlertDelivery,
    summary="Change the quiet hours or their zone",
    responses={**UNAUTHORIZED, **INVALID},
)
async def update_alert_delivery(
    body: AlertDeliveryPatch, viewer: ViewerDep, session: SessionDep
) -> AlertDelivery:
    """Fields left out, or null, stay as they are. `time_zone` must be an IANA
    zone, and the hours `HH:MM`, 24-hour; `quiet_start` equal to `quiet_end`
    turns quiet hours off. Needs no verified email, as the account's own
    settings do not. The next alert email goes by them."""
    user = viewer.user
    if body.time_zone is not None:
        if problem := await rules.time_zone_problem(session, body.time_zone):
            raise invalid("time_zone", problem, where="body")
        user.quiet_time_zone = body.time_zone
    if body.quiet_start is not None:
        user.quiet_start = time.fromisoformat(body.quiet_start)
    if body.quiet_end is not None:
        user.quiet_end = time.fromisoformat(body.quiet_end)
    await session.commit()
    return _delivery(user)
