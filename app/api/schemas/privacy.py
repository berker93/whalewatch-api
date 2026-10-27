"""The user's data, as the user sees it: what it is used for, a copy of all of it, and deleting it.

``GET``/``PATCH /v1/me/privacy``, ``GET /v1/me/export`` and ``DELETE /v1/account``.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.types import Money
from app.db.models import Basis, Purpose


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PurposeStanding(BaseModel):
    """One purpose the site uses the account's data for, its basis, and where the user stands."""

    purpose: Purpose = Field(
        description=(
            "`account`: running the account and the emails it needs (verify, reset, change). "
            "`product_email`: news about the product. `marketing_email`: promotion."
        ),
        examples=["marketing_email"],
    )
    basis: Basis = Field(
        description=(
            "The lawful basis: `contract` for the account, `legitimate_interest` for "
            "product email (objectable), `consent` for marketing email (opt-in)."
        ),
        examples=["consent"],
    )
    granted: bool = Field(
        description=(
            "Whether it applies: consent given, or not objected to. `account` is always "
            "true; it ends by deleting the account."
        ),
        examples=[False],
    )
    changeable: bool = Field(
        description="Whether `PATCH /v1/me/privacy` changes it: the two email purposes.",
        examples=[True],
    )
    since: datetime | None = Field(
        description="When the user's answer was recorded. Null for a purpose never asked about.",
        examples=["2026-10-24T09:30:00Z"],
    )


class TermsStanding(BaseModel):
    """Which terms the account accepted, against the ones in force."""

    accepted_version: str | None = Field(
        description="The terms accepted, by the date they took effect. Null: none on file.",
        examples=["2026-10-24"],
    )
    accepted_at: datetime | None = Field(
        description="When they were accepted.", examples=["2026-10-24T09:30:00Z"]
    )
    current_version: str = Field(description="The terms in force.", examples=["2026-10-24"])
    privacy_version: str = Field(
        description="The privacy policy in force.", examples=["2026-10-24"]
    )


class Privacy(BaseModel):
    """What the account's data is used for, and on what basis."""

    terms: TermsStanding = Field(description="The terms accepted and those in force.")
    purposes: list[PurposeStanding] = Field(
        description="Every purpose, `account` first, each present."
    )


class PrivacyPatch(_Request):
    """The user's choices: either, or both. Left out or null, a choice stays."""

    product_email: bool | None = Field(
        default=None,
        description="False objects to product email; true withdraws the objection.",
        examples=[True],
    )
    marketing_email: bool | None = Field(
        default=None,
        description="True consents to marketing email; false withdraws the consent.",
        examples=[False],
    )


class AccountDeleteRequest(_Request):
    """The password, to prove it is the owner deleting."""

    password: str = Field(description="The account's password.", examples=["correct horse 42"])


# --- the export -----------------------------------------------------------------

_AT = "2026-10-24T09:30:00Z"


class ExportAccount(BaseModel):
    """The account row, all of it but the password's hash."""

    id: int = Field(description="The account's internal number.", examples=[42])
    email: str = Field(description="The login, as typed.", examples=["ada@example.com"])
    name: str = Field(description="The name on the account.", examples=["Ada Lovelace"])
    time_zone: str = Field(
        description="The IANA zone times are shown in.", examples=["Europe/London"]
    )
    created_at: datetime = Field(description="When the account was made.", examples=[_AT])
    password_changed_at: datetime = Field(
        description="The last password change, or signup.", examples=[_AT]
    )
    email_verified_at: datetime | None = Field(
        description="When the address was verified. Null while it is not.", examples=[_AT]
    )
    last_seen_at: datetime | None = Field(
        description="The last signed-in request, to within five minutes.", examples=[_AT]
    )


class ExportLink(BaseModel):
    """An emailed link not yet used. The token itself is not kept, so not exported."""

    purpose: Literal["email_verify", "email_change", "password_reset"] = Field(
        description="What following it does.", examples=["email_change"]
    )
    new_email: str | None = Field(
        description="For an email change, the address it changes to.",
        examples=["ada@new.example"],
    )
    created_at: datetime = Field(description="When it was sent.", examples=[_AT])
    expires_at: datetime = Field(description="When it stops working.", examples=[_AT])


