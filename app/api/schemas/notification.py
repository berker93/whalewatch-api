"""Models for ``/v1/me/notifications``: what the user was told, and what they have read.

A notification is drawn from itself: its title, body and payload are as they
were when it was sent, not as the data reads now (:mod:`app.db.models.alert`).
The payload carries what a client needs to link it to the view it describes:
the filer's slug, the ticker and the quarter.
"""

from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.pagination import MAX_LIMIT
from app.api.schemas.envelope import Envelope
from app.db.models.alert import AlertKind

#: Where the unread count stops counting: the badge shows ``99+`` past 99, so
#: a count past this is never needed, and never costs a scan of a backlog.
MAX_COUNTED: Final = 100

#: The most ids one request marks read: one page, at its largest.
MAX_IDS: Final = MAX_LIMIT

NotificationId = Annotated[
    str,
    Field(
        pattern=r"^[1-9][0-9]{0,17}$",
        description="A notification's `id`.",
        examples=["4812"],
    ),
]


class NotificationPayload(BaseModel):
    """What the notification is about, for a link to it. Every key is always
    present, null where it does not apply to the kind; a kind may add keys of
    its own."""

    model_config = ConfigDict(extra="allow")

    ticker: str | None = Field(
        description="The stock's ticker as of the event. Null for a kind about no stock.",
        examples=["AAPL"],
    )
    filer_slug: str | None = Field(
        description="The investor, as `/v1/investors/{slug}` takes it. Null for a kind about none.",
        examples=["berkshire-hathaway"],
    )
    period: str | None = Field(
        description="The quarter the event is about. Null for a Form 4 event.",
        examples=["2026Q3"],
    )
    delta: str | None = Field(
        description="The change, as a decimal string: dollars for a value. Null when none.",
        examples=["-1250000000.00"],
    )
    confidence: str | None = Field(
        description="How sure the numbers are, where a price was estimated. Null otherwise.",
        examples=["high"],
    )
    as_of: str | None = Field(
        description="The ISO date the event is as of: the filing, trade or quarter-end date.",
        examples=["2026-09-30"],
    )


class Notification(BaseModel):
    """One thing the user was told."""

    id: NotificationId
    kind: AlertKind = Field(description="What kind of event it is.", examples=["filer_filed"])
    title: str = Field(
        description="One line, as it was sent.", examples=["Berkshire Hathaway filed for 2026Q3"]
    )
    body: str = Field(
        description="A sentence or two, as it was sent. May be empty.",
        examples=["42 positions; 3 opened, 5 exited."],
    )
    payload: NotificationPayload = Field(
        description="What it is about, as it was when sent: enough to link it to its view."
    )
    created_at: datetime = Field(description="When it was sent.", examples=["2026-11-14T21:58:03Z"])
    read_at: datetime | None = Field(
        description="When it was first marked read. Null while unread.",
        examples=[None],
    )


class NotificationEnvelope(Envelope[Notification]):
    """The user's notifications, newest first."""


class NotificationCount(BaseModel):
    """How many notifications are unread, for the bell's badge."""

    unread: int = Field(
        ge=0,
        le=MAX_COUNTED,
        description=(
            f"Unread notifications, counted up to {MAX_COUNTED}: {MAX_COUNTED} is "
            f"{MAX_COUNTED} or more."
        ),
        examples=[3],
    )


class MarkRead(BaseModel):
    """Which notifications to mark read: `ids`, or `all`. Exactly one."""

    model_config = ConfigDict(extra="forbid")

    ids: list[NotificationId] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_IDS,
        description=(
            f"Up to {MAX_IDS} ids. One that is not the user's, or no longer exists, is no change."
        ),
        examples=[["4812", "4810"]],
    )
    all: Literal[True] | None = Field(
        default=None, description="Every unread notification.", examples=[True]
    )

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.ids is None) == (self.all is None):
            raise ValueError("send ids or all, not both and not neither")
        return self
