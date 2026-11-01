"""Request and response models for ``/v1/me/alert-delivery``: when alert email may go."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: ``HH:MM``, 24-hour: a minute of the day.
CLOCK_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

Clock = Annotated[str, Field(pattern=CLOCK_PATTERN)]


class AlertDelivery(BaseModel):
    """When the signed-in user's alert email goes, and the rules it goes by."""

    time_zone: str = Field(
        description=(
            "An IANA zone: where the user is, which quiet hours, the day the cap "
            "counts and the digest's evening are on. The browser's at signup. Not "
            "the account's `time_zone`, which is what dates are shown in."
        ),
        examples=["Europe/Berlin"],
    )
    quiet_start: str = Field(
        description="When quiet hours begin, `HH:MM`: no alert email from then.",
        examples=["22:00"],
    )
    quiet_end: str = Field(
        description=(
            "When they end, `HH:MM`: what waited goes then, as one email. Before "
            "`quiet_start`, they run past midnight; equal to it, there are none."
        ),
        examples=["08:00"],
    )
    batch_minutes: int = Field(
        description=(
            "How long an alert email waits for more, from its first alert: "
            "everything in that time is one email."
        ),
        examples=[15],
    )
    emails_per_day: int = Field(
        description=(
            "The most alert emails in one of the user's days, the digest included. "
            "What does not fit goes in the next digest."
        ),
        examples=[3],
    )
    digest_at: str = Field(
        description=(
            "When the digest goes, `HH:MM`: the season's filings by investors on the "
            "watchlist, during a filing window, and the day's alerts past the cap. "
            "Nothing to say, no digest."
        ),
        examples=["19:00"],
    )


class AlertDeliveryPatch(BaseModel):
    """The settings to change. One left out, or null, stays as it is."""

    model_config = ConfigDict(extra="forbid")

    time_zone: str | None = Field(
        default=None, description="An IANA zone, such as `Europe/Berlin`.", examples=["Asia/Tokyo"]
    )
    quiet_start: Clock | None = Field(
        default=None, description="`HH:MM`, 24-hour.", examples=["23:00"]
    )
    quiet_end: Clock | None = Field(
        default=None, description="`HH:MM`, 24-hour.", examples=["07:00"]
    )
