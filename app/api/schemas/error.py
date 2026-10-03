"""The one shape every error answers in: ``{"error": {code, message, detail, request_id}}``.

Without it there were four: FastAPI's ``{"detail": "..."}``, its validation
``{"detail": [...]}``, the stock 404's ``{"detail": {"message", "suggestions"}}``,
and 422s raised by hand with a string where the schema promised the list. A
generated client types an error as the union of every documented body, so four
shapes meant every caller narrowing ``detail`` before it could print it.

Now ``code`` is what a program branches on, ``message`` is a sentence for a
person, ``detail`` is an object holding anything more specific (the failing
parameters, the stocks a caller may have meant), and ``request_id`` is what a
person quotes when reporting it. A more specific error narrows ``detail``; it
never changes the other three. Wrapped in ``error`` so that no error body can be
mistaken for a successful one, whose top level is ``data``.

The 500 is in this shape too, with nothing in it but a code, a fixed sentence
and the request id: never the exception or its traceback.

The handlers that render these are in :mod:`app.api.errors`.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """What went wrong, for a program to branch on. ``message`` says it for a person."""

    INVALID_CURSOR = "invalid_cursor"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    TIMEOUT = "timeout"
    #: Any other status. Nothing in this API raises one on purpose; it is here
    #: so that an exception from a dependency still answers in this shape.
    HTTP_ERROR = "http_error"


class ProblemError(BaseModel):
    """What went wrong."""

    code: ErrorCode = Field(
        description=(
            "What went wrong, stable across releases. `invalid_cursor`: `cursor` is "
            "malformed, or belongs to a different listing or sort. `not_found`: no such "
            "thing, or nothing published for the period asked for. `validation_error`: "
            "a parameter is missing or malformed; `detail.errors` says which. "
            "`rate_limited`: too many requests from this address; wait `Retry-After` "
            "seconds. `payload_too_large`: the request body is over the limit. "
            "`timeout`: the answer took too long to build. `internal_error`: a fault "
            "on our side; quote `request_id` when reporting it."
        ),
        examples=["not_found"],
    )
    message: str = Field(
        description="What went wrong, in a sentence for a person. Not a contract: do not parse it.",
        examples=["No investor 'berkshire'. GET /v1/investors lists every one."],
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Anything more specific than `code`, by field. Empty unless the error's "
            "own schema says what it holds."
        ),
        examples=[{}],
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "This request's id, also sent as the `X-Request-ID` header. Quote it when "
            "reporting a problem: it finds the request in the server's logs."
        ),
        examples=["3f2c9a7e5b1d4c0e8a6f2d9b7c5e1a30"],
    )


class Problem(BaseModel):
    """Why the request was refused, or failed."""

    error: ProblemError = Field(description="What went wrong, and in which request.")


class FieldError(BaseModel):
    """One parameter that failed validation, and why."""

    loc: list[str | int] = Field(
        description='Where the parameter is, then its name: `["query", "period"]`.',
        examples=[["query", "period"]],
    )
    msg: str = Field(
        description="Why it was refused, for a person.",
        examples=["Value error, '2026Q5' has no quarter 5: quarters are 1 to 4"],
    )
    type: str = Field(
        description="Pydantic's error type, such as `missing` or `value_error`.",
        examples=["value_error"],
    )


class ValidationDetail(BaseModel):
    """The parameters that failed."""

    errors: list[FieldError] = Field(
        description="Every parameter that failed, each once. Never empty.",
    )


class ValidationProblemError(ProblemError):
    """A parameter is missing or malformed."""

    detail: ValidationDetail = Field(  # type: ignore[assignment]
        description="Which parameters failed, and why.",
    )


class ValidationProblem(Problem):
    """The 422 body: a parameter is missing or malformed."""

    error: ValidationProblemError = Field(
        description="What went wrong, with the failing parameters in `detail.errors`.",
    )
