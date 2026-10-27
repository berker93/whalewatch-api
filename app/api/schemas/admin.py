"""The admin console's requests and answers (:mod:`app.api.routers.admin`)."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.api.schemas.usage import Usage

_AT = "2026-10-27T14:03:00Z"
_ADMIN = "ops@whalewatch.io"

EntitlementValue = int | bool | None


class AdminLoginRequest(BaseModel):
    """An admin's credentials: not a user's."""

    email: str = Field(max_length=320, description="The admin's login.", examples=[_ADMIN])
    password: str = Field(
        max_length=1024, description="The admin's password.", examples=["correct horse 42"]
    )


class AdminSession(BaseModel):
    """Who is signed in to the console, and until when."""

    email: str = Field(description="The admin's login.", examples=[_ADMIN])
    name: str = Field(description="The admin's name.", examples=["Grace Hopper"])
    expires_at: datetime = Field(
        description="When the session ends, however busy the admin is.", examples=[_AT]
    )


class AdminSessionState(BaseModel):
    """The console's session, when the admin cookie names a live one."""

    session: AdminSession = Field(description="Who, and until when.")


class AdminUserRow(BaseModel):
    """One account, as a row of the user list."""

    id: int = Field(description="The account's id: what the other routes take.", examples=[42])
    email: str = Field(description="The login.", examples=["ada@example.com"])
    name: str = Field(description="The name on the account.", examples=["Ada Lovelace"])
    plan: str = Field(
        description=(
            "The plan the account's entitlements come from: its override's, else its live "
            "subscription's, else `free`."
        ),
        examples=["pro"],
    )
    billed_plan: str | None = Field(
        description="The live subscription's plan, whatever an override says.", examples=["pro"]
    )
    subscription_status: str | None = Field(
        description="The live subscription's status.", examples=["active"]
    )
    grandfathered_plan: str | None = Field(
        description="The snapshot or promise the live subscription keeps the terms of.",
        examples=[None],
    )
    override_plan: str | None = Field(
        description="The live override's plan: set, the plan is a comp.", examples=[None]
    )
    override_expires_at: datetime | None = Field(
        description="When the live override ends on its own.", examples=[None]
    )
    created_at: datetime = Field(description="When the account signed up.", examples=[_AT])
    last_seen_at: datetime | None = Field(
        description="The last request signed in or with an API key, to within five minutes.",
        examples=[_AT],
    )
    email_verified_at: datetime | None = Field(
        description="When the email was verified; null while it is not.", examples=[_AT]
    )
    deleted_at: datetime | None = Field(
        description="When it was deleted: it is erased 30 days on.", examples=[None]
    )


class AdminUserPage(BaseModel):
    """One page of the user list."""

    items: list[AdminUserRow] = Field(description="The accounts on this page.")
    total: int = Field(description="How many accounts match, on every page.", examples=[1204])
    limit: int = Field(description="The page size asked for.", examples=[50])
    offset: int = Field(description="How many matches came before this page.", examples=[0])


class AdminSubscription(BaseModel):
    """One subscription the account has had, newest first."""

    id: int = Field(description="The subscription's id.", examples=[7])
    plan: str = Field(description="What it is billed as.", examples=["pro"])
    status: str = Field(description="As Paddle names it.", examples=["active"])
    grandfathered_plan: str | None = Field(
        description="The plan whose terms it keeps, if any.", examples=[None]
    )
    current_period_end: datetime | None = Field(
        description="When the period paid for ends.", examples=[_AT]
    )
    cancel_at: datetime | None = Field(description="When it is set to end.", examples=[None])
    past_due_since: datetime | None = Field(
        description="When a renewal's payment first failed, while unpaid.", examples=[None]
    )
    grace_ends_at: datetime | None = Field(
        description="When an unpaid renewal stops granting its plan.", examples=[None]
    )
    paddle_subscription_id: str | None = Field(
        description="Open it in Paddle's dashboard for payments and invoices.",
        examples=["sub_01hv8x2k3m"],
    )
    paddle_customer_id: str | None = Field(
        description="Who pays, in Paddle.", examples=["ctm_01hv8x2k3m"]
    )
    created_at: datetime = Field(description="When it began.", examples=[_AT])


