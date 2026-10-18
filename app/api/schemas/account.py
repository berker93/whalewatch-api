"""Request and response models for ``/v1/account``: the signed-in user's profile."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AccountProfile(BaseModel):
    """The signed-in user's account, as the account page shows and edits it."""

    name: str = Field(description="The name on the account, trimmed.", examples=["Ada Lovelace"])
    email: str = Field(
        description="The login, lowercased. Unchanged until a new one is confirmed.",
        examples=["ada@example.com"],
    )
    time_zone: str = Field(
        description=(
            "An IANA zone: what the timeline's days and Form 4 times are shown in, "
            "and what `/v1/timeline` counts days in when sent no `tz`."
        ),
        examples=["America/New_York"],
    )
    member_since: datetime = Field(
        description="When the account was made.", examples=["2026-10-17T09:30:00Z"]
    )
    password_changed_at: datetime = Field(
        description="The last password change, or `member_since` when it never changed.",
        examples=["2026-10-17T09:30:00Z"],
    )
    pending_email: str | None = Field(
        default=None,
        description=(
            "Where the login is changing to, while the confirmation link sent there "
            "waits. Null when no change is waiting."
        ),
        examples=[None],
    )


class AccountPatch(BaseModel):
    """The fields to change. Each is optional, and one left out, or null, stays as it is."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        description="At least 2 characters once trimmed. Applied at once.",
        examples=["Ada King"],
    )
    email: str | None = Field(
        default=None,
        description=(
            "A new login. Not applied: a confirmation link goes to it, a notice to the "
            "current address, and `pending_email` names it until it is confirmed. The "
            "current address, sent again, cancels a waiting change."
        ),
        examples=["ada@analytical.engine"],
    )
    time_zone: str | None = Field(
        default=None,
        description="An IANA zone, such as `Europe/London`. Applied at once.",
        examples=["Europe/London"],
    )


class AccountUpdate(BaseModel):
    """The profile after the change, and where a new email's confirmation went."""

    profile: AccountProfile = Field(description="The whole profile, as it is now.")
    pending_email: str | None = Field(
        description=(
            "Where the confirmation link went, when the change asked for a new email "
            "or one was already waiting. Null otherwise. The same as "
            "`profile.pending_email`."
        ),
        examples=["ada@analytical.engine"],
    )


class EmailConfirm(BaseModel):
    """The token from an email change's confirmation link."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(
        description="The `token` query parameter of the emailed link.",
        examples=["Zq3c9V0yJmL2x5r8u1N4a7D6b0E3f9H2k5M8p1S4t7W"],
    )
