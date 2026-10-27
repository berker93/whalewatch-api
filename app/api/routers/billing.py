"""Paying for a plan, through Paddle as merchant of record.

``GET /v1/billing``
    Whether payments work, and the account's subscription: its plan, status,
    period, a scheduled cancellation, and an unpaid renewal's grace. What an
    account page shows, and what it asks again after a checkout until the
    webhook has landed.
``POST /v1/billing/checkout``
    A Paddle checkout link for one plan, carrying the account's id. Paying
    there changes nothing by itself: the plan changes when Paddle's webhook
    says the subscription exists. Needs a verified email, and no live
    subscription: changing plan is the portal's.
``POST /v1/billing/portal``
    A Paddle customer portal session: change plan, update the card, cancel,
    invoices. Works unverified, as withdrawing anything does.
``POST /webhooks/paddle``
    Paddle's notifications. The signature is checked against the raw body
    before anything else reads it, the body is parsed, and only then is the
    database touched (:mod:`app.accounts.billing`). Outside ``/v1`` and the
    OpenAPI document: no client of this API calls it. Not rate limited: Paddle
    sends from a few addresses, in bursts.
"""

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy import select

from app.accounts import billing, entitlements
from app.accounts.billing import BillingError
from app.accounts.mail import deliver, mailer_for
from app.accounts.paddle import (
    Notification,
    PaddleClient,
    PaddleError,
    SignatureError,
    http_client,
    parse_notification,
    verify_signature,
)
from app.accounts.sessions import SessionStore, SessionStoreError
from app.api.auth import (
    UNAUTHORIZED,
    UNVERIFIED,
    NoStoreDep,
    SessionStoreDep,
    VerifiedViewerDep,
    ViewerDep,
)
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import INTERNAL_ERROR_MESSAGE, INVALID, ApiError, Responses, invalid, not_found
from app.api.schemas.billing import (
    Billing,
    BillingSubscription,
    CheckoutLink,
    CheckoutRequest,
    PortalLink,
)
from app.api.schemas.error import ErrorCode, Problem
from app.api.schemas.me import MePlan
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import LIVE_STATUSES, Plan, Subscription

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"], dependencies=[NoStoreDep])
webhook_router = APIRouter(include_in_schema=False)

#: Where Paddle posts its notifications: the notification destination's URL.
WEBHOOK_PATH = "/webhooks/paddle"

BILLING_UNAVAILABLE: Responses = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": Problem,
        "description": (
            "`billing_unavailable`: payments are not set up here, or Paddle could not "
            "be reached. Try again shortly."
        ),
    }
}


def _unavailable() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.BILLING_UNAVAILABLE,
        "Payments are unavailable right now. Try again shortly.",
        headers={"retry-after": "30"},
    )


async def get_paddle(settings: SettingsDep) -> AsyncIterator[PaddleClient]:
    """Paddle's API for one request; a 503 where billing is not configured."""
    if not settings.billing_enabled:
        raise _unavailable()
    async with http_client(settings) as http:
        yield PaddleClient(http)


PaddleDep = Annotated[PaddleClient, Depends(get_paddle)]


async def get_optional_paddle(settings: SettingsDep) -> AsyncIterator[PaddleClient | None]:
    """Paddle's API for one request, or None where billing is not configured:
    for a route that only sometimes needs it."""
    if not settings.billing_enabled:
        yield None
        return
    async with http_client(settings) as http:
        yield PaddleClient(http)


OptionalPaddleDep = Annotated[PaddleClient | None, Depends(get_optional_paddle)]


# --- the account's side -------------------------------------------------------------


def _subscription(row: Subscription, plan: Plan, now: datetime) -> BillingSubscription:
    live = row.status in LIVE_STATUSES
    grants = (
        live
        and (row.cancel_at is None or row.cancel_at > now)
        and (row.grace_ends_at is None or row.grace_ends_at > now)
    )
    return BillingSubscription(
        plan=MePlan(id=plan.slug, name=plan.name),
        status=row.status,
        current_period_end=row.current_period_end,
        cancel_at=row.cancel_at,
        grace_ends_at=row.grace_ends_at,
        grants_plan=grants,
        manageable=row.paddle_customer_id is not None,
    )


@router.get(
    "",
    operation_id="getBilling",
    response_model=Billing,
    summary="The account's subscription, and whether payments work",
    responses=UNAUTHORIZED,
)
async def read_billing(viewer: ViewerDep, session: SessionDep, settings: SettingsDep) -> Billing:
    """`subscription` is null for an account that never had one. Read from
    the payment provider's webhooks: after a checkout, ask again until it
    shows the plan bought."""
    row = await billing.subscription_of(session, viewer.user.id)
    subscription = None
    if row is not None:
        plan = await session.get(Plan, row.plan_id)
        assert plan is not None
        subscription = _subscription(row, plan, datetime.now(UTC))
    return Billing(enabled=settings.billing_enabled, subscription=subscription)


