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
    A new account, while ``SIGNUPS_OPEN``. Closed, the request is recorded and
    no session starts.
``POST /v1/auth/password``
    Change the password, signed in. Signs out every other session.
``POST /v1/auth/password-reset`` and ``.../confirm``
    Email a reset link; set a new password from one. The first answers the same
    whether or not the address has an account.

Failures
--------
The frontend branches on ``code``, so the codes are the contract:
``invalid_credentials`` is 403, never 401, which means only that the session is
gone; ``weak_password``, ``invalid_email`` and ``invalid_name`` are 422s naming
the field in ``detail.errors``, as a validation error does. Every route that
checks a password or sends an email is behind the stricter per-IP limit
(:func:`app.api.auth.auth_rate_limited`).
"""

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Body, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import rules
from app.accounts.mail import deliver, mailer_for, password_reset
from app.accounts.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.accounts.sessions import (
    PASSWORD_RESET_TTL,
    end_other_sessions,
    end_session,
    find_session,
    issue_token,
    now,
    redeem_token,
    revoke_tokens,
    start_session,
)
from app.api.auth import (
    UNAUTHORIZED,
    AuthRateLimitDep,
    NoStoreDep,
    ViewerDep,
    clear_session_cookie,
    cleared_cookie_header,
    session_token,
    set_session_cookie,
)
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import INVALID, ApiError, Responses
from app.api.schemas.auth import (
    Authed,
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
from app.db.models import AppUser, SignupRequest, TokenPurpose

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
            "`invalid_token`, or `validation_error` for a malformed body. "
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


def _email_taken() -> ApiError:
    return ApiError(
        409, ErrorCode.EMAIL_TAKEN, "That address already has an account. Log in instead."
    )


def _invalid_credentials() -> ApiError:
    return ApiError(403, ErrorCode.INVALID_CREDENTIALS, "Wrong email or password.")


def _session(user: AppUser, expires_at: datetime) -> Session:
    return Session(user=SessionUser(email=user.email), expires_at=expires_at)


@router.post(
    "/login",
    operation_id="login",
    response_model=Session,
    summary="Sign in with an email and a password",
    responses={**_FORBIDDEN, **INVALID},
    dependencies=[AuthRateLimitDep],
)
async def login(
    body: LoginRequest, response: Response, session: SessionDep, settings: SettingsDep
) -> Session:
    """Sets the session cookie. A wrong email and a wrong password are the same
    403 `invalid_credentials`, and take as long, so neither says which addresses
    have accounts."""
    email = rules.normalise_email(body.email)
    user = await session.scalar(select(AppUser).where(AppUser.email == email))
    matches = await verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not matches:
        raise _invalid_credentials()
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password(body.password)
    started = await start_session(session, user.id, remember=body.remember)
    await session.commit()
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
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> None:
    """Ends the session the cookie names and clears the cookie. A 204 with no
    cookie, or one already ended, too: signing out twice is signed out."""
    if (token := session_token(request, settings)) is not None:
        await end_session(session, token)
        await session.commit()
    clear_session_cookie(response, settings)


@router.get(
    "/session",
    operation_id="getSession",
    response_model=SessionState,
    summary="Who the session cookie is for",
    responses=UNAUTHORIZED,
)
async def read_session(
    request: Request, session: SessionDep, settings: SettingsDep
) -> SessionState:
    """`session: null` when the request carried no cookie. A 401
    `session_expired` when it carried one for a session that has ended, ran out
    or was signed out, and the cookie is cleared."""
    token = session_token(request, settings)
    if token is None:
        return SessionState(session=None)
    viewer = await find_session(session, token)
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
    response_model=RegisterResult,
    summary="Make an account, or ask for one while signups are closed",
    responses={
        **_REFUSED,
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": "`email_taken`: the address already has an account.",
        },
        **_FORBIDDEN,
    },
    dependencies=[AuthRateLimitDep],
)
async def register(
    body: RegisterRequest, response: Response, session: SessionDep, settings: SettingsDep
) -> RegisterResult:
    """While signups are closed, `status: recorded`: the request is kept and no
    session starts. Open, `status: authed`, with the session cookie set. Either
    way the fields are checked first: `invalid_name`, `invalid_email` and
    `weak_password`, in that order."""
    name = rules.normalise_name(body.name)
    email = rules.normalise_email(body.email)
    if problem := rules.name_problem(name):
        raise refused(ErrorCode.INVALID_NAME, "name", problem)
    if problem := rules.email_problem(email):
        raise refused(ErrorCode.INVALID_EMAIL, "email", problem)
    if problem := rules.password_problem(body.password):
        raise refused(ErrorCode.WEAK_PASSWORD, "password", problem)
    time_zone = body.time_zone or rules.DEFAULT_TIME_ZONE
    if problem := await rules.time_zone_problem(session, time_zone):
        raise refused(ErrorCode.VALIDATION_ERROR, "time_zone", problem)

    taken = await session.scalar(select(AppUser.id).where(AppUser.email == email))
    if not settings.signups_open:
        await session.execute(
            insert(SignupRequest)
            .values(email=email, name=name)
            .on_conflict_do_update(index_elements=["email"], set_={"name": name})
        )
        await session.commit()
        return RegisterResult(Recorded(status="recorded"))

    if taken is not None:
        raise _email_taken()
    user = AppUser(
        email=email,
        name=name,
        password_hash=await hash_password(body.password),
        time_zone=time_zone,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Registered by a request that raced this one.
        await session.rollback()
        raise _email_taken() from None
    started = await start_session(session, user.id, remember=True)
    await session.commit()
    set_session_cookie(response, settings, started)
    return RegisterResult(Authed(status="authed", session=_session(user, started.expires_at)))


@router.post(
    "/password",
    operation_id="changePassword",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the password, and sign out every other session",
    responses={**UNAUTHORIZED, **_FORBIDDEN, **_REFUSED},
    dependencies=[AuthRateLimitDep],
)
async def change_password(
    body: PasswordChangeRequest, viewer: ViewerDep, session: SessionDep
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
    await end_other_sessions(session, user.id, keep=viewer.session_id)
    await session.commit()


@router.post(
    "/password-reset",
    operation_id="requestPasswordReset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Email a password reset link",
    responses={**_FORBIDDEN, **INVALID},
    dependencies=[AuthRateLimitDep],
)
async def request_password_reset(
    body: PasswordResetRequest, session: SessionDep, settings: SettingsDep
) -> None:
    """A 204 whether or not the address has an account, so the answer reveals
    none. When it has one, a link valid for an hour goes to it, replacing any
    sent before."""
    email = rules.normalise_email(body.email)
    user = await session.scalar(select(AppUser).where(AppUser.email == email))
    if user is None:
        return
    token = await issue_token(session, user.id, TokenPurpose.PASSWORD_RESET, PASSWORD_RESET_TTL)
    await session.commit()
    minutes = int(PASSWORD_RESET_TTL.total_seconds() // 60)
    await deliver(
        mailer_for(settings), password_reset(settings, to=user.email, token=token, minutes=minutes)
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
    body: Annotated[PasswordResetConfirm, Body()], session: SessionDep
) -> None:
    """A token that is unknown, used, replaced or past its hour is a 422
    `invalid_token`. On success every session of the account is signed out:
    whoever had the old password is no longer in."""
    if problem := rules.password_problem(body.new_password):
        raise refused(ErrorCode.WEAK_PASSWORD, "new_password", problem)
    token = await redeem_token(session, body.token, TokenPurpose.PASSWORD_RESET)
    if token is None:
        raise refused(
            ErrorCode.INVALID_TOKEN, "token", "the link is not valid any more: ask for a new one"
        )
    await _set_password(session, token.user_id, body.new_password)
    await end_other_sessions(session, token.user_id, keep=None)
    await session.commit()


async def _set_password(session: AsyncSession, user_id: int, password: str) -> None:
    await session.execute(
        update(AppUser)
        .where(AppUser.id == user_id)
        .values(password_hash=await hash_password(password), password_changed_at=now())
    )
    await revoke_tokens(session, user_id, TokenPurpose.PASSWORD_RESET)
