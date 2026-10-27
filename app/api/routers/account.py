"""The signed-in user's account: ``GET`` and ``PATCH /v1/account``, and confirming an email change.

``GET /v1/account``
    The profile. Read on every app page, beside the watchlist, for the header's
    initials and the zone every time is shown in: one indexed read of the
    session joined to its user, and one of a waiting email change.
``PATCH /v1/account``
    Changes ``name`` and ``time_zone`` at once. A new ``email`` is not applied:
    a confirmation link goes to it and a notice to the current address, and
    ``pending_email`` names it until it is confirmed.
``DELETE /v1/account``
    Deletes the account, with its password: signed out everywhere at once, a
    paid subscription cancelled in Paddle, the rows erased thirty days later
    (:mod:`app.accounts.privacy`).
``POST /v1/account/email/confirm``
    The confirmation link's token. Needs no session, since the link may be
    opened on another device; the token is the proof, and the account's owner
    was signed in when they asked for it.

A ``PATCH`` is all or nothing. Every field is checked before any is applied,
and every refusal comes back at once, each a ``detail.errors`` entry whose
``loc`` is ``["body", "<field>"]``, the wire name: the account page shows the
message under the field the last part names. An email on another account is a
422 on ``email``, like any other refusal, not a 409.
"""

from typing import Final

from fastapi import APIRouter, BackgroundTasks, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import billing, privacy, rules
from app.accounts.mail import (
    account_deleted,
    deliver,
    email_change_confirmation,
    email_change_notice,
    mailer_for,
)
from app.accounts.passwords import verify_password
from app.accounts.sessions import (
    EMAIL_CHANGE_TTL,
    issue_token,
    now,
    pending_email,
    redeem_token,
    revoke_tokens,
)
from app.api.auth import (
    UNAUTHORIZED,
    AuthRateLimitDep,
    NoStoreDep,
    SessionStoreDep,
    ViewerDep,
    clear_session_cookie,
)
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import INVALID, ApiError, Responses
from app.api.routers.auth import refused
from app.api.routers.billing import OptionalPaddleDep
from app.api.schemas.account import AccountPatch, AccountProfile, AccountUpdate, EmailConfirm
from app.api.schemas.error import ErrorCode, Problem, ValidationProblem
from app.api.schemas.privacy import AccountDeleteRequest
from app.db.models import AppUser, DeletionRequester, TokenPurpose

router = APIRouter(prefix="/account", tags=["account"], dependencies=[NoStoreDep])

_REFUSED: Final[Responses] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ValidationProblem,
        "description": (
            "A field was refused. `detail.errors` has one entry per field, its `loc` "
            '`["body", "<field>"]`.'
        ),
    }
}
_FORBIDDEN_ORIGIN: Final[Responses] = {
    status.HTTP_403_FORBIDDEN: {
        "model": Problem,
        "description": "`forbidden_origin`: a write from a site not allowed to call.",
    }
}


async def _profile(session: AsyncSession, user: AppUser) -> AccountProfile:
    return AccountProfile(
        name=user.name,
        email=user.email,
        time_zone=user.time_zone,
        member_since=user.created_at,
        password_changed_at=user.password_changed_at,
        pending_email=await pending_email(session, user.id),
    )


async def _email_taken(session: AsyncSession, email: str, *, by_other_than: int) -> bool:
    found = await session.scalar(
        select(func.count())
        .select_from(AppUser)
        .where(AppUser.email == email, AppUser.id != by_other_than, AppUser.deleted_at.is_(None))
    )
    return bool(found)


@router.get(
    "",
    operation_id="getAccount",
    response_model=AccountProfile,
    summary="The signed-in user's profile",
    responses=UNAUTHORIZED,
)
async def read_account(viewer: ViewerDep, session: SessionDep) -> AccountProfile:
    """Name, email, time zone, and when the account was made and its password
    last changed. `pending_email` while an email change waits for its link."""
    return await _profile(session, viewer.user)


