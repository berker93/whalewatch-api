"""Every 4xx rendered as a :class:`~app.api.schemas.error.Problem`, and documented as one.

Two halves that have to agree. The handlers decide what an error *is* on the
wire; the ``responses=`` fragments below are what each route tells the OpenAPI
schema it may answer. ``tests/test_openapi.py`` holds them to each other: a
route that can 404 and documents no body fails there, not in a frontend.

The 500 is left alone. It is Starlette's plain text, by
:func:`~app.api.middleware.request_id_on_server_error`'s design, and is in no
route's contract: a client can only report it, quoting ``X-Request-ID``.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from app.api.schemas.error import ErrorCode, FieldError, Problem, ValidationProblem

#: A route's ``responses=``: status to OpenAPI response object, plus ``model``.
Responses = dict[int | str, dict[str, Any]]

#: The code a plain ``HTTPException`` gets, by status. Starlette raises the 404
#: for an unknown path and the 405 itself, so these cannot all be ApiErrors.
_CODES: Final[dict[int, ErrorCode]] = {
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.VALIDATION_ERROR,
}


class ApiError(HTTPException):
    """An ``HTTPException`` carrying its whole body, for one more specific than a sentence.

    ``detail`` is set from the body too, so a log line or a test reading
    ``exc.detail`` sees the same sentence the client does.
    """

    def __init__(
        self, status_code: int, body: Problem, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=body.detail, headers=headers)
        self.body = body


def invalid(name: str, msg: str, *, where: str = "query") -> RequestValidationError:
    """A 422 for a parameter that parsed but cannot be answered, shaped like Pydantic's.

    Raised rather than an ``HTTPException(422)`` so that a client handles a
    refused combination exactly as it handles a malformed value: one ``errors``
    list, keyed by parameter.
    """
    return RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg}])


def _where(loc: Sequence[str | int]) -> str:
    # ("query", "period") -> "period": every parameter here is in the query or
    # the path, and the client named it without saying which.
    return ".".join(str(part) for part in (loc[1:] or loc))


def _json(status_code: int, body: Problem, headers: Mapping[str, str] | None = None) -> Response:
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


async def http_error(request: Request, exc: Exception) -> Response:
    """Render an ``HTTPException``, ours or Starlette's, as a Problem."""
    assert isinstance(exc, HTTPException)  # registered for nothing else
    if isinstance(exc, ApiError):
        return _json(exc.status_code, exc.body, exc.headers)
    code = _CODES.get(exc.status_code, ErrorCode.HTTP_ERROR)
    if code is ErrorCode.VALIDATION_ERROR:
        body: Problem = ValidationProblem(code=code, detail=str(exc.detail), errors=[])
    else:
        body = Problem(code=code, detail=str(exc.detail))
    return _json(exc.status_code, body, exc.headers)


async def validation_error(request: Request, exc: Exception) -> Response:
    """Render Pydantic's refusal of a parameter as a ValidationProblem.

    ``input`` and ``ctx`` are dropped from each error: the first only echoes
    the request back, and the second can hold an exception object that has no
    JSON form.
    """
    assert isinstance(exc, RequestValidationError)
    errors = [FieldError(loc=list(e["loc"]), msg=e["msg"], type=e["type"]) for e in exc.errors()]
    detail = "; ".join(f"{_where(e.loc)}: {e.msg}" for e in errors)
    body = ValidationProblem(code=ErrorCode.VALIDATION_ERROR, detail=detail, errors=errors)
    return _json(status.HTTP_422_UNPROCESSABLE_CONTENT, body)


# --- what routes document -------------------------------------------------------


def not_found(description: str, model: Any = Problem) -> Responses:
    """A 404, described for the route. ``model`` for a body more specific than a Problem."""
    return {status.HTTP_404_NOT_FOUND: {"model": model, "description": description}}


#: Every route with a parameter. Declared, rather than left to FastAPI's
#: default, because the default documents FastAPI's body and not ours.
INVALID: Final[Responses] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ValidationProblem,
        "description": "A parameter is missing or malformed. `errors` says which.",
    }
}

#: Every paginated route: the cursor is checked before the handler runs.
INVALID_CURSOR: Final[Responses] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": Problem,
        "description": "`cursor` is malformed, or came from a different listing or sort order.",
    }
}
