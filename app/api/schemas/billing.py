"""``/v1/billing``: what the account pays for, a checkout link to start paying,
and the customer portal to change or stop."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.api.schemas.me import MePlan

Status = Literal[
    "trialing",
    "active",
    "past_due",
    "paused",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
]


class BillingSubscription(BaseModel):
    """The account's subscription as Paddle's last word on it left it."""

    plan: MePlan = Field(description="The plan it is for.")
    status: Status = Field(
        description=(
            "`active` or `trialing`: paid for. `past_due`: a renewal's payment failed "
            "and is being retried; the plan holds until `grace_ends_at`. `paused` and "
            "`canceled`: it grants nothing."
        ),
        examples=["active"],
    )
    current_period_end: datetime | None = Field(
        description="When the period paid for ends. Null when nothing is being paid for.",
        examples=["2026-11-27T10:00:00Z"],
    )
    cancel_at: datetime | None = Field(
        description="When a cancellation takes effect: the plan holds until then. Null for none.",
        examples=[None],
    )
    grace_ends_at: datetime | None = Field(
        description=(
            "While a renewal is unpaid: when the account moves to Free unless it is "
            "paid by then. Null otherwise."
        ),
        examples=[None],
    )
    grants_plan: bool = Field(
        description=(
            "Whether it gives the account its plan now: false once cancelled, paused, "
            "or past its grace. `GET /v1/me` says what the account is on."
        ),
        examples=[True],
    )
    manageable: bool = Field(
        description=(
            "Whether `POST /v1/billing/portal` opens it: paid through Paddle, not given by hand."
        ),
        examples=[True],
    )


class Billing(BaseModel):
    """Whether payments work, and the account's subscription."""

    enabled: bool = Field(
        description="Whether checkout and the portal work. False: payments are not set up.",
        examples=[True],
    )
    subscription: BillingSubscription | None = Field(
        description=(
            "The live subscription, else the latest one; null for an account that "
            "never had one. After a checkout this changes once the payment provider's "
            "webhook has arrived, not when the buyer is sent back: ask again until it does."
        ),
    )


class CheckoutRequest(BaseModel):
    """Which plan to buy."""

    plan: Annotated[str, StringConstraints(min_length=1, max_length=64)] = Field(
        description="A public paid plan's id, as `plan_limit` refusals and `GET /v1/me` name them.",
        examples=["pro"],
    )


class CheckoutLink(BaseModel):
    """Where to send the buyer to pay."""

    url: str = Field(
        description=(
            "Paddle's checkout for this account and plan. Open it; the account changes "
            "plan when the payment is confirmed by webhook, not on return from it."
        ),
        examples=["https://whalewatch.io/pay?_ptxn=txn_01hv8wptq8987qeep44cyrewp9"],
    )
    transaction_id: str = Field(
        description="Paddle's transaction, for support.",
        examples=["txn_01hv8wptq8987qeep44cyrewp9"],
    )


class PortalLink(BaseModel):
    """Links into Paddle's customer portal. Each works for a short while: open
    it now, and ask for a new one next time."""

    url: str = Field(
        description="The portal: change plan, update the card, cancel, download invoices.",
        examples=["https://customer-portal.paddle.com/cpl_01j7zbyqs3vah3aafp4jf62qaw?token=..."],
    )
    cancel_url: str | None = Field(
        description="Straight to cancelling. Null with no live subscription.",
        examples=[None],
    )
    update_payment_method_url: str | None = Field(
        description="Straight to replacing the card. Null with no live subscription.",
        examples=[None],
    )
