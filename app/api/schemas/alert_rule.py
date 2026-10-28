"""Models for ``/v1/me/alert-rules``: what the user asked to be told about, and how.

A rule names its target as the watchlist does (:class:`~app.api.schemas.watchlist.WatchRef`):
an investor by slug, a stock by CUSIP. A kind fixes which it takes: the
investor kinds an investor, the stock kinds a stock, the season digest none.
No kind is about an insider yet.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.api.schemas.notification import Notification
from app.api.schemas.watchlist import WatchRef
from app.db.models import AlertKind, Channel, EmailStatus

#: The largest threshold: the column's twenty digits, two after the point.
MAX_THRESHOLD: Final = Decimal("999999999999999999.99")

RuleId = Annotated[
    str, Field(pattern=r"^[1-9][0-9]{0,17}$", description="A rule's `id`.", examples=["31"])
]

#: Dollars, a decimal string both ways, so a threshold is sent back as it was set.
Threshold = Annotated[
    Decimal,
    Field(gt=0, le=MAX_THRESHOLD, decimal_places=2),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "decimal", "examples": ["1000000"]}),
]

_THRESHOLD = (
    "Dollars an event must reach to be told: a position change's traded value, a "
    "move in a stock's ownership, a cluster's buying. Null: every one. Only the "
    "kinds about dollars take one: `filer_position_change`, "
    "`stock_ownership_change`, `insider_cluster`."
)

_CHANNELS = (
    "Where its alerts go besides the list: `in_app` (the bell) and `email`. At least "
    "one; to stop it, turn it off."
)


def _channels(channels: list[Channel]) -> list[Channel]:
    """One of each, in :class:`Channel`'s order."""
    if not channels:
        raise ValueError("at least one channel: turn the rule off to stop it")
    return [channel for channel in Channel if channel in channels]


class RuleInvestor(BaseModel):
    """The investor a rule is about."""

    slug: str = Field(description="As `/v1/investors/{slug}` takes it.", examples=["berkshire"])
    display_name: str = Field(description="For a person.", examples=["Berkshire Hathaway"])


class RuleStock(BaseModel):
    """The stock a rule is about."""

    cusip: str = Field(description="Its CUSIP.", examples=["037833100"])
    ticker: str | None = Field(
        description="Its ticker today. Null when it has none.", examples=["AAPL"]
    )
    issuer_name: str = Field(description="As the SEC's list names it.", examples=["APPLE INC"])


class AlertRule(BaseModel):
    """One thing the user asked to be told about."""

    id: RuleId
    kind: AlertKind = Field(description="What kind of event.", examples=["filer_filed"])
    investor: RuleInvestor | None = Field(
        description="The investor, for `filer_filed` and `filer_position_change`; else null."
    )
    stock: RuleStock | None = Field(
        description="The stock, for `stock_ownership_change` and `insider_cluster`; else null."
    )
    min_value: Threshold | None = Field(description=_THRESHOLD, examples=["1000000"])
    channels: list[Channel] = Field(description=_CHANNELS, examples=[["in_app", "email"]])
    is_active: bool = Field(
        description="On. Off keeps its settings for when it is turned back on.", examples=[True]
    )
    sent: bool = Field(
        description=(
            "Whether the account's plan sends this kind now. False for every kind but "
            "`season_digest` on a plan without `alerts_instant`: such a rule, kept from "
            "a plan since left, sends nothing, and cannot be turned on or tested."
        ),
        examples=[True],
    )
    created_at: datetime = Field(description="When it was made.", examples=["2026-10-28T09:30:00Z"])


class AlertRules(BaseModel):
    """The user's rules, oldest first."""

    data: list[AlertRule] = Field(description="Oldest first, on and off. Empty when none.")


class NewAlertRule(BaseModel):
    """A rule to make, or the settings of one there is."""

    model_config = ConfigDict(extra="forbid")

    kind: AlertKind = Field(description="What kind of event.", examples=["filer_position_change"])
    ref: WatchRef | None = Field(
        default=None,
        description=(
            "What it is about: an investor for `filer_filed` and `filer_position_change`, "
            "a stock for `stock_ownership_change` and `insider_cluster`, nothing for "
            "`season_digest`."
        ),
    )
    min_value: Threshold | None = Field(default=None, description=_THRESHOLD, examples=["1000000"])
    channels: list[Channel] = Field(
        default_factory=lambda: [Channel.IN_APP],
        description=f"{_CHANNELS} Left out: `in_app`.",
        examples=[["in_app", "email"]],
    )

    _check_channels = field_validator("channels")(_channels)


class AlertRuleChange(BaseModel):
    """What to change of a rule. Each one left out is left as it is."""

    model_config = ConfigDict(extra="forbid")

    min_value: Threshold | None = Field(
        default=None, description=f"{_THRESHOLD} Null clears it.", examples=["5000000"]
    )
    channels: list[Channel] | None = Field(
        default=None, description=_CHANNELS, examples=[["in_app"]]
    )
    is_active: bool | None = Field(default=None, description="On or off.", examples=[False])

    @field_validator("channels")
    @classmethod
    def _check_channels(cls, channels: list[Channel] | None) -> list[Channel]:
        if channels is None:
            raise ValueError("at least one channel: turn the rule off to stop it")
        return _channels(channels)

    @field_validator("is_active")
    @classmethod
    def _not_null(cls, is_active: bool | None) -> bool:
        if is_active is None:
            raise ValueError("true or false")
        return is_active

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("change min_value, channels or is_active")
        return self


class AlertPresetRequest(BaseModel):
    """Something on the watchlist to be alerted about."""

    model_config = ConfigDict(extra="forbid")

    ref: WatchRef = Field(description="An investor or a stock.")


class AlertTest(BaseModel):
    """A test alert, sent as the rule would send one."""

    notification: Notification = Field(
        description="The notification written: in the list, unread, with `payload.test` true."
    )
    email: EmailStatus | None = Field(
        description=(
            "How far its email got: `sent`, `suppressed` (the address bounced or "
            "complained, or the rule has left the list) or `failed`. Null when the rule "
            "does not email, or when the email could not even be logged."
        ),
        examples=["sent"],
    )
