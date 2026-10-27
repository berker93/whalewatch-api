"""Signing in and out, registering, and passwords: the frontend's ``AuthAdapter``.

``POST /v1/auth/login``
    An email and a password, for a session cookie.
``POST /v1/auth/logout``
    Ends the session and clears the cookie. Fine without one.
``GET /v1/auth/session``
    Who the cookie is for, or ``session: null`` without one. A cookie naming a
    session that has ended is a 401 ``session_expired``, so the frontend can say
    so rather than quietly showing a signed-out page.
``POST /v1/auth/register``
    A new account, while ``SIGNUPS_OPEN``, waiting for its address to be
    verified: a link is emailed, and no session starts. An address that already
    has an account gets the same answer, and its owner an email instead, so the
    answer never says which addresses have accounts. Closed, the request is
    recorded. Either way only with the terms accepted, and with marketing email
    as its own box was left (:mod:`app.accounts.privacy`).
``POST /v1/auth/verify-email`` and ``.../resend``
    Verify the address from its link, which starts the session: the address is
    proved before the account is used. Email another link, signed in.
``POST /v1/auth/password``
    Change the password, signed in. Signs out every other session.
``POST /v1/auth/password-reset`` and ``.../confirm``
    Email a reset link, good for an hour and once; set a new password from one.
    The first answers the same whether or not the address has an account, and
    as fast: the email is sent after the answer.

Logins, registrations and reset requests are limited per client IP and per
email address (:func:`app.api.auth.limit_attempts`). A login for an address
with no account does the same argon2 work as one with a wrong password, and
fails with the same answer, so neither the answer nor its timing says which
addresses have accounts.

Failures
--------
The frontend branches on ``code``, so the codes are the contract:
``invalid_credentials`` is 403, never 401, which means only that the session is
gone; ``weak_password``, ``invalid_email``, ``invalid_name`` and
``terms_not_accepted`` are 422s naming the field in ``detail.errors``, as a
validation error does. The routes :func:`~app.api.auth.limit_attempts` does
not cover, but that check a password or a token, are behind the stricter
per-IP limit (:func:`app.api.auth.auth_rate_limited`).
"""

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, BackgroundTasks, Body, Request, Response, status
from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import activity, privacy, rules
from app.accounts.mail import (
    Message,
    deliver,
    email_verification,
    mailer_for,
    password_reset,
    registration_attempt,
)
from app.accounts.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.accounts.sessions import (
    EMAIL_VERIFY_TTL,
    PASSWORD_RESET_TTL,
    find_session,
    issue_token,
    now,
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
    cleared_cookie_header,
    limit_attempts,
    session_token,
    set_session_cookie,
)
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import INVALID, RATE_LIMITED, ApiError, Responses
from app.api.schemas.auth import (
    CheckEmail,
    EmailVerifyRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    Recorded,
    RegisterRequest,
    RegisterResult,
    Session,
    SessionState,
    SessionUser,
)
from app.api.schemas.error import (
    ErrorCode,
    FieldError,
    Problem,
    ValidationDetail,
    ValidationProblem,
)
from app.core.config import Settings
from app.db.models import AppUser, ConsentSource, SignupRequest, TokenPurpose

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[NoStoreDep])

_FORBIDDEN: Final[Responses] = {
    status.HTTP_403_FORBIDDEN: {
        "model": Problem,
        "description": "`invalid_credentials`, or `forbidden_origin` for a write from elsewhere.",
    }
}
_REFUSED: Final[Responses] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ValidationProblem,
        "description": (
            "A field was refused: `weak_password`, `invalid_email`, `invalid_name`, "
            "`terms_not_accepted`, `invalid_token`, or `validation_error` for a malformed "
            "body. "
            "`detail.errors` names the field."
        ),
    }
}


def refused(code: ErrorCode, field: str, message: str) -> ApiError:
    """A 422 for one body field, in the validation error's shape, with its own code."""
    return ApiError(
        422,
        code,
        f"{field}: {message}",
        detail=ValidationDetail(
            errors=[FieldError(loc=["body", field], msg=message, type=code.value)]
        ),
    )


def _invalid_credentials() -> ApiError:
    return ApiError(403, ErrorCode.INVALID_CREDENTIALS, "Wrong email or password.")


def _invalid_token(what: str) -> ApiError:
    return refused(ErrorCode.INVALID_TOKEN, "token", f"the link is not valid any more: {what}")


def _session(user: AppUser, expires_at: datetime) -> Session:
    return Session(
        user=SessionUser(email=user.email, email_verified=user.email_verified_at is not None),
        expires_at=expires_at,
    )


