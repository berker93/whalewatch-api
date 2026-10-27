"""What an admin changes about a user's plan: overrides and grandfathering.

Overrides
---------
An override gives one user one plan, whole, from now until its expiry: a
customer support has made a promise to, a friend. It needs a reason and an
expiry, always: a comp nobody remembers giving is a comp nobody ends. It sits
above the user's subscription (:mod:`app.accounts.entitlements`) and needs
none, so it never touches Paddle and never collides with a payment: a friend
on Pro can still buy Pro, and when the override ends their subscription's
terms are back. One is live at a time; setting another ends the one before.

Grandfathering
--------------
Pins a subscription to its plan's terms as they are today, so that raising
Pro's limits or lowering them later leaves this user where they were. One
call: the plan is copied into a private snapshot (``plan.snapshot_of_plan_id``)
and the subscription's ``grandfathered_plan_id`` names it. A snapshot whose
terms match today's is reused, so a hundred users grandfathered on one day
share one plan. It pins what the plan allows, not its price, which Paddle
keeps per subscription: a price rise is a new Paddle price that existing
subscriptions are not moved to. A subscription Paddle moves to another plan
stops being grandfathered (:mod:`app.accounts.billing`).

Every change here is logged (:mod:`app.admin.log`) in its own transaction.
The caller then has the user's sessions read their grant again
(:meth:`~app.accounts.sessions.SessionStore.forget_grants`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.sessions import now
from app.admin import log
from app.db.models import (
    LIVE_STATUSES,
    AdminActionKind,
    AppUser,
    Plan,
    PlanEntitlement,
    PlanOverride,
    Subscription,
)
from app.db.models.plan import MIN_OVERRIDE_REASON

#: Taken while a snapshot is looked for and made, so two admins
#: grandfathering at once make one.
SNAPSHOT_LOCK: Final = 0x77770041


class PlanChangeError(Exception):
    """A change the user's state does not allow. ``code`` is the API's."""

    code = "conflict"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownUserError(PlanChangeError):
    code = "not_found"


class UnknownPlanError(PlanChangeError):
    code = "unknown_plan"


class BadOverrideError(PlanChangeError):
    code = "validation_error"


class NoOverrideError(PlanChangeError):
    code = "not_found"


class NoSubscriptionError(PlanChangeError):
    code = "no_subscription"


class AlreadyGrandfatheredError(PlanChangeError):
    code = "already_grandfathered"


class NotGrandfatheredError(PlanChangeError):
    code = "not_found"


def normalise_reason(reason: str | None) -> str | None:
    reason = (reason or "").strip()
    return reason or None


async def _user(session: AsyncSession, user_id: int) -> AppUser:
    """The account, locked, so two admins' changes to it come one after the other."""
    user = await session.scalar(
        select(AppUser).where(AppUser.id == user_id, AppUser.deleted_at.is_(None)).with_for_update()
    )
    if user is None:
        raise UnknownUserError(f"No account {user_id}, or it is deleted.")
    return user


async def _plan(session: AsyncSession, slug: str) -> Plan:
    plan = await session.scalar(select(Plan).where(Plan.slug == slug))
    if plan is None:
        raise UnknownPlanError(f"No plan {slug!r}. GET /admin/plans lists them.")
    return plan


# --- overrides ----------------------------------------------------------------------


async def set_override(
    session: AsyncSession,
    *,
    user_id: int,
    plan: str,
    reason: str,
    expires_at: datetime,
    admin_id: int,
    request_id: str | None = None,
) -> tuple[PlanOverride, list[int]]:
    """Give the user ``plan`` until ``expires_at``, ending any override they
    have. The new override and the ids of those it ended. Does not commit.

    :raises PlanChangeError: no such live account or plan, no reason, or an
        expiry that is not in the future.
    """
    given = normalise_reason(reason)
    if given is None or len(given) < MIN_OVERRIDE_REASON:
        raise BadOverrideError(f"Give a reason of at least {MIN_OVERRIDE_REASON} characters.")
    if expires_at.tzinfo is None:
        raise BadOverrideError("expires_at needs a time zone: say Z for UTC.")
    if expires_at <= now():
        raise BadOverrideError("expires_at must be in the future.")
    await _user(session, user_id)
    terms = await _plan(session, plan)
    ended = await _end_live(session, user_id, admin_id)
    override = PlanOverride(
        user_id=user_id,
        plan_id=terms.id,
        reason=given,
        expires_at=expires_at,
        created_by=admin_id,
    )
    session.add(override)
    await session.flush()
    await log.record(
        session,
        AdminActionKind.OVERRIDE_SET,
        admin_id=admin_id,
        user_id=user_id,
        request_id=request_id,
        detail={
            "override_id": override.id,
            "plan": terms.slug,
            "reason": given,
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "replaced": ended,
        },
    )
    return override, ended


async def _end_live(session: AsyncSession, user_id: int, admin_id: int) -> list[int]:
    """End the user's override still to run, if any: the ids ended."""
    ended = await session.scalars(
        update(PlanOverride)
        .where(
            PlanOverride.user_id == user_id,
            PlanOverride.ended_at.is_(None),
            PlanOverride.expires_at > func.now(),
        )
        .values(ended_at=func.now(), ended_by=admin_id)
        .returning(PlanOverride.id)
    )
    return sorted(ended)


