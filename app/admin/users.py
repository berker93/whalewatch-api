"""The user list and one user's page, for the admin console.

One query (:func:`accounts`) says, for every account, what the console shows
in a row: its email, its plan as entitlements resolve it (an override's, else
a live subscription's, else Free), the plan it is billed for, whether it is
grandfathered, and when it was last seen, signed in or by API key. The list
filters and sorts that query; the plan mix (:mod:`app.admin.metrics`) counts
it. It is a scan of ``app_user`` with a lookup per row: fine for the thousands
of accounts an admin pages through, and never on a user's request.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import (
    Row,
    Select,
    Subquery,
    and_,
    func,
    literal,
    or_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.accounts.entitlements import FREE, Grant, override_is_live, subscription_is_live
from app.db.models import (
    AdminUser,
    AlertRule,
    ApiKey,
    AppUser,
    Plan,
    PlanOverride,
    SavedFilter,
    Subscription,
    UsageCounter,
    UserActivityDay,
    WatchlistItem,
)

Status = Literal["active", "unverified", "deleted", "all"]
Sort = Literal["signed_up", "last_seen", "email"]


def accounts() -> Subquery:
    """Every account, one row each, with its plan and when it was last seen."""
    sub = (
        select(Subscription.status, Subscription.plan_id, Subscription.grandfathered_plan_id)
        .where(Subscription.user_id == AppUser.id, subscription_is_live())
        .lateral("sub")
    )
    override = (
        select(PlanOverride.plan_id, PlanOverride.expires_at)
        .where(PlanOverride.user_id == AppUser.id, override_is_live())
        .order_by(PlanOverride.id.desc())
        .limit(1)
        .lateral("override")
    )
    keys = (
        select(func.max(ApiKey.last_used_at).label("last_used_at"))
        .where(ApiKey.user_id == AppUser.id)
        .lateral("keys")
    )
    billed, held, given = aliased(Plan), aliased(Plan), aliased(Plan)
    return (
        select(
            AppUser.id,
            AppUser.email,
            AppUser.name,
            AppUser.created_at,
            AppUser.email_verified_at,
            AppUser.deleted_at,
            # GREATEST ignores nulls: null only for an account never seen either way.
            func.greatest(AppUser.last_seen_at, keys.c.last_used_at).label("last_seen_at"),
            func.coalesce(given.slug, billed.slug, literal(FREE)).label("plan"),
            billed.slug.label("billed_plan"),
            sub.c.status.label("subscription_status"),
            held.slug.label("grandfathered_plan"),
            given.slug.label("override_plan"),
            override.c.expires_at.label("override_expires_at"),
        )
        .select_from(AppUser)
        .outerjoin(sub, true())
        .outerjoin(billed, billed.id == sub.c.plan_id)
        .outerjoin(held, held.id == sub.c.grandfathered_plan_id)
        .outerjoin(override, true())
        .outerjoin(given, given.id == override.c.plan_id)
        .outerjoin(keys, true())
        .subquery("accounts")
    )


@dataclass(frozen=True, slots=True)
class UserFilter:
    """What the list is narrowed to. Every field None or default: everyone not deleted."""

    q: str | None = None
    """A piece of the email or the name, any case."""
    plan: str | None = None
    """The plan entitlements resolve to, by slug: ``free`` included."""
    status: Status = "active"
    """``active``: not deleted. ``unverified``: not deleted, email not verified."""
    signed_up_from: date | None = None
    signed_up_to: date | None = None
    """Inclusive, UTC days."""
    seen_from: date | None = None
    seen_to: date | None = None
    comped: bool | None = None
    """Whether the plan comes from an override."""
    grandfathered: bool | None = None


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _filtered(rows: Subquery, wanted: UserFilter) -> Select[Any]:
    query = select(rows)
    c = rows.c
    if wanted.q:
        query = query.where(
            or_(
                c.email.ilike(_like(wanted.q), escape="\\"),
                c.name.ilike(_like(wanted.q), escape="\\"),
            )
        )
    if wanted.plan:
        query = query.where(c.plan == wanted.plan)
    if wanted.status == "active":
        query = query.where(c.deleted_at.is_(None))
    elif wanted.status == "unverified":
        query = query.where(c.deleted_at.is_(None), c.email_verified_at.is_(None))
    elif wanted.status == "deleted":
        query = query.where(c.deleted_at.is_not(None))
    for column, start, end in (
        (c.created_at, wanted.signed_up_from, wanted.signed_up_to),
        (c.last_seen_at, wanted.seen_from, wanted.seen_to),
    ):
        if start is not None:
            query = query.where(column >= _day_start(start))
        if end is not None:
            query = query.where(column < _day_start(end + timedelta(days=1)))
    if wanted.comped is not None:
        query = query.where(
            c.override_plan.is_not(None) if wanted.comped else c.override_plan.is_(None)
        )
    if wanted.grandfathered is not None:
        query = query.where(
            c.grandfathered_plan.is_not(None)
            if wanted.grandfathered
            else c.grandfathered_plan.is_(None)
        )
    return query


async def list_users(
    session: AsyncSession,
    wanted: UserFilter,
    *,
    sort: Sort = "signed_up",
    descending: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Row[Any]], int]:
    """A page of the accounts ``wanted`` matches, and how many match in all."""
    rows = accounts()
    query = _filtered(rows, wanted)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    column = {
        "signed_up": rows.c.created_at,
        "last_seen": rows.c.last_seen_at,
        "email": rows.c.email,
    }[sort]
    order = column.desc().nulls_last() if descending else column.asc().nulls_last()
    # The id breaks ties, so a page boundary never repeats or skips a row.
    tiebreak = rows.c.id.desc() if descending else rows.c.id.asc()
    page = await session.execute(query.order_by(order, tiebreak).limit(limit).offset(offset))
    return list(page.all()), total or 0


async def account(session: AsyncSession, user_id: int) -> Row[Any] | None:
    """One account's row, deleted or not: None for an id never had or erased."""
    rows = accounts()
    return (await session.execute(select(rows).where(rows.c.id == user_id))).one_or_none()