class ExportSession(BaseModel):
    """A signed-in session. Its cookie is not kept, so not exported."""

    remember: bool = Field(
        description="Whether it was a keep-me-signed-in session.", examples=[True]
    )
    expires_at: datetime = Field(description="When it ends.", examples=[_AT])


class ExportStock(BaseModel):
    """A followed stock."""

    cusip: str = Field(description="Its CUSIP.", examples=["037833100"])
    ticker: str | None = Field(description="Its ticker, if known.", examples=["AAPL"])
    name: str | None = Field(description="Its issuer's name, if known.", examples=["APPLE INC"])
    added_at: datetime = Field(description="When it was followed.", examples=[_AT])


class ExportInvestor(BaseModel):
    """A followed investor."""

    slug: str = Field(description="Its slug.", examples=["berkshire-hathaway"])
    name: str = Field(description="Its name.", examples=["Berkshire Hathaway"])
    added_at: datetime = Field(description="When it was followed.", examples=[_AT])


class ExportInsider(BaseModel):
    """A followed insider."""

    cik: str = Field(description="Their CIK.", examples=["0001197647"])
    name: str = Field(description="Their name, as filed.", examples=["HUANG JEN HSUN"])
    added_at: datetime = Field(description="When they were followed.", examples=[_AT])


class ExportWatchlist(BaseModel):
    """What the account follows, each list oldest first."""

    securities: list[ExportStock] = Field(description="Followed stocks.")
    investors: list[ExportInvestor] = Field(description="Followed investors.")
    insiders: list[ExportInsider] = Field(description="Followed insiders.")


class ExportSavedFilter(BaseModel):
    """A saved screener filter."""

    name: str = Field(description="What the user called it.", examples=["Crowded buys"])
    query: dict[str, Any] = Field(
        description="`GET /v1/flows`'s parameters, as saved.", examples=[{"direction": "buy"}]
    )
    created_at: datetime = Field(description="When it was saved.", examples=[_AT])


class ExportAlertRule(BaseModel):
    """One thing the account asked to be told about."""

    kind: str = Field(description="The kind of event.", examples=["filer_filed"])
    filer_slug: str | None = Field(
        description="The investor it is about. Null for a kind about none.",
        examples=["berkshire-hathaway"],
    )
    cusip: str | None = Field(
        description="The stock it is about. Null for a kind about none.", examples=["037833100"]
    )
    min_value: Money | None = Field(
        description="Dollars below which an event is not told. Null: every event.",
        examples=[None],
    )
    channels: list[str] = Field(description="Where it is told.", examples=[["in_app"]])
    is_active: bool = Field(description="Whether it is on.", examples=[True])
    created_at: datetime = Field(description="When it was made.", examples=[_AT])


class ExportNotification(BaseModel):
    """One thing the account was told."""

    kind: str = Field(description="The kind of event.", examples=["filer_filed"])
    title: str = Field(description="As it was sent.", examples=["Berkshire Hathaway filed"])
    body: str = Field(description="As it was sent.", examples=[""])
    payload: dict[str, Any] = Field(
        description="What it was about.", examples=[{"filer_slug": "berkshire-hathaway"}]
    )
    created_at: datetime = Field(description="When it was sent.", examples=[_AT])
    read_at: datetime | None = Field(description="When it was read.", examples=[None])


class ExportConsent(BaseModel):
    """One row of the lawful-basis ledger."""

    purpose: Purpose = Field(description="What the data is used for.", examples=["account"])
    basis: Basis = Field(description="The lawful basis it rested on.", examples=["contract"])
    granted: bool = Field(
        description="Consent given, or not objected to, from then on.", examples=[True]
    )
    terms_version: str | None = Field(
        description="For `account`, the terms accepted. Null: none on file.",
        examples=["2026-10-24"],
    )
    privacy_version: str | None = Field(
        description="The privacy policy in force at the time.", examples=["2026-10-24"]
    )
    source: Literal["register", "create_user", "preferences"] = Field(
        description=(
            "`register`: the signup form. `create_user`: an operator made the account. "
            "`preferences`: the user changed it."
        ),
        examples=["register"],
    )
    recorded_at: datetime = Field(description="When.", examples=[_AT])


