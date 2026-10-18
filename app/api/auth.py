"""Who a request is from: the session cookie, read, set and cleared.

The session is an opaque token in an httpOnly cookie (:mod:`app.accounts.sessions`).
httpOnly, so a script on the page, an injected one included, cannot read it;
``SameSite=Lax``, so another site's form or script cannot send it with a write;
Secure wherever the API is deployed. ``SESSION_COOKIE_DOMAIN=.whalewatch.com``
shares it between the marketing site and the app.

Three dependencies, by how much a route needs a session:

- :data:`ViewerDep`: signed in, or a 401. ``unauthenticated`` when the request
  carried no cookie, ``session_expired`` when it carried one that has ended:
  the frontend tells "log in" from "your session ended" by the code. The 401
  for an ended one also clears the cookie.
- :data:`OptionalViewerDep`: the viewer when there is one, else None. An ended
  session is None here: a route that works signed out does not fail for it.
- :data:`AuthRateLimitDep`: the stricter per-IP limit for anything that checks
  a password or sends an email.

Every route that reads or writes an account answers ``Cache-Control:
no-store`` (:func:`no_store`). The cached public endpoints say ``public``, and a
shared cache that kept one user's account for the next would be the worst bug
this file could have.
"""

from typing import Annotated, Final

from fastapi import Depends, Request, Response, status
from redis.exceptions import RedisError

from app.accounts.sessions import StartedSession, Viewer, find_session, now
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import ApiError, Responses
from app.api.rate_limit import SlidingWindowLimiter
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


async def require_viewer(request: Request, session: SessionDep, settings: SettingsDep) -> Viewer:
    token = session_token(request, settings)
    if token is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Not signed in.")
    viewer = await find_session(session, token)
    if viewer is None:
        raise ApiError(
            401,
            ErrorCode.SESSION_EXPIRED,
            "The session has ended. Log in again.",
            headers={"set-cookie": cleared_cookie_header(settings)},
        )
    return viewer


async def optional_viewer(
    request: Request, session: SessionDep, settings: SettingsDep
) -> Viewer | None:
    token = session_token(request, settings)
    return None if token is None else await find_session(session, token)


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


ViewerDep = Annotated[Viewer, Depends(require_viewer)]
OptionalViewerDep = Annotated[Viewer | None, Depends(optional_viewer)]
AuthRateLimitDep = Depends(auth_rate_limited)
NoStoreDep = Depends(no_store)