class AdminOverride(BaseModel):
    """One override the account was given, newest first."""

    id: int = Field(description="The override's id.", examples=[3])
    plan: str = Field(description="The plan it gives, whole.", examples=["pro"])
    reason: str = Field(description="Why it was given.", examples=["friend of the founders"])
    starts_at: datetime = Field(description="When it began.", examples=[_AT])
    expires_at: datetime = Field(description="When it ends on its own.", examples=[_AT])
    ended_at: datetime | None = Field(
        description="When it was ended early, or replaced by another.", examples=[None]
    )
    created_by: str = Field(description="The admin who gave it.", examples=[_ADMIN])
    ended_by: str | None = Field(description="The admin who ended it.", examples=[None])
    live: bool = Field(description="Granting its plan now.", examples=[True])


class AdminCount(BaseModel):
    """How many of something the account holds, against its plan."""

    count: int = Field(description="Held now.", examples=[3])
    limit: int | None = Field(description="The plan's; null for no limit.", examples=[3])


class AdminApiKeys(BaseModel):
    """The account's API keys."""

    live: int = Field(description="Keys that work.", examples=[1])
    last_used_at: datetime | None = Field(
        description="The last request made with any of them.", examples=[_AT]
    )


class AdminExports(BaseModel):
    """The account's data exports (`GET /v1/me/export`)."""

    total: int = Field(description="Taken ever.", examples=[2])
    last_on: date | None = Field(
        description="The last UTC day one was taken.", examples=["2026-10-20"]
    )


class AdminUsageMonth(BaseModel):
    """One quota month of one metered thing, as last flushed."""

    key: str = Field(
        description="`api_calls_month` or `mcp_calls_month`.", examples=["api_calls_month"]
    )
    period: date = Field(description="The quota month's first day.", examples=["2026-10-14"])
    count: int = Field(description="Calls counted.", examples=[8125])
    warned_at: datetime | None = Field(
        description="When the 80% email went that month.", examples=[None]
    )


class AdminUsage(BaseModel):
    """What the account has used."""

    quotas: Usage = Field(description="This quota month's calls, live from the meter.")
    months: list[AdminUsageMonth] = Field(
        description="The last twelve quota months as flushed, newest first."
    )
    saved_filters: AdminCount = Field(description="Screener filters saved.")
    watchlist_items: AdminCount = Field(description="Things followed.")
    api_keys: AdminApiKeys = Field(description="API keys.")
    data_exports: AdminExports = Field(description="Data exports taken.")
    active_days_30: int = Field(
        description="Days active in the last 30, signed in or by API key.", examples=[12]
    )


class AdminActionRow(BaseModel):
    """One logged admin action."""

    id: int = Field(description="The action's id: `before` pages by it.", examples=[118])
    at: datetime = Field(description="When.", examples=[_AT])
    source: Literal["console", "cli"] = Field(
        description="`console`: an admin, signed in. `cli`: a shell on a server.",
        examples=["console"],
    )
    admin_id: int | None = Field(
        description="Who, from the console; null from the CLI.", examples=[1]
    )
    action: str = Field(
        description=(
            "`login`, `logout`, `override_set`, `override_ended`, `grandfathered`, "
            "`ungrandfathered`, `admin_created` or `admin_disabled`."
        ),
        examples=["override_set"],
    )
    user_id: int | None = Field(description="The account acted on, if any.", examples=[42])
    target_admin_id: int | None = Field(
        description="The admin acted on, by `admin_created` and `admin_disabled`.",
        examples=[None],
    )
    request_id: str | None = Field(
        description="The request it was made in, to find its log lines by.",
        examples=["3f2c9a7e5b1d4c0e8a6f2d9b7c5e1a30"],
    )
    detail: dict[str, Any] = Field(
        description="What changed: plans by slug, the reason, the expiry.",
        examples=[{"plan": "pro", "reason": "beta tester"}],
    )


class AdminActionPage(BaseModel):
    """One page of the admin log, newest first."""

    items: list[AdminActionRow] = Field(description="The actions on this page.")
    next_before: int | None = Field(
        description="Pass as `before` for the next page; null on the last.", examples=[68]
    )


class AdminUserDetail(BaseModel):
    """One account: who, on what, given what, and using what."""

    account: AdminUserRow = Field(description="Its row of the user list.")
    entitlements: dict[str, EntitlementValue] = Field(
        description="Every key as the account resolves it now.",
        examples=[{"history_quarters": 20, "export_enabled": True}],
    )
    subscriptions: list[AdminSubscription] = Field(description="Every one it has had.")
    overrides: list[AdminOverride] = Field(description="Every one it was given.")
    usage: AdminUsage = Field(description="What it has used.")
    actions: list[AdminActionRow] = Field(description="The last 50 admin actions about it.")