# --- one user's page --------------------------------------------------------------------


async def subscriptions(session: AsyncSession, user_id: int) -> list[Row[Any]]:
    """Every subscription the account has had, newest first."""
    plan, held = aliased(Plan), aliased(Plan)
    rows = await session.execute(
        select(
            Subscription.id,
            plan.slug.label("plan"),
            Subscription.status,
            held.slug.label("grandfathered_plan"),
            Subscription.current_period_end,
            Subscription.cancel_at,
            Subscription.past_due_since,
            Subscription.grace_ends_at,
            Subscription.paddle_subscription_id,
            Subscription.paddle_customer_id,
            Subscription.created_at,
        )
        .join(plan, plan.id == Subscription.plan_id)
        .outerjoin(held, held.id == Subscription.grandfathered_plan_id)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
    )
    return list(rows.all())


async def overrides(session: AsyncSession, user_id: int) -> list[Row[Any]]:
    """Every override the account has been given, newest first, and by whom."""
    creator, ender = aliased(AdminUser), aliased(AdminUser)
    now = func.now()
    rows = await session.execute(
        select(
            PlanOverride.id,
            Plan.slug.label("plan"),
            PlanOverride.reason,
            PlanOverride.starts_at,
            PlanOverride.expires_at,
            PlanOverride.ended_at,
            creator.email.label("created_by"),
            ender.email.label("ended_by"),
            and_(
                PlanOverride.ended_at.is_(None),
                PlanOverride.starts_at <= now,
                PlanOverride.expires_at > now,
            ).label("live"),
        )
        .join(Plan, Plan.id == PlanOverride.plan_id)
        .join(creator, creator.id == PlanOverride.created_by)
        .outerjoin(ender, ender.id == PlanOverride.ended_by)
        .where(PlanOverride.user_id == user_id)
        .order_by(PlanOverride.id.desc())
    )
    return list(rows.all())


@dataclass(frozen=True, slots=True)
class Counts:
    """What the account holds and has done, beyond its monthly quotas."""

    saved_filters: int
    watchlist_items: int
    alert_rules_on: int
    live_api_keys: int
    api_key_last_used_at: datetime | None
    data_exports: int
    last_data_export: date | None
    active_days_30: int


async def counts(session: AsyncSession, user_id: int, today: date) -> Counts:
    """What the account holds and has done: one round trip."""
    exports = (
        select(
            func.coalesce(func.sum(UserActivityDay.data_exports), 0).label("total"),
            func.max(UserActivityDay.day).filter(UserActivityDay.data_exports > 0).label("last"),
            func.count()
            .filter(UserActivityDay.day > today - timedelta(days=30))
            .label("active_30"),
        )
        .where(UserActivityDay.user_id == user_id)
        .subquery()
    )
    keys = (
        select(
            func.count().filter(ApiKey.revoked_at.is_(None)).label("live"),
            func.max(ApiKey.last_used_at).label("last_used_at"),
        )
        .where(ApiKey.user_id == user_id)
        .subquery()
    )
    row = (
        await session.execute(
            select(
                select(func.count())
                .select_from(SavedFilter)
                .where(SavedFilter.user_id == user_id)
                .scalar_subquery(),
                select(func.count())
                .select_from(WatchlistItem)
                .where(WatchlistItem.user_id == user_id)
                .scalar_subquery(),
                select(func.count())
                .select_from(AlertRule)
                .where(AlertRule.user_id == user_id, AlertRule.is_active)
                .scalar_subquery(),
                keys.c.live,
                keys.c.last_used_at,
                exports.c.total,
                exports.c.last,
                exports.c.active_30,
            )
            # One row each: joined on nothing, said so.
            .select_from(keys.join(exports, true()))
        )
    ).one()
    return Counts(
        saved_filters=row[0],
        watchlist_items=row[1],
        alert_rules_on=row[2],
        live_api_keys=row[3],
        api_key_last_used_at=row[4],
        data_exports=int(row[5]),
        last_data_export=row[6],
        active_days_30=row[7],
    )


async def usage_months(
    session: AsyncSession, user_id: int, *, months: int = 12
) -> list[UsageCounter]:
    """The account's last ``months`` quota months of each metered thing, newest first."""
    return list(
        await session.scalars(
            select(UsageCounter)
            .where(UsageCounter.user_id == user_id)
            .order_by(UsageCounter.period.desc(), UsageCounter.key)
            .limit(months * 2)
        )
    )


@dataclass(frozen=True, slots=True)
class Subject:
    """An account looked at by an admin: what :func:`app.accounts.entitlements.limit`
    and :func:`app.api.metering.usage_of` read, as they read a viewer."""

    user: AppUser
    grant: Grant | None
