"""Who a console request is from: the admin session cookie, read, set and cleared.

The admin cookie is not the user one. Another name (``ADMIN_SESSION_COOKIE_NAME``),
naming a session in another Redis keyspace (:mod:`app.admin.auth`), so a
user's cookie signs nobody into the console and an admin's signs nobody into
an account. ``Path=/admin``, so a browser sends it to the console's routes and
nowhere else on the API. ``SameSite=Strict``, so no link from another site
arrives signed in. No ``Domain``, so it is never shared with the marketing
site and the app the way the user's is. No ``Max-Age``: it ends with the
browser, and its session after ``ADMIN_SESSION_HOURS`` whatever the browser does.

:data:`AdminDep` is the console's one dependency: an admin with a live session
and access, or a 401 ``unauthenticated`` (no cookie) or ``session_expired``
(an ended session, or an admin disabled since; the cookie is cleared). A
Redis that cannot be reached is a 503 ``unavailable``, as for a user.
"""

from typing import Annotated

from fastapi import Depends, Request, Response

from app.accounts.sessions import SessionStore, StartedSession
from app.admin.auth import Admin, find_admin
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import ApiError
from app.api.schemas.error import ErrorCode
from app.core.config import Settings

#: Where the admin cookie is sent: the console's routes only.
ADMIN_COOKIE_PATH = "/admin"


def get_admin_store(request: Request) -> SessionStore:
    """The admins' sessions, built by the lifespan."""
    store: SessionStore = request.app.state.admin_sessions
    return store


AdminStoreDep = Annotated[SessionStore, Depends(get_admin_store)]


def set_admin_cookie(response: Response, settings: Settings, started: StartedSession) -> None:
    response.set_cookie(
        settings.admin_session_cookie_name,
        started.token,
        path=ADMIN_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_admin_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.admin_session_cookie_name,
        path=ADMIN_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def admin_token(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(settings.admin_session_cookie_name) or None


async def require_admin(
    request: Request, session: SessionDep, store: AdminStoreDep, settings: SettingsDep
) -> Admin:
    token = admin_token(request, settings)
    if token is None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Not signed in to the admin console.")
    admin = await find_admin(session, store, token)
    if admin is None:
        cleared = Response()
        clear_admin_cookie(cleared, settings)
        raise ApiError(
            401,
            ErrorCode.SESSION_EXPIRED,
            "The admin session has ended. Sign in again.",
            headers={"set-cookie": cleared.headers["set-cookie"]},
        )
    return admin


AdminDep = Annotated[Admin, Depends(require_admin)]


def request_id(request: Request) -> str | None:
    """The request's id, for the log row it writes."""
    value: str | None = getattr(request.state, "request_id", None)
    return value