async def end_override(
    session: AsyncSession,
    *,
    user_id: int,
    admin_id: int,
    reason: str | None = None,
    request_id: str | None = None,
) -> list[int]:
    """End the user's override now, before its expiry: the ids ended. Does
    not commit.

    :raises PlanChangeError: no such live account, or no override running.
    """
    await _user(session, user_id)
    ended = await _end_live(session, user_id, admin_id)
    if not ended:
        raise NoOverrideError("The account has no override running.")
    await log.record(
        session,
        AdminActionKind.OVERRIDE_ENDED,
        admin_id=admin_id,
        user_id=user_id,
        request_id=request_id,
        detail={"override_ids": ended, "reason": normalise_reason(reason)},
    )
    return ended


# --- grandfathering ----------------------------------------------------------------


async def _live_subscription(session: AsyncSession, user_id: int) -> Subscription:
    row = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status.in_(LIVE_STATUSES))
        .with_for_update()
    )
    if row is None:
        raise NoSubscriptionError(
            "The account has no live subscription to grandfather: give it an override instead."
        )
    return row


async def _values(session: AsyncSession, plan_id: int) -> dict[str, Any]:
    rows = await session.execute(
        select(PlanEntitlement.key, PlanEntitlement.value).where(PlanEntitlement.plan_id == plan_id)
    )
    return dict(rows.tuples().all())


@dataclass(frozen=True, slots=True)
class Snapshot:
    plan: Plan
    created: bool
    """Made now, rather than an earlier one with the same terms reused."""


async def snapshot_of(session: AsyncSession, source: Plan) -> Snapshot:
    """A private copy of ``source``'s terms as they are now: an earlier
    snapshot of it with the same price and entitlements, or a new one,
    ``pro_20261027``. Does not commit."""
    await session.execute(select(func.pg_advisory_xact_lock(SNAPSHOT_LOCK)))
    values = await _values(session, source.id)
    earlier = await session.scalars(
        select(Plan)
        .where(
            Plan.snapshot_of_plan_id == source.id,
            Plan.price_cents == source.price_cents,
            Plan.currency == source.currency,
            Plan.interval.is_not_distinct_from(source.interval),
        )
        .order_by(Plan.id.desc())
    )
    for plan in earlier:
        if await _values(session, plan.id) == values:
            return Snapshot(plan=plan, created=False)

    taken_at = now()
    base = f"{source.slug}_{taken_at:%Y%m%d}"
    taken = set(await session.scalars(select(Plan.slug).where(Plan.slug.like(f"{base}%"))))
    slug = base
    suffix = 2
    while slug in taken:
        slug, suffix = f"{base}_{suffix}", suffix + 1
    plan = Plan(
        slug=slug,
        name=f"{source.name} (terms of {taken_at:%d %b %Y})",
        price_cents=source.price_cents,
        currency=source.currency,
        interval=source.interval,
        is_public=False,
        snapshot_of_plan_id=source.id,
        snapshot_at=taken_at,
    )
    session.add(plan)
    await session.flush()
    session.add_all(
        PlanEntitlement(plan_id=plan.id, key=key, value=value) for key, value in values.items()
    )
    await session.flush()
    return Snapshot(plan=plan, created=True)


async def grandfather(
    session: AsyncSession,
    *,
    user_id: int,
    admin_id: int,
    reason: str | None = None,
    request_id: str | None = None,
) -> tuple[Plan, Snapshot]:
    """Pin the user's subscription to a snapshot of its plan's terms today:
    the plan and the snapshot. Does not commit.

    :raises PlanChangeError: no such live account, no live subscription, or
        one grandfathered already: end that first.
    """
    await _user(session, user_id)
    subscription = await _live_subscription(session, user_id)
    if subscription.grandfathered_plan_id is not None:
        held = await session.get(Plan, subscription.grandfathered_plan_id)
        raise AlreadyGrandfatheredError(
            f"The subscription is grandfathered to {held.slug if held else '?'} already. "
            "End that first to grandfather it to today's terms."
        )
    source = await session.get(Plan, subscription.plan_id)
    assert source is not None
    snapshot = await snapshot_of(session, source)
    subscription.grandfathered_plan_id = snapshot.plan.id
    await log.record(
        session,
        AdminActionKind.GRANDFATHERED,
        admin_id=admin_id,
        user_id=user_id,
        request_id=request_id,
        detail={
            "subscription_id": subscription.id,
            "plan": source.slug,
            "snapshot": snapshot.plan.slug,
            "snapshot_created": snapshot.created,
            "reason": normalise_reason(reason),
        },
    )
    return source, snapshot


async def ungrandfather(
    session: AsyncSession,
    *,
    user_id: int,
    admin_id: int,
    reason: str | None = None,
    request_id: str | None = None,
) -> Plan:
    """Put the user's subscription back on its plan's current terms: the
    grandfathered plan it left. Does not commit.

    :raises PlanChangeError: no such live account, or no grandfathered live
        subscription.
    """
    await _user(session, user_id)
    subscription = await _live_subscription(session, user_id)
    if subscription.grandfathered_plan_id is None:
        raise NotGrandfatheredError("The subscription is not grandfathered.")
    held = await session.get(Plan, subscription.grandfathered_plan_id)
    assert held is not None
    subscription.grandfathered_plan_id = None
    await log.record(
        session,
        AdminActionKind.UNGRANDFATHERED,
        admin_id=admin_id,
        user_id=user_id,
        request_id=request_id,
        detail={
            "subscription_id": subscription.id,
            "snapshot": held.slug,
            "reason": normalise_reason(reason),
        },
    )
    return held
