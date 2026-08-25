"""ASGI middleware binding a request_id to every log line, and one access log.

Written as raw ASGI rather than as a ``BaseHTTPMiddleware`` subclass. Starlette's
base class runs the rest of the app in a separate anyio task and buffers the
response through a memory stream, which costs a task per request and has a long
history of surprises around background tasks and streaming responses. The three
things this needs — read a header, stamp a response header, time the call — are
all reachable from the plain ``(scope, receive, send)`` signature.
"""

import re
import time
import uuid
from typing import Final

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Both read and echoed. The ``X-`` prefix is deprecated by RFC 6648 but this
#: spelling is what load balancers, Envoy and every other service in a typical
#: mesh already emit, and interoperating beats being right about the prefix.
REQUEST_ID_HEADER: Final = "x-request-id"

#: An inbound request id is attacker-controlled and ends up in every log line for
#: that request, so it is not taken on trust. Restricting it to this alphabet
#: keeps quote marks, newlines and control characters out of the log stream, and
#: the length cap stops a client from paying us to store 10 KB per line. Anything
#: that does not match is replaced with a fresh id rather than rejected: the
#: caller gets a working request and we get a usable identifier.
_SAFE_REQUEST_ID: Final = re.compile(r"\A[A-Za-z0-9._:+/=-]{1,128}\Z")


def _inbound_request_id(scope: Scope) -> str | None:
    """Return the caller-supplied request id, if it sent a usable one."""
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name != REQUEST_ID_HEADER.encode():
            continue
        # ASGI header values are bytes and are not guaranteed to be UTF-8.
        candidate = raw_value.decode("latin-1").strip()
        return candidate if _SAFE_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    """Give each request an id, bind it to the log context, and log the result.

    The id is taken from the inbound ``X-Request-ID`` when there is one, so a
    trace that starts at the edge proxy or in a calling service keeps a single
    identifier all the way through instead of getting a new one at every hop.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket scopes have no request to identify, and
            # MutableHeaders below would not apply to them anyway.
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid.uuid4().hex

        # Clear before binding. Each request is normally handled in its own task
        # with its own context copy, but this costs nothing and makes the
        # guarantee independent of how the server happens to schedule work.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        # Also on the scope, so exception handlers and route handlers can reach
        # it through ``request.state.request_id`` without importing structlog.
        scope.setdefault("state", {})["request_id"] = request_id

        # Only overwritten if the app actually starts a response. If it raises
        # before doing so, this is the status the client will end up seeing from
        # Starlette's error middleware, so it is the honest thing to log.
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        # perf_counter, not time(): it is monotonic, so an NTP correction during
        # a slow request cannot produce a negative duration.
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "request_failed",
                **_access_fields(scope, status_code, started),
            )
            # Re-raised, not swallowed: turning the exception into a response is
            # Starlette's ServerErrorMiddleware's job, and it sits outside this
            # middleware precisely so that one place decides what a 500 looks
            # like. We only make sure the failure is on the record first.
            raise
        else:
            logger.info(
                "request_completed",
                **_access_fields(scope, status_code, started),
            )
        finally:
            structlog.contextvars.clear_contextvars()


def _access_fields(scope: Scope, status_code: int, started: float) -> dict[str, object]:
    """The one access line's payload.

    ``request_id`` is not in here: it is bound to the context, so every line
    emitted during this request carries it, and duplicating it would only create
    a second place for it to disagree with itself.
    """
    fields: dict[str, object] = {
        "method": scope["method"],
        "path": scope["path"],
        "status": status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    # Only when there is one, so the common case stays a narrow line. The raw
    # query string rather than parsed params — this is what the client sent, and
    # reproducing a bug means replaying exactly that.
    if query := scope.get("query_string", b""):
        fields["query"] = query.decode("latin-1")
    if client := scope.get("client"):
        fields["client_ip"] = client[0]
    return fields


async def request_id_on_server_error(request: Request, exc: Exception) -> Response:
    """Put the request id on the 500 that an unhandled exception produces.

    Starlette turns unhandled exceptions into a response in ``ServerErrorMiddleware``,
    which is hardcoded as the outermost layer of the stack — outside anything
    ``add_middleware`` can install. So that response never passes through the
    ``send`` wrapper above, and without this handler it would be the one response
    on the whole service that lacks the header, in the exact case where the
    caller most needs an id to quote in a bug report.

    Registering a handler for ``Exception`` replaces only *how the response is
    built*: Starlette still re-raises afterwards, so the failure reaches the
    server as it did before. The body and status are byte-identical to
    Starlette's own default; the header is the only addition.

    The id is read from the scope rather than the log context because the
    middleware clears its context bindings as the exception passes back through.
    """
    request_id = getattr(request.state, "request_id", None)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)
