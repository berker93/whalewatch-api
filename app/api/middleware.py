"""ASGI middleware: the request_id and access log, and the guards in front of every route.

In the order a request meets them, outermost first (see :func:`app.main.create_app`):

- :class:`RequestContextMiddleware` gives the request its id and logs it.
- :class:`SecurityHeadersMiddleware` stamps the headers every response carries.
- Starlette's ``CORSMiddleware``, which answers preflights itself.
- :class:`UnhandledErrorMiddleware` turns an exception nothing handled into a 500.
- :class:`BodySizeLimitMiddleware` refuses a body over the limit, 413.
- :class:`~app.api.rate_limit.RateLimitMiddleware`, 429.
- :class:`TimeoutMiddleware` gives up on a slow request, 504.

Every one of them that refuses a request answers in the one error shape, through
:func:`app.api.errors.problem`, and inside the first three, so a refusal still
carries the request id, the security headers and, for a browser, CORS headers it
can read.

Written as raw ASGI rather than as a ``BaseHTTPMiddleware`` subclass. Starlette's
base class runs the rest of the app in a separate anyio task and buffers the
response through a memory stream, which costs a task per request and has a long
history of surprises around background tasks and streaming responses. The three
things this needs — read a header, stamp a response header, time the call — are
all reachable from the plain ``(scope, receive, send)`` signature.
"""

import asyncio
import re
import time
import uuid
from typing import Final

import structlog
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import internal_error, problem
from app.api.schemas.error import ErrorCode
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
    """The 500 for an exception that escaped every middleware this app installs.

    Normally :class:`UnhandledErrorMiddleware` has already answered by the time
    an exception gets here, and Starlette, seeing a response started, only
    re-raises. This is for one raised outside it — in the request context or
    the CORS layer — which reaches Starlette's ``ServerErrorMiddleware``, the
    outermost layer and one no ``add_middleware`` can get outside of. Its own
    500 would be plain text, without the request id the caller most needs.

    Registering a handler for ``Exception`` replaces only *how the response is
    built*: Starlette still re-raises afterwards, so the failure reaches the
    server as it did before.
    """
    headers = {**security_headers(request.app.state.settings.environment)}
    if request_id := getattr(request.state, "request_id", None):
        headers[REQUEST_ID_HEADER] = request_id
    return internal_error(request.scope, headers)


# --- guards ------------------------------------------------------------------------


def _tracking_start(send: Send) -> tuple[Send, list[bool]]:
    """``send``, and a flag set once the response has started: after that, a
    middleware that wanted to answer with an error can no longer."""
    started = [False]

    async def tracked(message: Message) -> None:
        if message["type"] == "http.response.start":
            started[0] = True
        await send(message)

    return tracked, started


def security_headers(environment: str) -> dict[str, str]:
    """The headers every response carries.

    ``nosniff`` because a JSON body that a browser decides is HTML is a script
    injection; ``no-referrer`` because nothing here links anywhere that needs to
    know where the click came from. HSTS only in production: it tells a browser
    to refuse plain HTTP to this host for a year, which on a developer's
    localhost would outlive the reason for it. ``includeSubDomains`` but not
    ``preload``, which is a commitment made with the browser vendors, not here.
    """
    headers = {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }
    if environment == "production":
        headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    return headers


class SecurityHeadersMiddleware:
    """Put :func:`security_headers` on every response, unless a route set its own."""

    def __init__(self, app: ASGIApp, environment: str) -> None:
        self.app = app
        self.headers = security_headers(environment)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class UnhandledErrorMiddleware:
    """Answer an exception nothing handled with a 500 Problem, then re-raise it.

    Inside the request context, security headers and CORS, unlike Starlette's
    own ``ServerErrorMiddleware``, so the 500 gets all three: a frontend on
    another origin can read the body and show the user its ``request_id``.

    Re-raised, so that :class:`RequestContextMiddleware` logs the traceback
    under that id and the server sees the failure. Starlette, finding the
    response already sent, sends nothing more. And never Starlette's debug page:
    it is not reached, whatever ``debug`` says.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracked, started = _tracking_start(send)
        try:
            await self.app(scope, receive, tracked)
        except Exception:
            if not started[0]:
                await internal_error(scope)(scope, receive, send)
            raise


class _BodyTooLarge(HTTPException):
    """Raised from ``receive`` once a streamed body passes the limit.

    An ``HTTPException`` so that, should a route ever read a body, FastAPI's
    handlers render it as the 413 it is rather than as a parse failure.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(413, f"The request body is over the {limit}-byte limit.")


class BodySizeLimitMiddleware:
    """Refuse a request body over ``max_bytes``, 413.

    A declared ``Content-Length`` over the limit is refused before anything
    runs. A body without one (chunked) is counted as it is read, and refused
    at the first chunk past the limit — so nothing past it is ever buffered.
    Starlette has a body limit of its own, but it answers in plain text, and
    only once something reads the body; no route here does.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if (declared := _content_length(scope)) is not None and declared > self.max_bytes:
            await self._refuse(scope, receive, send)
            return

        received = 0

        async def receive_with_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge(self.max_bytes)
            return message

        tracked, started = _tracking_start(send)
        try:
            await self.app(scope, receive_with_limit, tracked)
        except _BodyTooLarge:
            if started[0]:
                raise
            await self._refuse(scope, receive, send)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem(
            413,
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"The request body is over the {self.max_bytes}-byte limit.",
            scope=scope,
            # The rest of the body is unread, so the connection cannot carry
            # another request; saying so stops the client trying.
            headers={"connection": "close"},
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                # The server refuses a malformed Content-Length before we see it.
                return None
    return None


class TimeoutMiddleware:
    """Answer 504 for a request still running after ``seconds``, and cancel it.

    Cancelling is what makes this more than a status code: the handler's
    pending query is cancelled with it, and its session returns the connection
    to the pool on the way out, so a pile-up of slow requests cannot hold every
    connection. A response already started cannot be replaced, so a timeout
    after that is re-raised, and the client sees the connection close.
    """

    def __init__(self, app: ASGIApp, seconds: float) -> None:
        self.app = app
        self.seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tracked, started = _tracking_start(send)
        deadline = asyncio.timeout(self.seconds)
        try:
            async with deadline:
                await self.app(scope, receive, tracked)
        except TimeoutError:
            # A TimeoutError from inside the handler, a socket's, is not ours:
            # it is an unhandled exception like any other.
            if not deadline.expired() or started[0]:
                raise
            logger.warning("request.timed_out", timeout_s=self.seconds)
            response = problem(
                504,
                ErrorCode.TIMEOUT,
                f"The answer took longer than {self.seconds:g} seconds to build. "
                "Try again, or narrow the request.",
                scope=scope,
            )
            await response(scope, receive, send)