class OverrideRequest(BaseModel):
    """A plan to give an account, why, and until when."""

    plan: str = Field(description="The plan's slug: `GET /admin/plans`.", examples=["pro"])
    reason: str = Field(
        max_length=500,
        description="Why, at least 3 characters: kept with it and in the log.",
        examples=["beta tester"],
    )
    expires_at: datetime = Field(
        description="When it ends on its own: in the future, with a time zone.",
        examples=["2027-01-31T23:59:59Z"],
    )


class ReasonRequest(BaseModel):
    """Why, for the log. Optional."""

    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why, kept in the log.",
        examples=["support ticket 412"],
    )


class OverrideSet(BaseModel):
    """The override given."""

    override: AdminOverride = Field(description="The override, as the account's page lists it.")
    replaced: list[int] = Field(description="The overrides this one ended.", examples=[[2]])


class OverrideEnded(BaseModel):
    """The overrides ended."""

    ended: list[int] = Field(description="Their ids.", examples=[[3]])


class Grandfathered(BaseModel):
    """The snapshot the subscription now keeps the terms of."""

    plan: str = Field(description="The plan the subscription is on.", examples=["pro"])
    snapshot: str = Field(
        description="The private plan holding its terms as of now.", examples=["pro_20261027"]
    )
    snapshot_created: bool = Field(
        description="False: an earlier snapshot with the same terms was reused.",
        examples=[True],
    )


class Ungrandfathered(BaseModel):
    """The terms the subscription no longer keeps."""

    snapshot: str = Field(
        description="The plan whose terms it kept until now.", examples=["pro_20261027"]
    )


class AdminPlan(BaseModel):
    """One plan, public, private or a grandfathering snapshot."""

    slug: str = Field(description="What overrides name it by.", examples=["pro"])
    name: str = Field(description="For a person.", examples=["Pro"])
    price_cents: int = Field(description="Per interval, in cents.", examples=[1400])
    currency: str = Field(description="ISO 4217.", examples=["USD"])
    interval: str | None = Field(
        description="`month` or `year`; null for a plan that costs nothing.", examples=["month"]
    )
    is_public: bool = Field(description="Offered on the pricing page.", examples=[True])
    snapshot_of: str | None = Field(
        description="The plan this is a grandfathering snapshot of.", examples=[None]
    )
    snapshot_at: datetime | None = Field(
        description="When the snapshot was taken.", examples=[None]
    )
    values: dict[str, EntitlementValue] = Field(
        description="What it allows, every key.",
        examples=[{"history_quarters": 20, "export_enabled": True}],
    )


class MetricPoint(BaseModel):
    """One day, week or month."""

    start: date = Field(description="Its first UTC day in the range.", examples=["2026-10-01"])
    end: date = Field(
        description="Its last UTC day in the range: today, for the one still running.",
        examples=["2026-10-27"],
    )
    signups: int = Field(description="Accounts made.", examples=[41])
    deletions: int = Field(description="Accounts whose deletion was asked for.", examples=[2])
    active: int | None = Field(
        description=(
            "Accounts active, signed in or by API key: that day for `every=day`, the 7 "
            "days ending `end` for a week, the 30 for a month. Null before activity "
            "was recorded."
        ),
        examples=[310],
    )
    accounts: int | None = Field(
        description="Accounts not deleted, at its last snapshot.", examples=[1204]
    )
    plan_mix: dict[str, int] | None = Field(
        description="Those accounts by plan slug, `free` included.",
        examples=[{"free": 1100, "pro": 98, "api_hobbyist": 6}],
    )
    comped: int | None = Field(
        description="Of those, how many have their plan by override.", examples=[4]
    )
    partial: bool = Field(
        description="Today is in it, or a day has no snapshot: not final.", examples=[False]
    )


class MetricSeries(BaseModel):
    """Signups, actives and plan mix over a range of UTC days."""

    every: Literal["day", "week", "month"] = Field(
        description="What each point is: a day, an ISO week, a calendar month.",
        examples=["week"],
    )
    start: date = Field(description="The range's first UTC day.", examples=["2026-05-04"])
    end: date = Field(description="The range's last UTC day.", examples=["2026-10-27"])
    points: list[MetricPoint] = Field(description="Oldest first.")
