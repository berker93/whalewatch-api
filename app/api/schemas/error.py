"""The one shape every 4xx answers in: ``{code, detail}``, plus a field or two.

Without it there were four: FastAPI's ``{"detail": "..."}``, its validation
``{"detail": [...]}``, the stock 404's ``{"detail": {"message", "suggestions"}}``,
and 422s raised by hand with a string where the schema promised the list. A
generated client types an error as the union of every documented body, so four
shapes meant every caller narrowing ``detail`` before it could print it.

Now ``detail`` is always a sentence for a person, ``code`` is what a program
branches on, and anything more specific is an extra field beside them, never a
change to either. The handlers that render these are in :mod:`app.api.errors`.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """What went wrong, for a program to branch on. ``detail`` says it for a person."""

    INVALID_CURSOR = "invalid_cursor"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    VALIDATION_ERROR = "validation_error"
    #: Any other status. Nothing in this API raises one on purpose; it is here
    #: so that an exception from a dependency still answers in this shape.
    HTTP_ERROR = "http_error"


class Problem(BaseModel):
    """Why the request was refused."""

    code: ErrorCode = Field(
        description=(
            "What went wrong, stable across releases. `invalid_cursor`: `cursor` is "
            "malformed, or belongs to a different listing or sort. `not_found`: no such "
            "thing, or nothing published for the period asked for. `validation_error`: "
            "a parameter is missing or malformed; `errors` says which."
        ),
        examples=["not_found"],
    )
    detail: str = Field(
        description="What went wrong, in a sentence for a person. Not a contract: do not parse it.",
        examples=["No investor 'berkshire'. GET /v1/investors lists every one."],
    )


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


class ValidationProblem(Problem):
    """The 422 body: a parameter is missing or malformed."""

    errors: list[FieldError] = Field(
        description="Every parameter that failed, each once. Never empty.",
    )
