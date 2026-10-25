"""Who a request is from: the session cookie, read, set and cleared.

The session is an opaque token in an httpOnly cookie, naming a session kept in
Redis (:mod:`app.accounts.sessions`).
httpOnly, so a script on the page, an injected one included, cannot read it;
``SameSite=Lax``, so another site's form or script cannot send it with a write;
Secure wherever the API is deployed. ``SESSION_COOKIE_DOMAIN=.whalewatch.com``
shares it between the marketing site and the app.

The dependencies, by how much a route needs a session:

- :data:`ViewerDep`: signed in, or a 401. ``unauthenticated`` when the request
  carried no cookie, ``session_expired`` when it carried one that has ended:
  the frontend tells "log in" from "your session ended" by the code. The 401
  for an ended one also clears the cookie.
- :data:`VerifiedViewerDep`: signed in with a verified email, or a 403
  ``email_unverified``. For every route that saves something: an unverified
  account reads everything and saves nothing.
- :data:`OptionalViewerDep`: the viewer when there is one, else None. An ended
  session is None here, and so is a Redis that cannot be reached: a route that
  works signed out does not fail for either.
- :data:`AuthRateLimitDep`: the stricter per-IP limit for the credential calls
  that :func:`limit_attempts` does not cover.

A Redis that cannot be reached is a 503 ``unavailable`` for a route that needs
a session (:func:`app.api.errors.sessions_unavailable`): never a guess at who is
asking. Every signed-in request marks the account seen
(:func:`app.accounts.sessions.touch`), at most every five minutes.

Logins, registrations and password reset requests go through
:func:`limit_attempts`: a few per quarter hour per client IP and, separately,
per email address, so neither one address trying many accounts nor many
addresses trying one gets far.

Every route that reads or writes an account answers ``Cache-Control:
no-store`` (:func:`no_store`). The cached public endpoints say ``public``, and a
shared cache that kept one user's account for the next would be the worst bug
this file could have.
"""

import hashlib
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from fastapi import Depends, Request, Response, status
from redis.exceptions import RedisError

from app.accounts.sessions import (
    SessionStore,
    SessionStoreError,
    StartedSession,
    Viewer,
    find_session,
    now,
    touch,
)
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import ApiError, Responses
from app.api.rate_limit import SlidingWindowLimiter, bucket
from app.api.schemas.error import ErrorCode, Problem
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: What the 401s document.
UNAUTHORIZED: Final[Responses] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": Problem,
        "description": (
            "Not signed in. `unauthenticated` when the request had no session cookie, "
            "`session_expired` when the session it named has ended."
        ),
    }
}


#: What the 403 for an unverified account documents.
UNVERIFIED: Final[Responses] = {
    status.HTTP_403_FORBIDDEN: {
        "model": Problem,
        "description": (
            "`email_unverified`: the account's email is not verified yet, so it saves "
            "nothing; `forbidden_origin`: a write from a site not allowed to call."
        ),
    }
}


def get_session_store(request: Request) -> SessionStore:
    """The sessions, in the app's Redis: built by the lifespan."""
    store: SessionStore = request.app.state.sessions
    return store


SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]


def set_session_cookie(response: Response, settings: Settings, started: StartedSession) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        started.token,
        # A remembered session's cookie outlives the browser; another ends with it.
        max_age=int((started.expires_at - now()).total_seconds()) if started.remember else None,
        domain=settings.session_cookie_domain,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        domain=settings.session_cookie_domain,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def cleared_cookie_header(settings: Settings) -> str:
    """The ``Set-Cookie`` that clears the session, for an error response to carry."""
    response = Response()
    clear_session_cookie(response, settings)
    return response.headers["set-cookie"]