class ExportSubscription(BaseModel):
    """One subscription, live or ended, newest first."""

    plan: str = Field(description="The plan's slug.", examples=["pro"])
    status: str = Field(description="As the payment provider names it.", examples=["active"])
    current_period_end: datetime | None = Field(
        description="When the period paid for ends.", examples=[_AT]
    )
    cancel_at: datetime | None = Field(
        description="When it ends, if it is set to.", examples=[None]
    )
    grandfathered_plan: str | None = Field(
        description="The plan whose terms the account keeps, if any.", examples=[None]
    )
    overrides: dict[str, int | bool | None] = Field(
        description="Entitlements set for this subscription alone.", examples=[{}]
    )
    created_at: datetime = Field(description="When it began.", examples=[_AT])


class ExportApiKey(BaseModel):
    """One API key, working or revoked: never the key, which is not kept."""

    name: str = Field(description="What the user called it.", examples=["nightly job"])
    prefix: str = Field(description="Its first characters.", examples=["ww_Ab12Cd34"])
    created_at: datetime = Field(description="When it was made.", examples=[_AT])
    last_used_at: datetime | None = Field(
        description="The last request made with it, to within five minutes.", examples=[_AT]
    )
    revoked_at: datetime | None = Field(
        description="When it stopped working; null while it works.", examples=[None]
    )


class ExportUsage(BaseModel):
    """One month's count of one metered thing, newest first."""

    key: str = Field(
        description="`api_calls_month` or `mcp_calls_month`.", examples=["api_calls_month"]
    )
    period: date = Field(description="The quota month's first day.", examples=["2026-10-14"])
    count: int = Field(
        description="Calls counted, as last written from the live counter.", examples=[8125]
    )
    warned_at: datetime | None = Field(
        description="When the 80% email was sent that month, if it was.", examples=[None]
    )


class ExportPlanOverride(BaseModel):
    """A plan given to the account by WhaleWatch's staff, newest first."""

    plan: str = Field(description="The plan's slug.", examples=["pro"])
    reason: str = Field(description="Why it was given.", examples=["beta tester"])
    starts_at: datetime = Field(description="When it began.", examples=[_AT])
    expires_at: datetime = Field(description="When it ends on its own.", examples=[_AT])
    ended_at: datetime | None = Field(
        description="When it was ended early, if it was.", examples=[None]
    )


class ExportActivityDay(BaseModel):
    """One day the account was used, signed in or with an API key, newest first."""

    day: date = Field(description="The day, in UTC.", examples=["2026-10-27"])
    data_exports: int = Field(description="Exports like this one taken that day.", examples=[1])


class Export(BaseModel):
    """Everything WhaleWatch holds on the signed-in user."""

    format: Literal["whalewatch-export/1"] = Field(
        description="What this document is, and its version.", examples=["whalewatch-export/1"]
    )
    exported_at: datetime = Field(description="When it was made.", examples=[_AT])
    account: ExportAccount = Field(description="The account.")
    watchlist: ExportWatchlist = Field(description="What the account follows.")
    saved_filters: list[ExportSavedFilter] = Field(
        description="The screener filters saved, oldest first."
    )
    alert_rules: list[ExportAlertRule] = Field(
        description="What the account asked to be told about, oldest first."
    )
    notifications: list[ExportNotification] = Field(
        description="Everything the account was told, newest first."
    )
    consents: list[ExportConsent] = Field(description="The whole ledger, oldest first.")
    links: list[ExportLink] = Field(description="Emailed links still waiting to be used.")
    sessions: list[ExportSession] = Field(description="Sessions still signed in.")
    subscriptions: list[ExportSubscription] = Field(
        description="Every subscription the account has had, newest first."
    )
    api_keys: list[ExportApiKey] = Field(description="Every API key made, oldest first.")
    usage: list[ExportUsage] = Field(
        description="What was counted of each monthly quota, by month, newest first."
    )
    plan_overrides: list[ExportPlanOverride] = Field(
        description="Every plan staff gave the account, newest first."
    )
    activity: list[ExportActivityDay] = Field(
        description="Every day the account was used, newest first: this export's included."
    )
    not_included: list[str] = Field(
        description="What is held but not here, and why: a person can still ask for it.",
        examples=[["server logs: request lines (time, path, IP address)"]],
    )
