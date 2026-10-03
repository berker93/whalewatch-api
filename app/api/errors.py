"""Every error rendered as a :class:`~app.api.schemas.error.Problem`, and documented as one.

Two halves that have to agree. The handlers decide what an error *is* on the
wire; the ``responses=`` fragments below are what each route tells the OpenAPI
schema it may answer. ``tests/test_openapi.py`` holds them to each other: a
route that can 404 and documents no body fails there, not in a frontend.

Errors raised outside a route — the 429, 413 and 504 the middleware in
:mod:`app.api.middleware` and :mod:`app.api.rate_limit` answer, and the 500 for
an exception nothing handled — are built by :func:`problem` too, so there is
one place that decides what an error body looks like.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.types import Scope

from app.api.schemas.error import (
    ErrorCode,
    FieldError,
    Problem,
    ValidationDetail,
    ValidationProblem,
)

#: A route's ``responses=``: status to OpenAPI response object, plus ``model``.
Responses = dict[int | str, dict[str, Any]]

#: The code a plain ``HTTPException`` gets, by status. Starlette raises the 404
#: for an unknown path and the 405 itself, so these cannot all be ApiErrors.
_CODES: Final[dict[int, ErrorCode]] = {
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_413_CONTENT_TOO_LARGE: ErrorCode.PAYLOAD_TOO_LARGE,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.VALIDATION_ERROR,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
}

#: The whole of what a 500 says. Deliberately nothing about the exception: its
#: type and message can carry SQL, file paths or a row's values, and they are in
#: the log line that ``request_id`` finds.
INTERNAL_ERROR_MESSAGE: Final = (
    "Something went wrong on our side. If it keeps happening, report it quoting request_id."
)


class ApiError(HTTPException):
    """An ``HTTPException`` with a code and, optionally, a ``detail`` object.

    ``HTTPException.detail`` is the message, so a log line or a test reading
    ``exc.detail`` sees the same sentence the client does. The object the body
    carries as ``detail`` is :attr:`extra`.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        *,
        detail: BaseModel | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.extra = detail


def request_id_of(scope: Scope) -> str | None:
    """The id :class:`~app.api.middleware.RequestContextMiddleware` gave this request."""
    request_id: str | None = scope.get("state", {}).get("request_id")
    return request_id


def problem(
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    scope: Scope,
    detail: BaseModel | Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The error response, as an ASGI app: a handler returns it, a middleware calls it."""
    if isinstance(detail, BaseModel):
        detail = detail.model_dump(mode="json")
    body = {
        "error": {
            "code": code.value,
            "message": message,
            "detail": dict(detail or {}),
            "request_id": request_id_of(scope),
        }
    }
    return JSONResponse(body, status_code=status_code, headers=headers)


def invalid(name: str, msg: str, *, where: str = "query") -> RequestValidationError:
    """A 422 for a parameter that parsed but cannot be answered, shaped like Pydantic's.

    Raised rather than an ``HTTPException(422)`` so that a client handles a
    refused combination exactly as it handles a malformed value: one
    ``detail.errors`` list, keyed by parameter.
    """
    return RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg}])


def _where(loc: Sequence[str | int]) -> str:
    # ("query", "period") -> "period": every parameter here is in the query or
    # the path, and the client named it without saying which.
    return ".".join(str(part) for part in (loc[1:] or loc))


async def http_error(request: Request, exc: Exception) -> JSONResponse:
    """Render an ``HTTPException``, ours or Starlette's, as a Problem."""
    assert isinstance(exc, HTTPException)  # registered for nothing else
    if isinstance(exc, ApiError):
        code, extra = exc.code, exc.extra
    else:
        code, extra = _CODES.get(exc.status_code, ErrorCode.HTTP_ERROR), None
    if code is ErrorCode.VALIDATION_ERROR and extra is None:
        # Keeps the 422's documented promise that detail.errors is always there.
        extra = ValidationDetail(errors=[])
    return problem(
        exc.status_code,
        code,
        str(exc.detail),
        scope=request.scope,
        detail=extra,
        headers=exc.headers,
    )


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Render Pydantic's refusal of a parameter as a ValidationProblem.

    ``input`` and ``ctx`` are dropped from each error: the first only echoes
    the request back, and the second can hold an exception object that has no
    JSON form.
    """
    assert isinstance(exc, RequestValidationError)
    errors = [FieldError(loc=list(e["loc"]), msg=e["msg"], type=e["type"]) for e in exc.errors()]
    message = "; ".join(f"{_where(e.loc)}: {e.msg}" for e in errors)
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        message,
        scope=request.scope,
        detail=ValidationDetail(errors=errors),
    )


def internal_error(scope: Scope, headers: Mapping[str, str] | None = None) -> JSONResponse:
    """The 500 for an exception nothing handled: a code, a sentence, the request id."""
    return problem(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        INTERNAL_ERROR_MESSAGE,
        scope=scope,
        headers=headers,
    )


# --- what routes document -------------------------------------------------------


def not_found(description: str, model: Any = Problem) -> Responses:
    """A 404, described for the route. ``model`` for a body more specific than a Problem."""
    return {status.HTTP_404_NOT_FOUND: {"model": model, "description": description}}


#: Every route with a parameter. Declared, rather than left to FastAPI's
#: default, because the default documents FastAPI's body and not ours.
INVALID: Final[Responses] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ValidationProblem,
        "description": "A parameter is missing or malformed. `detail.errors` says which.",
    }
}

#: Every route the rate limit applies to: all but the probes.
RATE_LIMITED: Final[Responses] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": Problem,
        "description": "Too many requests from this address. Wait `Retry-After` seconds.",
        "headers": {
            "Retry-After": {
                "description": "Seconds until a request from this address will be answered.",
                "schema": {"type": "integer"},
            }
        },
    }
}

#: Every paginated route: the cursor is checked before the handler runs.
INVALID_CURSOR: Final[Responses] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": Problem,
        "description": "`cursor` is malformed, or came from a different listing or sort order.",
    }
}
