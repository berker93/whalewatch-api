"""The 402 body: what the caller's plan stopped, and the plan that would not have.

Raised by :mod:`app.api.entitlements`. ``limit`` is an entitlement key, the
same name ``GET /v1/me`` lists it under, so a client can show its own copy
of the limit without parsing ``message``.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.api.schemas.error import Problem, ProblemError


class PlanOffer(BaseModel):
    """A plan that allows what was refused: the cheapest public one."""

    slug: str = Field(description="The plan's id.", examples=["pro"])
    name: str = Field(description="Its name, for a person.", examples=["Pro"])
    price_cents: int = Field(description="Its price, in cents.", examples=[1400])
    currency: str = Field(description="ISO 4217.", examples=["USD"])
    interval: Literal["month", "year"] | None = Field(
        description="How often `price_cents` is billed.", examples=["month"]
    )
    value: int | None = Field(
        description="Its value of `limit`: a whole number, or null for no limit.",
        examples=[20],
    )


class PlanLimitDetail(BaseModel):
    """Which limit, how far the request needed it to go, and who goes further."""

    limit: Literal[
        "history_quarters", "insider_months", "watchlist_items", "screener_saved_filters"
    ] = Field(
        description=(
            "The entitlement that stopped the request: `history_quarters` (a period "
            "before the caller's window), `insider_months` (a trade day before it), "
            "`watchlist_items` or `screener_saved_filters` (that many saved already)."
        ),
        examples=["history_quarters"],
    )
    allowed: int = Field(
        description="The caller's value of `limit`. Never null: no limit is never refused.",
        examples=[4],
    )
    needed: int = Field(
        description=(
            "What the request needed it to be: quarters or months back from today, "
            "counting the current one, or one more than is saved."
        ),
        examples=[9],
    )
    plan: PlanOffer | None = Field(
        description="The cheapest public plan whose value is at least `needed`; null for none.",
    )


class PlanLimitProblemError(ProblemError):
    """The caller's plan does not allow the request."""

    detail: PlanLimitDetail = Field(  # type: ignore[assignment]
        description="The limit, and the plan that lifts it.",
    )


class PlanLimitProblem(Problem):
    """The 402 body: the caller's plan does not allow the request."""

    error: PlanLimitProblemError = Field(
        description="What was refused, with the limit and the plan that lifts it in `detail`.",
    )
