"""Leaving an email's list, and what the mail provider says happened to each message.

``GET /v1/email/unsubscribe?token=``
    What an unsubscribe link leaves, and whether its account is still on it:
    what whalewatch-web's ``/unsubscribe`` page reads to say "Unsubscribe
    from the 13F season digest?". Changes nothing, so a mail scanner that
    follows every link in a message unsubscribes nobody.
``POST /v1/email/unsubscribe?token=``
    Leave it, at once: the next email on the list is not sent. Both the web
    page's button and the one-click ``List-Unsubscribe-Post`` header (RFC 8058)
    post here, the latter from the mailbox provider's servers with
    ``List-Unsubscribe=One-Click`` as a form body, no cookie and no origin.
    The token is all the proof needed; it unsubscribes that list and does
    nothing else. Leaving a list already left is the same answer.
``POST /webhooks/postmark``
    Postmark's Delivery, Bounce, SpamComplaint and SubscriptionChange
    webhooks (:mod:`app.accounts.postmark`). HTTP basic auth, checked before
    the body is read; outside ``/v1``, the OpenAPI document and the rate limit,
    like Paddle's. A 2xx tells Postmark it is done, so every event this API
    does not record is a 200 too.
"""

import base64
import binascii
import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.accounts import email_lists
from app.accounts.delivery import record_event
from app.accounts.postmark import WEBHOOK_USER, parse_event
from app.accounts.unsubscribe import InvalidTokenError, MailingList, read_token
from app.api.auth import NoStoreDep
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import ApiError, Responses
from app.api.schemas.email import EmailSubscription
from app.api.schemas.error import ErrorCode, FieldError, ValidationDetail, ValidationProblem
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/email", tags=["email"], dependencies=[NoStoreDep])
webhook_router = APIRouter(include_in_schema=False)

#: Where Postmark posts its webhooks, with the basic auth in the URL it is given.
WEBHOOK_PATH = "/webhooks/postmark"

_INVALID_TOKEN: Responses = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ValidationProblem,
        "description": (
            "`invalid_token`: not an unsubscribe link this site sent, or one cut short "
            "by the mail client. The account's email settings stop the same emails."
        ),
    }
}

Token = Annotated[
    str,
    Query(
        min_length=1,
        max_length=200,
        description="The `token` of the unsubscribe link, as the email has it.",
    ),
]


def _list_of(settings: SettingsDep, token: Token) -> tuple[int, MailingList]:
    try:
        return read_token(settings, token)
    except InvalidTokenError as exc:
        message = f"the unsubscribe link is not valid: {exc}"
        raise ApiError(
            422,
            ErrorCode.INVALID_TOKEN,
            f"token: {message}",
            detail=ValidationDetail(
                errors=[
                    FieldError(
                        loc=["query", "token"], msg=message, type=ErrorCode.INVALID_TOKEN.value
                    )
                ]
            ),
        ) from None


ListDep = Annotated[tuple[int, MailingList], Depends(_list_of)]


@router.get(
    "/unsubscribe",
    operation_id="getEmailSubscription",
    response_model=EmailSubscription,
    summary="What an email's unsubscribe link leaves, and whether it is left",
    responses=_INVALID_TOKEN,
)
async def read_subscription(found: ListDep, session: SessionDep) -> EmailSubscription:
    """Needs no session: the link's token says which account and which list.
    An alert since deleted, or an account since deleted, is not subscribed."""
    user_id, mailing_list = found
    return EmailSubscription.of(await email_lists.standing(session, user_id, mailing_list))


@router.post(
    "/unsubscribe",
    operation_id="unsubscribeEmail",
    response_model=EmailSubscription,
    summary="Leave an email's list, from its link or its one-click header",
    responses=_INVALID_TOKEN,
)
async def unsubscribe(found: ListDep, session: SessionDep) -> EmailSubscription:
    """Needs no session and reads no body: a mailbox provider's one-click
    unsubscribe posts `List-Unsubscribe=One-Click` here from its own servers.
    From the moment this answers, no email on the list is sent, however long
    ago it was queued. An alert's list stops that alert's email and keeps it in
    the notification centre; an alert that only emailed is turned off. The
    account's own emails (verification, password reset, billing) are on no list
    and are never stopped from here."""
    user_id, mailing_list = found
    standing = await email_lists.leave(session, user_id, mailing_list)
    await session.commit()
    logger.info("mail.unsubscribed", user_id=user_id, mailing_list=mailing_list.id)
    return EmailSubscription.of(standing)


# --- Postmark's side -------------------------------------------------------------------


def _authorised(request: Request, settings: Settings) -> bool:
    assert settings.postmark_webhook_secret is not None  # Settings refuses postmark without it
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(credentials, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    expected = f"{WEBHOOK_USER}:{settings.postmark_webhook_secret.get_secret_value()}"
    return hmac.compare_digest(decoded.encode(), expected.encode())


@webhook_router.post(WEBHOOK_PATH, status_code=status.HTTP_200_OK)
async def postmark_webhook(
    request: Request, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Apply the event once; applying it twice changes nothing more. A 500 has
    Postmark retry it."""
    if settings.email_backend != "postmark":
        # 503, so Postmark retries what it sent before this API was told of it.
        raise ApiError(503, ErrorCode.UNAVAILABLE, "Email is not sent through Postmark here.")
    if not _authorised(request, settings):
        logger.warning("mail.webhook_refused")
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Not Postmark's credentials.")
    try:
        event = parse_event(settings, await request.body())
    except ValueError as exc:
        logger.error("mail.webhook_malformed", error=str(exc))
        raise ApiError(400, ErrorCode.VALIDATION_ERROR, "Not a Postmark webhook.") from None
    recorded = await record_event(session, event)
    await session.commit()
    logger.info(
        "mail.webhook",
        record_type=event.record_type,
        kind=event.kind.value,
        email_log_id=recorded.email_log_id,
        status=recorded.status,
        suppression=recorded.suppression,
        lifted=recorded.lifted,
    )
    return {
        "event": event.kind.value,
        "email_log_id": recorded.email_log_id,
        "status": recorded.status,
        "suppression": recorded.suppression,
    }
