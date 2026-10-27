"""``GET /v1/me``: who is signed in, their plan, what it lets them do, and what
they have spent of it this month, in one answer."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.api.schemas.usage import Usage


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
    """The plan the account is on: what it is billed for."""

    id: str = Field(description="Stable, for a program to branch on.", examples=["free"])
    name: str = Field(description="For a person.", examples=["Free"])


class MeEntitlements(BaseModel):
    """What the account may do now: its plan's allowance, with any terms it was
    promised and any granted to it alone, and `save` from its own state.

    A limit is a whole number, or null for no limit; a flag is true or false.
    The values are rows, not code: they change without a release, so read them
    here rather than writing a plan's numbers into the page."""

    save: bool = Field(
        description=(
            "Whether the account may save anything: follow and unfollow. False until its "
            "email is verified; a save then is a 403 `email_unverified`."
        ),
        examples=[True],
    )
    history_quarters: int | None = Field(
        description="How many of the latest quarters of holdings the account reads.",
        examples=[4],
    )
    insider_months: int | None = Field(
        description="How many months of insider trades back the account reads.", examples=[6]
    )
    watchlist_items: int | None = Field(
        description="The most things the watchlist holds. Null: no limit but the 500 any holds.",
        examples=[10],
    )
    screener_saved_filters: int | None = Field(
        description="How many screener filters the account may save.", examples=[3]
    )
    api_calls_month: int | None = Field(
        description="API calls a month, made with the account's API keys. 0: no API access.",
        examples=[0],
    )
    mcp_calls_month: int | None = Field(
        description="Calls through the MCP server a month. 0: none.", examples=[0]
    )
    export_enabled: bool = Field(description="CSV export of tables.", examples=[False])
    alerts_instant: bool = Field(
        description="Same-day filing alerts, rather than the Monday digest.", examples=[False]
    )
    mcp_enabled: bool = Field(description="Access through the MCP server.", examples=[False])


class Me(BaseModel):
    """The signed-in user, their plan and their entitlements."""

    user: MeUser = Field(description="Who is signed in.")
    plan: MePlan = Field(description="Which plan the account is on: Free without a subscription.")
    entitlements: MeEntitlements = Field(description="What the account may do now.")
    usage: Usage = Field(description="This month's spending of each monthly quota.")