def session_token(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(settings.session_cookie_name) or None


@dataclass(frozen=True, slots=True)
class SessionLookup:
    """What the request's cookie named: one lookup, whichever dependencies ask.

    FastAPI runs a dependency once per request however many others need it, so
    a route that takes both a viewer and the entitlements
    (:mod:`app.api.entitlements`) reads the session once, and marks the account
    seen once.
    """

    token: str | None
    viewer: Viewer | None
    unavailable: SessionStoreError | None = None
    """Redis could not be reached: who is asking is unknown."""


async def lookup_session(
    request: Request, session: SessionDep, store: SessionStoreDep, settings: SettingsDep
) -> SessionLookup:
    token = session_token(request, settings)
    if token is None:
        return SessionLookup(token=None, viewer=None)
    try:
        viewer = await find_session(session, store, token)
    except SessionStoreError as exc:
        return SessionLookup(token=token, viewer=None, unavailable=exc)
    if viewer is not None:
        await touch(session, viewer.user)
    return SessionLookup(token=token, viewer=viewer)


SessionLookupDep = Annotated[SessionLookup, Depends(lookup_session)]


async def require_viewer(lookup: SessionLookupDep, settings: SettingsDep) -> Viewer:
    if lookup.unavailable is not None:
        raise lookup.unavailable
    if lookup.token is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Not signed in.")
    if lookup.viewer is None:
        raise ApiError(
            401,
            ErrorCode.SESSION_EXPIRED,
            "The session has ended. Log in again.",
            headers={"set-cookie": cleared_cookie_header(settings)},
        )
    return lookup.viewer


async def require_verified(viewer: Annotated[Viewer, Depends(require_viewer)]) -> Viewer:
    if viewer.user.email_verified_at is None:
        raise ApiError(
            403,
            ErrorCode.EMAIL_UNVERIFIED,
            "Verify your email to save anything: follow the link we sent, or ask for "
            "another from your account page.",
        )
    return viewer


async def optional_viewer(lookup: SessionLookupDep) -> Viewer | None:
    if lookup.unavailable is not None:
        logger.warning("sessions.unavailable", error=repr(lookup.unavailable.__cause__))
    return lookup.viewer


def no_store(response: Response) -> None:
    """Keep this answer out of every cache: it is one person's."""
    response.headers["cache-control"] = "no-store"


async def auth_rate_limited(request: Request) -> None:
    """Count one credential request from this address, or refuse it, 429.

    The limiter is the one the lifespan builds (``auth_rate_limiter``). An app
    without one, as in a test that does not run the lifespan, limits nothing,
    and a Redis that cannot be reached lets the request through, as the global
    limit does.
    """
    limiter: SlidingWindowLimiter | None = getattr(request.app.state, "auth_rate_limiter", None)
    client = request.client
    if limiter is None or client is None:
        return
    try:
        decision = await limiter.hit(client.host)
    except (RedisError, OSError) as exc:
        logger.warning("auth_rate_limit.unavailable", error=repr(exc))
        return
    if not decision.admitted:
        raise ApiError(
            429,
            ErrorCode.RATE_LIMITED,
            f"Over {limiter.limit} sign-in attempts a minute from this address. "
            f"Try again in {decision.retry_after} seconds.",
            headers={"retry-after": str(decision.retry_after)},
        )


Attempt = Literal["login", "register", "password_reset", "verify_email"]


def email_key(email: str) -> str:
    """What an address is counted as: its SHA-256, lowercased first as the
    citext column compares it, so the limit sees ``Ada@`` and ``ada@`` as one
    and Redis never holds an address in the clear."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


async def limit_attempts(request: Request, action: Attempt, email: str) -> None:
    """Count one ``action`` from this client IP and for ``email``, or refuse
    it, 429. Called by the route once it has read the email from the body.

    Each action has its own budget, so a user who mistypes a password a few
    times can still ask for a reset. The address is counted whether or not it
    has an account, so a refusal says nothing about which ones do. A refused
    attempt is not counted, so retrying through a 429 does not extend it.

    The limiter is the one the lifespan builds (``attempt_limiter``). An app
    without one limits nothing, and a Redis that cannot be reached lets the
    attempt through, as the global limit does.
    """
    limiter: SlidingWindowLimiter | None = getattr(request.app.state, "attempt_limiter", None)
    client = request.client
    if limiter is None:
        return
    keys = [f"{action}:email:{email_key(email)}"]
    if client is not None:
        keys.insert(0, f"{action}:ip:{bucket(client.host)}")
    for key in keys:
        try:
            decision = await limiter.hit(key)
        except (RedisError, OSError) as exc:
            logger.warning("attempt_limit.unavailable", error=repr(exc))
            return
        if not decision.admitted:
            minutes = limiter.window_ms // 60_000
            raise ApiError(
                429,
                ErrorCode.RATE_LIMITED,
                f"Too many attempts: at most {limiter.limit} in {minutes} minutes. "
                f"Try again in {decision.retry_after} seconds.",
                headers={"retry-after": str(decision.retry_after)},
            )


ViewerDep = Annotated[Viewer, Depends(require_viewer)]
VerifiedViewerDep = Annotated[Viewer, Depends(require_verified)]
OptionalViewerDep = Annotated[Viewer | None, Depends(optional_viewer)]
AuthRateLimitDep = Depends(auth_rate_limited)
NoStoreDep = Depends(no_store)