@router.post(
    "/checkout",
    operation_id="createCheckout",
    response_model=CheckoutLink,
    summary="A checkout link for a plan",
    responses={
        **UNAUTHORIZED,
        **UNVERIFIED,
        **INVALID,
        **BILLING_UNAVAILABLE,
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": (
                "`already_subscribed`: the account has a subscription. Change plan, or "
                "fix a failed payment, in the portal (`POST /v1/billing/portal`)."
            ),
        },
    },
)
async def create_checkout(
    body: CheckoutRequest,
    viewer: VerifiedViewerDep,
    session: SessionDep,
    settings: SettingsDep,
    paddle: PaddleDep,
) -> CheckoutLink:
    """Open `url`. The account is on the plan once the payment provider
    confirms the payment by webhook, usually within seconds: `GET
    /v1/billing` says when. A plan that is not for sale is a 422; an account
    whose email is not verified, a 403."""
    terms = entitlements.current().plans.get(body.plan)
    price_id = settings.paddle_prices.get(body.plan)
    if terms is None or not terms.is_public or terms.price_cents == 0 or price_id is None:
        raise invalid("plan", f"{body.plan!r} is not a plan for sale", where="body")
    user_id = viewer.user.id
    live = await session.scalar(
        select(Subscription.id).where(
            Subscription.user_id == user_id, Subscription.status.in_(LIVE_STATUSES)
        )
    )
    if live is not None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            ErrorCode.ALREADY_SUBSCRIBED,
            "The account already has a subscription: change it in the customer portal.",
        )
    paid_before = await billing.customer_of(session, user_id)
    try:
        checkout = await paddle.create_checkout(
            price_id=price_id,
            user_id=user_id,
            customer_id=paid_before.paddle_customer_id if paid_before else None,
        )
    except PaddleError as exc:
        logger.error("billing.checkout_failed", error=str(exc), status=exc.status, code=exc.code)
        raise _unavailable() from exc
    return CheckoutLink(url=checkout.url, transaction_id=checkout.transaction_id)


@router.post(
    "/portal",
    operation_id="createPortalSession",
    response_model=PortalLink,
    summary="A link into the payment provider's customer portal",
    responses={
        **UNAUTHORIZED,
        **BILLING_UNAVAILABLE,
        **not_found("The account has never paid: there is nothing to manage."),
    },
)
async def create_portal_session(
    viewer: ViewerDep, session: SessionDep, paddle: PaddleDep
) -> PortalLink:
    """Change plan, update the card, cancel, or download invoices there. The
    links work for a short while: ask for new ones each time. What changes
    reaches the account by webhook, as a checkout does."""
    row = await billing.customer_of(session, viewer.user.id)
    if row is None or row.paddle_customer_id is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            ErrorCode.NOT_FOUND,
            "The account has never paid: there is nothing to manage.",
        )
    live = row.status in LIVE_STATUSES
    try:
        portal = await paddle.create_portal_session(
            customer_id=row.paddle_customer_id,
            subscription_id=row.paddle_subscription_id if live else None,
        )
    except PaddleError as exc:
        logger.error("billing.portal_failed", error=str(exc), status=exc.status, code=exc.code)
        raise _unavailable() from exc
    return PortalLink(
        url=portal.url,
        cancel_url=portal.cancel_url,
        update_payment_method_url=portal.update_payment_method_url,
    )


# --- Paddle's side ------------------------------------------------------------------


async def verified_notification(request: Request, settings: SettingsDep) -> Notification:
    """The notification in the request, once its signature is Paddle's.

    The first thing the webhook does: nothing reads the body, the database or
    Redis before this has checked the bytes as they arrived.
    """
    if not settings.billing_enabled:
        # 503, so Paddle retries what it sent before billing was configured.
        raise _unavailable()
    assert settings.paddle_webhook_secret is not None
    body = await request.body()
    try:
        verify_signature(
            request.headers.get("paddle-signature"),
            body,
            settings.paddle_webhook_secret.get_secret_value(),
            now=time.time(),
            tolerance=settings.paddle_signature_tolerance_seconds,
        )
    except SignatureError as exc:
        logger.warning("billing.signature_refused", reason=str(exc))
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.INVALID_SIGNATURE,
            "The Paddle-Signature header does not sign this body.",
        ) from None
    try:
        return parse_notification(body)
    except ValueError as exc:
        logger.error("billing.malformed_notification", error=str(exc))
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, ErrorCode.VALIDATION_ERROR, "Not a Paddle notification."
        ) from None


@webhook_router.post(WEBHOOK_PATH, status_code=status.HTTP_200_OK)
async def paddle_webhook(
    # First: the signature is checked before the parameters below are built.
    notification: Annotated[Notification, Depends(verified_notification)],
    session: SessionDep,
    store: SessionStoreDep,
    settings: SettingsDep,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Apply the event once; a 2xx tells Paddle it is done. A 500 has it retry."""
    try:
        handled = await billing.handle(session, settings, notification)
        await session.commit()
    except BillingError as exc:
        await session.rollback()
        logger.error(
            "billing.event_failed",
            event_id=notification.event_id,
            type=notification.event_type,
            error=str(exc),
        )
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE
        ) from exc
    await _forget_grants(store, handled.users)
    _send_later(background, settings, handled)
    return {
        "event_id": notification.event_id,
        "outcome": handled.outcome.value,
        "duplicate": handled.duplicate,
    }


async def _forget_grants(store: SessionStore, users: set[int]) -> None:
    """Have the users' sessions read their plan again. A Redis down is not
    worth a retry from Paddle: a session re-reads its grant within minutes anyway."""
    for user_id in sorted(users):
        try:
            await store.forget_grants(user_id)
        except SessionStoreError as exc:
            logger.warning("billing.grants_not_forgotten", user_id=user_id, error=repr(exc))


def _send_later(background: BackgroundTasks, settings: Settings, handled: billing.Handled) -> None:
    if not handled.mail:
        return
    mailer = mailer_for(settings)
    for message in handled.mail:
        background.add_task(deliver, mailer, message)