@router.patch(
    "",
    operation_id="updateAccount",
    response_model=AccountUpdate,
    summary="Change the name, the time zone, or (once confirmed) the email",
    responses={**UNAUTHORIZED, **_FORBIDDEN_ORIGIN, **_REFUSED},
)
async def update_account(
    body: AccountPatch, viewer: ViewerDep, session: SessionDep, settings: SettingsDep
) -> AccountUpdate:
    """Fields left out, or null, stay as they are. `name` is stored trimmed and
    `time_zone` must be an IANA zone; both apply at once. A new `email` is
    stored as typed and waits: `profile.email` keeps the current one,
    `pending_email` names the new one, a link goes to it and a notice to the
    current address. A second change replaces the first. The current address
    sent again is no change, and cancels one waiting. One refused field and none
    is applied."""
    user = viewer.user
    errors: list[dict[str, object]] = []

    name = None if body.name is None else rules.normalise_name(body.name)
    if name is not None and (problem := rules.name_problem(name)):
        errors.append(_error("name", problem))

    email = None if body.email is None else rules.normalise_email(body.email)
    # Compared as the citext column compares: a change of case alone is the
    # same login, and no change.
    same_email = email is not None and email.lower() == user.email.lower()
    if email is not None and not same_email:
        if problem := rules.email_problem(email):
            errors.append(_error("email", problem))
        elif await _email_taken(session, email, by_other_than=user.id):
            errors.append(_error("email", "is already on another account"))

    zone = body.time_zone
    if zone is not None and (problem := await rules.time_zone_problem(session, zone)):
        errors.append(_error("time_zone", problem))

    if errors:
        raise RequestValidationError(errors)

    if name is not None:
        user.name = name
    if zone is not None:
        user.time_zone = zone

    token: str | None = None
    if same_email:
        await revoke_tokens(session, user.id, TokenPurpose.EMAIL_CHANGE)
    elif email is not None:
        token = await issue_token(
            session, user.id, TokenPurpose.EMAIL_CHANGE, EMAIL_CHANGE_TTL, new_email=email
        )
    await session.commit()

    if token is not None and email is not None:
        mailer = mailer_for(settings)
        hours = int(EMAIL_CHANGE_TTL.total_seconds() // 3600)
        await deliver(
            mailer, email_change_confirmation(settings, to=email, token=token, hours=hours)
        )
        await deliver(mailer, email_change_notice(to=user.email, new_email=email))

    profile = await _profile(session, user)
    return AccountUpdate(profile=profile, pending_email=profile.pending_email)


@router.delete(
    "",
    operation_id="deleteAccount",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete the account: signed out at once, erased after 30 days",
    responses={
        **UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: {
            "model": Problem,
            "description": (
                "`invalid_credentials`: not the account's password; `forbidden_origin`: "
                "a write from a site not allowed to call."
            ),
        },
        **INVALID,
    },
    dependencies=[AuthRateLimitDep],
)
async def delete_account(
    body: AccountDeleteRequest,
    response: Response,
    background: BackgroundTasks,
    viewer: ViewerDep,
    session: SessionDep,
    store: SessionStoreDep,
    settings: SettingsDep,
    paddle: OptionalPaddleDep,
) -> None:
    """Every session ends, this one too, and the cookie is cleared. From then
    on the login signs nobody in, and its address may make a new account. The
    watchlist, the consent records and the account itself are erased 30 days
    later; until then an operator can restore it on request. A record that the
    account was deleted, and when, is kept, with nothing else about it. A
    notice goes to the address. A paid subscription is cancelled at once and
    is not charged again; restoring the account does not bring it back. Works
    unverified: an account that cannot save may still go."""
    user = viewer.user
    if not await verify_password(body.password, user.password_hash):
        raise ApiError(403, ErrorCode.INVALID_CREDENTIALS, "That isn't the account's password.")
    deletion = await privacy.delete_account(
        session, store, user, requested_by=DeletionRequester.USER
    )
    await session.commit()
    clear_session_cookie(response, settings)
    # After the deletion has committed, never before: it does not wait on
    # Paddle. What Paddle cannot cancel now, the hourly billing job retries.
    cancelled: list[str] = []
    if paddle is not None:
        cancelled = (await billing.cancel_for_deleted(session, paddle, user_id=user.id)).plans
        await session.commit()
    background.add_task(
        deliver,
        mailer_for(settings),
        account_deleted(
            to=user.email, purge_after=deletion.purge_after.date(), cancelled_plans=cancelled
        ),
    )


def _error(field: str, message: str) -> dict[str, object]:
    return {"type": "value_error", "loc": ("body", field), "msg": message}


@router.post(
    "/email/confirm",
    operation_id="confirmEmailChange",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Confirm an email change from its link",
    responses={**_FORBIDDEN_ORIGIN, **_REFUSED},
    dependencies=[AuthRateLimitDep],
)
async def confirm_email_change(body: EmailConfirm, session: SessionDep) -> None:
    """Swaps the login to the new address and clears `pending_email`. From
    then on the old address no longer logs in, and every session reports the
    new one. The link reached the new address, so the account's email is
    verified, whether or not the old one was. A token that is unknown, used,
    replaced by a later change, or past its 24 hours is a 422 `invalid_token`;
    an address another account took in the meantime, a 422 `invalid_email`.
    Needs no session."""
    token = await redeem_token(session, body.token, TokenPurpose.EMAIL_CHANGE)
    if token is None or token.new_email is None:
        raise refused(
            ErrorCode.INVALID_TOKEN,
            "token",
            "the link is not valid any more: change the email again",
        )
    if await _email_taken(session, token.new_email, by_other_than=token.user_id):
        # The token is spent either way: the address it named is someone else's.
        await session.commit()
        raise refused(ErrorCode.INVALID_EMAIL, "email", "is already on another account")
    user = await session.get(AppUser, token.user_id)
    assert user is not None  # the token's foreign key cascades
    if user.deleted_at is not None:
        await session.commit()
        raise refused(
            ErrorCode.INVALID_TOKEN, "token", "the link is not valid any more: the account is gone"
        )
    user.email = token.new_email
    user.email_verified_at = now()
    # A verification link for the old address would verify the new one.
    await revoke_tokens(session, user.id, TokenPurpose.EMAIL_VERIFY)
    await session.commit()