def _live_user(email: str) -> Select[tuple[AppUser]]:
    """The account ``email`` logs in to, compared without case by the column."""
    return select(AppUser).where(AppUser.email == email, AppUser.deleted_at.is_(None))


def _send_verification(
    background: BackgroundTasks, settings: Settings, *, to: str, token: str
) -> None:
    hours = int(EMAIL_VERIFY_TTL.total_seconds() // 3600)
    _send_later(background, settings, email_verification(settings, to=to, token=token, hours=hours))


def _send_later(background: BackgroundTasks, settings: Settings, message: Message) -> None:
    """Send ``message`` once the answer has gone: SMTP takes seconds, and an
    answer that waited for it would take longer when an address has an
    account than when it has none."""
    background.add_task(deliver, mailer_for(settings), message)


@router.post(
    "/login",
    operation_id="login",
    response_model=Session,
    summary="Sign in with an email and a password",
    responses={**_FORBIDDEN, **INVALID, **RATE_LIMITED},
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    store: SessionStoreDep,
    settings: SettingsDep,
) -> Session:
    """Sets the session cookie. A wrong email and a wrong password are the same
    403 `invalid_credentials`, and take as long, so neither says which addresses
    have accounts. An unverified account signs in; it reads but saves nothing.
    At most 5 attempts in 15 minutes from one address, and for one email: then
    a 429 with `Retry-After`."""
    email = rules.normalise_email(body.email)
    await limit_attempts(request, "login", email)
    user = await session.scalar(_live_user(email))
    # The same argon2 work whether or not there is an account.
    matches = await verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not matches:
        raise _invalid_credentials()
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(body.password)
    user.last_seen_at = now()
    await activity.mark_active(session, user.id, user.last_seen_at)
    await session.commit()
    started = await store.start(user.id, remember=body.remember)
    set_session_cookie(response, settings, started)
    return _session(user, started.expires_at)


@router.post(
    "/logout",
    operation_id="logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    responses=_FORBIDDEN,
)
async def logout(
    request: Request, response: Response, store: SessionStoreDep, settings: SettingsDep
) -> None:
    """Ends the session the cookie names and clears the cookie. A 204 with no
    cookie, or one already ended, too: signing out twice is signed out. The
    session is gone at once: the cookie, copied elsewhere, no longer works."""
    if (token := session_token(request, settings)) is not None:
        await store.end(token)
    clear_session_cookie(response, settings)


@router.get(
    "/session",
    operation_id="getSession",
    response_model=SessionState,
    summary="Who the session cookie is for",
    responses=UNAUTHORIZED,
)
async def read_session(
    request: Request, session: SessionDep, store: SessionStoreDep, settings: SettingsDep
) -> SessionState:
    """`session: null` when the request carried no cookie. A 401
    `session_expired` when it carried one for a session that has ended, ran out
    or was signed out, and the cookie is cleared."""
    token = session_token(request, settings)
    if token is None:
        return SessionState(session=None)
    viewer = await find_session(session, store, token)
    if viewer is None:
        raise ApiError(
            401,
            ErrorCode.SESSION_EXPIRED,
            "The session has ended. Log in again.",
            headers={"set-cookie": cleared_cookie_header(settings)},
        )
    return SessionState(session=_session(viewer.user, viewer.expires_at))


@router.post(
    "/register",
    operation_id="register",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RegisterResult,
    summary="Make an account, or ask for one while signups are closed",
    responses={**_REFUSED, **_FORBIDDEN, **RATE_LIMITED},
)
async def register(
    body: RegisterRequest,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
) -> RegisterResult:
    """While signups are closed, `status: recorded`: the request is kept. Open,
    `status: check_email`, and an email goes to the address: a link to verify
    a new account, or, when the address already has an account, a note to its
    owner and nothing made (an unverified one is sent a fresh link). The answer,
    its status and its timing are the same either way, so none of them says
    which addresses have accounts, and no session starts: the link starts it.

    The fields are checked first: `invalid_name`, `invalid_email`,
    `weak_password` and `terms_not_accepted`, in that order. Then at most 5
    attempts in 15 minutes from one address, and for one email.

    The terms and privacy policy in force are recorded as accepted, product
    email as on (legitimate interest, objectable later), and marketing email
    as `marketing_email` says: off unless the user ticked its own box."""
    name = rules.normalise_name(body.name)
    email = rules.normalise_email(body.email)
    if problem := rules.name_problem(name):
        raise refused(ErrorCode.INVALID_NAME, "name", problem)
    if problem := rules.email_problem(email):
        raise refused(ErrorCode.INVALID_EMAIL, "email", problem)
    if problem := rules.password_problem(body.password):
        raise refused(ErrorCode.WEAK_PASSWORD, "password", problem)
    if not body.accept_terms:
        raise refused(
            ErrorCode.TERMS_NOT_ACCEPTED,
            "accept_terms",
            "accept the terms of service and the privacy policy to make an account",
        )
    time_zone = body.time_zone or rules.DEFAULT_TIME_ZONE
    if problem := await rules.time_zone_problem(session, time_zone):
        raise refused(ErrorCode.VALIDATION_ERROR, "time_zone", problem)
    # After the fields, which the form checks too: a typo is not an attempt.
    await limit_attempts(request, "register", email)

    if not settings.signups_open:
        answers = {
            "name": name,
            "terms_version": privacy.TERMS_VERSION,
            "privacy_version": privacy.PRIVACY_VERSION,
            "marketing_email": body.marketing_email,
        }
        await session.execute(
            insert(SignupRequest)
            .values(email=email, **answers)
            .on_conflict_do_update(index_elements=["email"], set_=answers)
        )
        await session.commit()
        return RegisterResult(Recorded(status="recorded"))

    check_email = RegisterResult(CheckEmail(status="check_email"))
    # Before looking, so an address with an account takes the same argon2 work
    # as one without; every email goes after the answer.
    password_hash = await hash_password(body.password)
    existing = await session.scalar(_live_user(email))
    if existing is not None:
        await _tell_existing(session, background, settings, existing)
        return check_email

    joined = now()
    user = AppUser(
        email=email,
        name=name,
        password_hash=password_hash,
        time_zone=time_zone,
        last_seen_at=joined,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Registered by a request that raced this one, which sent the link.
        await session.rollback()
        return check_email
    privacy.record_signup(
        session, user.id, marketing_email=body.marketing_email, source=ConsentSource.REGISTER
    )
    await activity.mark_active(session, user.id, joined)
    await privacy.forget_signup_request(session, email)
    token = await issue_token(session, user.id, TokenPurpose.EMAIL_VERIFY, EMAIL_VERIFY_TTL)
    await session.commit()
    _send_verification(background, settings, to=email, token=token)
    return check_email


async def _tell_existing(
    session: AsyncSession, background: BackgroundTasks, settings: Settings, user: AppUser
) -> None:
    """Registering with an address that has an account changes nothing in it.
    Verified, its owner hears that someone tried. Not yet, the owner is most
    likely registering again for a link that never came: a fresh one goes,
    replacing the last, and the account keeps the name and password it has."""
    if user.email_verified_at is not None:
        _send_later(background, settings, registration_attempt(settings, to=user.email))
        return
    token = await issue_token(session, user.id, TokenPurpose.EMAIL_VERIFY, EMAIL_VERIFY_TTL)
    await session.commit()
    _send_verification(background, settings, to=user.email, token=token)


@router.post(
    "/verify-email",
    operation_id="verifyEmail",
    response_model=Session,
    summary="Verify the account's email from its link, and sign in",
    responses={**_FORBIDDEN, **_REFUSED},
    dependencies=[AuthRateLimitDep],
)
async def verify_email(
    body: EmailVerifyRequest,
    response: Response,
    session: SessionDep,
    store: SessionStoreDep,
    settings: SettingsDep,
) -> Session:
    """Verifies the address and sets the session cookie: following the link is
    how a new account is first signed in. Needs no session: the link may be
    opened on another device, and the token is the proof. A token that is
    unknown, used, replaced by a later one, or past its 24 hours is a 422
    `invalid_token`."""
    token = await redeem_token(session, body.token, TokenPurpose.EMAIL_VERIFY)
    user = None if token is None else await session.get(AppUser, token.user_id)
    if user is None or user.deleted_at is not None:
        raise _invalid_token("ask for a new one")
    if user.email_verified_at is None:
        user.email_verified_at = now()
    user.last_seen_at = now()
    await activity.mark_active(session, user.id, user.last_seen_at)
    await session.commit()
    started = await store.start(user.id, remember=True)
    set_session_cookie(response, settings, started)
    return _session(user, started.expires_at)


@router.post(
    "/verify-email/resend",
    operation_id="resendVerificationEmail",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Email another link to verify the account's email",
    responses={**UNAUTHORIZED, **_FORBIDDEN, **RATE_LIMITED},
)
async def resend_verification(
    request: Request,
    background: BackgroundTasks,
    viewer: ViewerDep,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """A fresh link, valid for 24 hours, replacing any sent before. Already
    verified: no change, and nothing sent. At most 5 in 15 minutes."""
    user = viewer.user
    if user.email_verified_at is not None:
        return
    await limit_attempts(request, "verify_email", user.email)
    token = await issue_token(session, user.id, TokenPurpose.EMAIL_VERIFY, EMAIL_VERIFY_TTL)
    await session.commit()
    _send_verification(background, settings, to=user.email, token=token)


@router.post(
    "/password",
    operation_id="changePassword",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the password, and sign out every other session",
    responses={**UNAUTHORIZED, **_FORBIDDEN, **_REFUSED},
    dependencies=[AuthRateLimitDep],
)
async def change_password(
    body: PasswordChangeRequest, viewer: ViewerDep, session: SessionDep, store: SessionStoreDep
) -> None:
    """A wrong `current_password` is a 403 `invalid_credentials`, never a 401,
    which would mean the session is gone. A `new_password` shorter than the
    rule, or the same as the current one, is a 422 `weak_password`. On success
    `password_changed_at` is now, every other session is signed out, and this
    one stays."""
    user = viewer.user
    if not await verify_password(body.current_password, user.password_hash):
        raise ApiError(403, ErrorCode.INVALID_CREDENTIALS, "That isn't the current password.")
    if problem := rules.password_problem(body.new_password):
        raise refused(ErrorCode.WEAK_PASSWORD, "new_password", problem)
    if body.new_password == body.current_password:
        raise refused(
            ErrorCode.WEAK_PASSWORD, "new_password", "must differ from the current password"
        )
    await _set_password(session, user.id, body.new_password)
    # Before the commit: a Redis that cannot be reached leaves the old
    # password in place, rather than a new one with the old sessions still in.
    await store.end_all(user.id, keep=viewer.session_id)
    await session.commit()


@router.post(
    "/password-reset",
    operation_id="requestPasswordReset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Email a password reset link",
    responses={**_FORBIDDEN, **INVALID, **RATE_LIMITED},
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """A 204 whether or not the address has an account, so the answer reveals
    none, and as fast: the email goes after the answer. When it has one, a link
    valid for an hour and for one use goes to it, replacing any sent before.
    At most 5 in 15 minutes from one address, and for one email."""
    email = rules.normalise_email(body.email)
    await limit_attempts(request, "password_reset", email)
    user = await session.scalar(_live_user(email))
    if user is None:
        return
    token = await issue_token(session, user.id, TokenPurpose.PASSWORD_RESET, PASSWORD_RESET_TTL)
    await session.commit()
    minutes = int(PASSWORD_RESET_TTL.total_seconds() // 60)
    _send_later(
        background,
        settings,
        password_reset(settings, to=user.email, token=token, minutes=minutes),
    )


@router.post(
    "/password-reset/confirm",
    operation_id="confirmPasswordReset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password from a reset link",
    responses={**_FORBIDDEN, **_REFUSED},
    dependencies=[AuthRateLimitDep],
)
async def confirm_password_reset(
    body: Annotated[PasswordResetConfirm, Body()], session: SessionDep, store: SessionStoreDep
) -> None:
    """A token that is unknown, used, replaced or past its hour is a 422
    `invalid_token`. On success every session of the account is signed out:
    whoever had the old password is no longer in. The link reached the
    account's inbox, so an unverified email is verified too."""
    if problem := rules.password_problem(body.new_password):
        raise refused(ErrorCode.WEAK_PASSWORD, "new_password", problem)
    token = await redeem_token(session, body.token, TokenPurpose.PASSWORD_RESET)
    user = None if token is None else await session.get(AppUser, token.user_id)
    if user is None or user.deleted_at is not None:
        raise _invalid_token("ask for a new one")
    await _set_password(session, user.id, body.new_password)
    await session.execute(
        update(AppUser)
        .where(AppUser.id == user.id)
        .values(email_verified_at=func.coalesce(AppUser.email_verified_at, now()))
    )
    await store.end_all(user.id, keep=None)
    await session.commit()


async def _set_password(session: AsyncSession, user_id: int, password: str) -> None:
    """A verification link signs in too, so a new password ends those as well
    as the reset links: neither gets past a password change."""
    await session.execute(
        update(AppUser)
        .where(AppUser.id == user_id)
        .values(password_hash=await hash_password(password), password_changed_at=now())
    )
    await revoke_tokens(session, user_id, TokenPurpose.PASSWORD_RESET)
    await revoke_tokens(session, user_id, TokenPurpose.EMAIL_VERIFY)
