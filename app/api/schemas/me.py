"""``GET /v1/me``: who is signed in, their plan, and what it lets them do, in one answer."""

from datetime import datetime

from pydantic import BaseModel, Field


class MeUser(BaseModel):
    """The signed-in account."""

    email: str = Field(description="The login, as typed.", examples=["ada@example.com"])
    name: str = Field(description="The name on the account.", examples=["Ada Lovelace"])
    time_zone: str = Field(
        description="The IANA zone times are shown in.", examples=["America/New_York"]
    )
    email_verified: bool = Field(
        description="Whether the address has been verified. False: reads, saves nothing.",
        examples=[True],
    )
    email_verified_at: datetime | None = Field(
        description="When it was verified. Null while it is not.",
        examples=["2026-10-24T09:30:00Z"],
    )
    created_at: datetime = Field(
        description="When the account was made.", examples=["2026-10-18T10:00:00Z"]
    )
    last_seen_at: datetime | None = Field(
        description="The last signed-in request, to within five minutes. This one, usually.",
        examples=["2026-10-24T09:30:00Z"],
    )


class MePlan(BaseModel):
    """The plan the account is on."""

    id: str = Field(description="Stable, for a program to branch on.", examples=["free"])
    name: str = Field(description="For a person.", examples=["Free"])


class MeEntitlements(BaseModel):
    """What the account may do now: its plan's allowance, narrowed by its state."""

    save: bool = Field(
        description=(
            "Whether the account may save anything: follow and unfollow. False until its "
            "email is verified; a save then is a 403 `email_unverified`."
        ),
        examples=[True],
    )
    watchlist_max_items: int = Field(
        description="The most things the watchlist holds.", examples=[500]
    )


class Me(BaseModel):
    """The signed-in user, their plan and their entitlements."""

    user: MeUser = Field(description="Who is signed in.")
    plan: MePlan = Field(description="Which plan the account is on: Free, for everyone today.")
    entitlements: MeEntitlements = Field(description="What the account may do now.")
