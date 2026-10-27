"""Models for API keys and monthly quotas: ``/v1/api-keys``, ``usage`` on
``GET /v1/me``, and the 429 a spent quota answers."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.api.schemas.error import Problem, ProblemError
from app.api.schemas.plan_limit import PlanOffer
from app.db.models.usage import MAX_KEY_NAME

Metered = Literal["api_calls_month", "mcp_calls_month"]


# --- usage ---------------------------------------------------------------------------


class QuotaUsage(BaseModel):
    """One quota's month: spent, allowed, and when it starts again."""

    used: int = Field(description="Calls counted this quota month.", examples=[8125])
    limit: int | None = Field(
        description="The plan's quota a month: 0 for none, null for no limit.", examples=[10000]
    )
    remaining: int | None = Field(
        description="`limit` less `used`, never below 0. Null for no limit.", examples=[1875]
    )
    period_start: date = Field(
        description=(
            "The quota month's first day: the subscription's anniversary, or the "
            "account's on Free. Months start at midnight UTC."
        ),
        examples=["2026-10-14"],
    )
    resets_at: datetime = Field(
        description="When the next month starts and `used` goes back to 0.",
        examples=["2026-11-14T00:00:00Z"],
    )


class Usage(BaseModel):
    """What the account has spent of each monthly quota.

    A count is at most a few minutes behind for an account whose calls are
    being counted on several servers at once, and exact otherwise."""

    api_calls_month: QuotaUsage = Field(description="Requests made with the account's API keys.")
    mcp_calls_month: QuotaUsage = Field(description="Calls made through the MCP server.")


# --- the 429 -------------------------------------------------------------------------


class QuotaDetail(BaseModel):
    """Which quota, where it stands, when it resets, and who has more."""

    limit: Metered = Field(
        description="The quota spent, by its entitlement's name.", examples=["api_calls_month"]
    )
    allowed: int = Field(description="The month's quota.", examples=[10000])
    used: int = Field(description="Calls counted this month: all of it.", examples=[10000])
    resets_at: datetime = Field(
        description="When the next month starts, as `X-RateLimit-Reset` says in seconds.",
        examples=["2026-11-14T00:00:00Z"],
    )
    plan: PlanOffer | None = Field(
        description="The cheapest public plan with a larger quota; null for none.",
    )


class QuotaProblemError(ProblemError):
    """The month's quota is spent."""

    detail: QuotaDetail = Field(  # type: ignore[assignment]
        description="The quota, and when it resets.",
    )


class QuotaProblem(Problem):
    """The 429 body for a spent quota: `quota_exceeded`."""

    error: QuotaProblemError = Field(
        description="What was refused, with the quota and its reset in `detail`.",
    )


# --- API keys ------------------------------------------------------------------------

KeyName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_KEY_NAME)
]


class NewApiKey(BaseModel):
    """A key to make."""

    name: KeyName = Field(
        description=f"What to call it, 1 to {MAX_KEY_NAME} characters, trimmed.",
        examples=["nightly job"],
    )


class ApiKey(BaseModel):
    """One API key that works. The key itself is shown once, when it is made."""

    id: str = Field(description="What `DELETE /v1/api-keys/{id}` takes.", examples=["7"])
    name: str = Field(description="What the user called it.", examples=["nightly job"])
    prefix: str = Field(
        description="Its first characters, to tell it from the others.", examples=["ww_Ab12Cd34"]
    )
    created_at: datetime = Field(description="When it was made.", examples=["2026-10-25T09:30:00Z"])
    last_used_at: datetime | None = Field(
        description="The last request made with it, to within five minutes. Null: never used.",
        examples=["2026-10-25T10:02:00Z"],
    )


class CreatedApiKey(ApiKey):
    """A key just made, with the key: copy it now, it is never shown again."""

    key: str = Field(
        description="Send it as `Authorization: Bearer <key>`. Shown in this answer only.",
        examples=["ww_Ab12Cd34ExampleExampleExampleExampleExam"],
    )


class ApiKeys(BaseModel):
    """The account's keys that work, oldest first."""

    data: list[ApiKey] = Field(description="Every key that works. Empty: none.")
