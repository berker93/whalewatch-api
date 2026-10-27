"""Subscriptions from Paddle's webhooks, and the dunning of a renewal that failed.

What a user is on is whatever Paddle's webhooks last said, never what a
checkout redirect claims: the page Paddle sends the buyer back to changes
nothing, and the account page asks ``GET /v1/billing`` until the webhook has
landed. :func:`handle` is the whole of it, inside the request's transaction.

Once, in order
--------------
Every event is recorded by its ``event_id`` (``paddle_event``) in the same
transaction as what it changes, so a redelivery finds its row and changes
nothing, and an event whose handling failed left no row and is handled again
when Paddle retries it. Paddle does not promise order: each subscription keeps
the ``occurred_at`` of the last event applied to it, and an older one arriving
later is recorded as ``stale`` and changes nothing. A subscription event
carries the whole subscription, so the newest one is the truth.

Events
------
Every ``subscription.*`` event (created, updated, activated, trialing,
past_due, paused, resumed, canceled, imported) applies the subscription it
carries: its plan, by the price of its item (``PADDLE_PRICES``); its status,
which Paddle names as :class:`~app.db.models.SubscriptionStatus` does; the end
of its period; and a scheduled cancellation's date as ``cancel_at``. A
subscription new to us belongs to the account in its ``custom_data.user_id``,
which the checkout put there, or else to the account that paid with its
customer before. One with neither is ``unmatched``, logged as an error and
answered 200: retrying would not find its owner.

``transaction.payment_failed`` for a renewal (origin ``subscription_recurring``)
starts the subscription's dunning. A failed first checkout has no subscription
yet and is ``ignored``: Paddle's checkout tells the buyer itself.

Dunning
-------
A renewal that fails, by its failed payment or by the subscription turning
``past_due``, whichever arrives first, keeps its plan for
``BILLING_GRACE_DAYS`` while Paddle retries the card:

1. at once, an email that the payment failed and when the grace ends;
2. ``BILLING_REMINDER_DAYS`` before it ends, a reminder (:func:`run_dunning`);
3. when it ends, the account is Free (the grant ends at ``grace_ends_at``,
   without waiting for a job) and an email says so (:func:`run_dunning`);
4. if a later retry, or a new card, pays it, the subscription turns
   ``active``, the plan is back, and an email says so.

Which email went last is ``dunning_notified``, claimed by a conditional
UPDATE, so each is sent at most once per failure. Paddle ends its own
retries by cancelling or pausing the subscription, which ends the dunning
with no email of ours: the downgrade one has gone by then.

Deleted accounts
----------------
Deleting an account cancels its Paddle subscription at once
(:func:`cancel_for_deleted`), right after the deletion commits: the deletion
never waits on Paddle. One Paddle could not be reached for is tried again by
the hourly ``billing`` job until it is cancelled, and the purge holds the
account back meanwhile (:func:`app.accounts.privacy.purge_due`), since
erasing the row would lose the only record of a subscription still billing.
Restoring the account does not bring the subscription back.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import mail
from app.accounts.mail import Message
from app.accounts.paddle import Notification, PaddleClient, PaddleError, parse_time
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import (
    LIVE_STATUSES,
    AppUser,
    DunningStep,
    PaddleEvent,
    PaddleEventOutcome,
    Plan,
    Subscription,
    SubscriptionStatus,
)

logger = get_logger(__name__)

#: The events that carry a whole subscription, each applied the same way.
SUBSCRIPTION_EVENTS: Final = frozenset(
    {
        "subscription.created",
        "subscription.updated",
        "subscription.activated",
        "subscription.trialing",
        "subscription.past_due",
        "subscription.paused",
        "subscription.resumed",
        "subscription.canceled",
        "subscription.imported",
    }
)
PAYMENT_FAILED: Final = "transaction.payment_failed"
#: A transaction's ``origin`` when it is a subscription's renewal.
RENEWAL_ORIGIN: Final = "subscription_recurring"

#: The statuses Paddle may still charge in: what a deleted account's
#: subscription must leave. Paused is one: it resumes by itself on a date.
BILLABLE_STATUSES: Final = (*LIVE_STATUSES, SubscriptionStatus.PAUSED)

#: The statuses a dunning ends in by the plan being paid for again.
PAID_STATUSES: Final = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)


class BillingError(Exception):
    """An event this API cannot apply as things are: a price no plan is sold
    at, or a second live subscription. Its transaction is rolled back and the
    webhook answered 500, so Paddle retries it once the cause is fixed."""


@dataclass
class Handled:
    """What one event did, and what to do once it is committed."""

    outcome: PaddleEventOutcome
    duplicate: bool = False
    users: set[int] = field(default_factory=set)
    """Whose sessions should read their grant again."""
    mail: list[Message] = field(default_factory=list)


def plan_for_price(settings: Settings, price_id: str) -> str | None:
    """The slug of the plan sold at ``price_id``, or None."""
    return next((slug for slug, price in settings.paddle_prices.items() if price == price_id), None)


async def handle(session: AsyncSession, settings: Settings, event: Notification) -> Handled:
    """Record ``event`` and apply it, in the caller's transaction, which the
    caller commits. A repeat is ``duplicate`` and changes nothing.

    :raises BillingError: when it cannot be applied; roll back.
    """
    claimed = await session.scalar(
        insert(PaddleEvent)
        .values(
            event_id=event.event_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            entity_id=event.entity_id,
            outcome=PaddleEventOutcome.IGNORED,
        )
        .on_conflict_do_nothing(index_elements=[PaddleEvent.event_id])
        .returning(PaddleEvent.event_id)
    )
    if claimed is None:
        logger.info("billing.duplicate_event", event_id=event.event_id, type=event.event_type)
        return Handled(PaddleEventOutcome.IGNORED, duplicate=True)

    if event.event_type in SUBSCRIPTION_EVENTS:
        handled = await _apply_subscription(session, settings, event)
    elif event.event_type == PAYMENT_FAILED:
        handled = await _apply_payment_failed(session, settings, event)
    else:
        handled = Handled(PaddleEventOutcome.IGNORED)

    await session.execute(
        update(PaddleEvent)
        .where(PaddleEvent.event_id == event.event_id)
        .values(outcome=handled.outcome)
    )
    logger.info(
        "billing.event",
        event_id=event.event_id,
        type=event.event_type,
        entity=event.entity_id,
        outcome=handled.outcome.value,
    )
    return handled


# --- subscription events ---------------------------------------------------------------


async def _locked(session: AsyncSession, paddle_id: str) -> Subscription | None:
    """The row mirroring ``paddle_id``, locked until commit: two events about
    one subscription are applied one after the other."""
    row: Subscription | None = await session.scalar(
        select(Subscription)
        .where(Subscription.paddle_subscription_id == paddle_id)
        .with_for_update()
    )
    return row


async def _owner(session: AsyncSession, data: Mapping[str, Any]) -> int | None:
    """The account a subscription new to us belongs to: ``custom_data.user_id``,
    else whoever paid as its customer before."""
    custom = data.get("custom_data") or {}
    try:
        user_id = int(str(custom.get("user_id")))
    except ValueError:
        user_id = None
    if user_id is not None and await session.get(AppUser, user_id) is not None:
        return user_id
    customer = data.get("customer_id")
    if not customer:
        return None
    paid_before: int | None = await session.scalar(
        select(Subscription.user_id)
        .where(Subscription.paddle_customer_id == customer)
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    return paid_before


def _price_ids(data: Mapping[str, Any]) -> list[str]:
    return [
        price_id
        for item in data.get("items") or []
        if (price_id := (item.get("price") or {}).get("id"))
    ]


async def _apply_subscription(
    session: AsyncSession, settings: Settings, event: Notification
) -> Handled:
    data = event.data
    paddle_id = event.entity_id
    if paddle_id is None:
        raise BillingError(f"{event.event_type} {event.event_id} names no subscription")
    row = await _locked(session, paddle_id)
    if (
        row is not None
        and row.paddle_event_at is not None
        and event.occurred_at < row.paddle_event_at
    ):
        return Handled(PaddleEventOutcome.STALE)

    try:
        status = SubscriptionStatus(str(data.get("status")))
    except ValueError:
        raise BillingError(f"subscription {paddle_id} has status {data.get('status')!r}") from None
    prices = _price_ids(data)
    slug = next(filter(None, (plan_for_price(settings, price) for price in prices)), None)
    if slug is None:
        raise BillingError(
            f"subscription {paddle_id} is for {prices or 'no price'}, which PADDLE_PRICES "
            "sells no plan at"
        )
    plan = await session.scalar(select(Plan).where(Plan.slug == slug))
    if plan is None:
        raise BillingError(f"PADDLE_PRICES names plan {slug!r}, which does not exist")

    if row is None:
        user_id = await _owner(session, data)
        if user_id is None:
            logger.error(
                "billing.unmatched_subscription",
                event_id=event.event_id,
                subscription=paddle_id,
                customer=data.get("customer_id"),
            )
            return Handled(PaddleEventOutcome.UNMATCHED)
        row = Subscription(user_id=user_id, paddle_subscription_id=paddle_id)
        session.add(row)

    if status in LIVE_STATUSES:
        other = await session.scalar(
            select(Subscription.id).where(
                Subscription.user_id == row.user_id,
                Subscription.status.in_(LIVE_STATUSES),
                or_(
                    Subscription.paddle_subscription_id.is_(None),
                    Subscription.paddle_subscription_id != paddle_id,
                ),
            )
        )
        if other is not None:
            # Checkout refuses this, so it is a purchase made some other way, or
            # a comp given while a checkout was open: a person decides which
            # goes (and refunds), then Paddle's retry applies this one.
            raise BillingError(
                f"user {row.user_id} already has live subscription {other}; "
                f"{paddle_id} cannot be live beside it"
            )

    period = data.get("current_billing_period") or {}
    scheduled = data.get("scheduled_change") or {}
    new = row.id is None
    previous = row.status
    if row.grandfathered_plan_id is not None and row.plan_id != plan.id:
        await _end_snapshot(session, row, plan)
    row.plan_id = plan.id
    row.status = status
    row.paddle_customer_id = data.get("customer_id") or row.paddle_customer_id
    row.current_period_end = parse_time(period.get("ends_at"))
    row.cancel_at = (
        parse_time(scheduled.get("effective_at")) if scheduled.get("action") == "cancel" else None
    )
    row.paddle_event_at = event.occurred_at
    if new:
        # Its quota months start when it began, not when the row was written.
        started = parse_time(data.get("started_at")) or parse_time(data.get("created_at"))
        if started is not None:
            row.created_at = started

    handled = Handled(PaddleEventOutcome.APPLIED, users={row.user_id})
    user = await session.get(AppUser, row.user_id)
    if status is SubscriptionStatus.PAST_DUE:
        if row.past_due_since is None:
            handled.mail += _start_dunning(settings, row, user, plan, event.occurred_at)
    elif status in PAID_STATUSES:
        if row.past_due_since is not None and previous == SubscriptionStatus.PAST_DUE:
            if row.dunning_notified is not None and _mailable(user):
                assert user is not None
                handled.mail.append(mail.payment_recovered(settings, to=user.email, plan=plan.name))
            _end_dunning(row)
    else:
        _end_dunning(row)
    await session.flush()
    return handled


async def _end_snapshot(session: AsyncSession, row: Subscription, plan: Plan) -> None:
    """Stop grandfathering ``row`` to a snapshot of a plan it is no longer on.

    A snapshot pins the terms of the plan the user had (:mod:`app.admin.plans`).
    Kept after an upgrade, it would win whole over the plan just bought. A
    grandfathered plan that is not a snapshot, a launch promise made by hand,
    is the promise whatever the plan, and stays.
    """
    snapshot_of = await session.scalar(
        select(Plan.snapshot_of_plan_id).where(Plan.id == row.grandfathered_plan_id)
    )
    if snapshot_of is not None and snapshot_of != plan.id:
        logger.info(
            "billing.grandfathering_ended",
            subscription=row.paddle_subscription_id,
            plan=plan.slug,
        )
        row.grandfathered_plan_id = None


# --- failed payments -------------------------------------------------------------------


async def _apply_payment_failed(
    session: AsyncSession, settings: Settings, event: Notification
) -> Handled:
    data = event.data
    paddle_id = data.get("subscription_id")
    if not paddle_id or data.get("origin") != RENEWAL_ORIGIN:
        return Handled(PaddleEventOutcome.IGNORED)
    row = await _locked(session, paddle_id)
    if row is None:
        logger.warning("billing.payment_failed_unknown", subscription=paddle_id)
        return Handled(PaddleEventOutcome.UNMATCHED)
    # Older than the subscription's state as we have it: a renewal since
    # recovered, or one the past_due event has already started.
    if row.paddle_event_at is not None and event.occurred_at < row.paddle_event_at:
        return Handled(PaddleEventOutcome.STALE)
    handled = Handled(PaddleEventOutcome.APPLIED, users={row.user_id})
    if row.past_due_since is None and row.status in LIVE_STATUSES:
        plan = await session.get(Plan, row.plan_id)
        user = await session.get(AppUser, row.user_id)
        assert plan is not None
        handled.mail += _start_dunning(settings, row, user, plan, event.occurred_at)
        await session.flush()
    return handled


# --- dunning ---------------------------------------------------------------------------


def _mailable(user: AppUser | None) -> bool:
    return user is not None and user.deleted_at is None


def _start_dunning(
    settings: Settings, row: Subscription, user: AppUser | None, plan: Plan, at: datetime
) -> list[Message]:
    row.past_due_since = at
    row.grace_ends_at = at + timedelta(days=settings.billing_grace_days)
    if not _mailable(user):
        return []
    assert user is not None
    row.dunning_notified = DunningStep.PAYMENT_FAILED
    return [
        mail.payment_failed(
            settings, to=user.email, plan=plan.name, grace_ends_at=row.grace_ends_at
        )
    ]


def _end_dunning(row: Subscription) -> None:
    row.past_due_since = row.grace_ends_at = row.dunning_notified = None


@dataclass(frozen=True, slots=True)
class DunningRun:
    reminded: int
    downgraded: int
    mail: list[Message]


async def run_dunning(session: AsyncSession, settings: Settings, now: datetime) -> DunningRun:
    """Claim and write the reminders and downgrade notices due at ``now``, in
    the caller's transaction: send ``mail`` once it is committed."""
    remind_from = now + timedelta(days=settings.billing_reminder_days)
    in_dunning = and_(
        Subscription.grace_ends_at.is_not(None), Subscription.status.in_(LIVE_STATUSES)
    )
    reminded = (
        await session.execute(
            update(Subscription)
            .where(
                in_dunning,
                Subscription.grace_ends_at > now,
                Subscription.grace_ends_at <= remind_from,
                or_(
                    Subscription.dunning_notified.is_(None),
                    Subscription.dunning_notified == DunningStep.PAYMENT_FAILED,
                ),
            )
            .values(dunning_notified=DunningStep.GRACE_ENDING)
            .returning(Subscription.user_id, Subscription.plan_id, Subscription.grace_ends_at)
        )
    ).all()
    downgraded = (
        await session.execute(
            update(Subscription)
            .where(
                in_dunning,
                Subscription.grace_ends_at <= now,
                or_(
                    Subscription.dunning_notified.is_(None),
                    Subscription.dunning_notified != DunningStep.DOWNGRADED,
                ),
            )
            .values(dunning_notified=DunningStep.DOWNGRADED)
            .returning(Subscription.user_id, Subscription.plan_id, Subscription.grace_ends_at)
        )
    ).all()

    messages = []
    for rows, step in ((reminded, DunningStep.GRACE_ENDING), (downgraded, DunningStep.DOWNGRADED)):
        for user, plan, grace_ends_at in await _recipients(session, rows):
            if step is DunningStep.GRACE_ENDING:
                messages.append(
                    mail.grace_ending(
                        settings, to=user.email, plan=plan.name, grace_ends_at=grace_ends_at
                    )
                )
            else:
                messages.append(mail.downgraded(settings, to=user.email, plan=plan.name))
    return DunningRun(reminded=len(reminded), downgraded=len(downgraded), mail=messages)


async def _recipients(
    session: AsyncSession, rows: Sequence[Row[tuple[int, int, datetime | None]]]
) -> list[tuple[AppUser, Plan, datetime]]:
    found = []
    for user_id, plan_id, grace_ends_at in rows:
        user, plan = await session.get(AppUser, user_id), await session.get(Plan, plan_id)
        if _mailable(user) and plan is not None and grace_ends_at is not None:
            assert user is not None
            found.append((user, plan, grace_ends_at))
    return found


# --- deleted accounts ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cancelled:
    """What :func:`cancel_for_deleted` did."""

    plans: list[str]
    """The name of each plan whose subscription it cancelled."""
    failed: int
    """Subscriptions Paddle could not cancel now: tried again by the next run."""


async def cancel_for_deleted(
    session: AsyncSession, paddle: PaddleClient, *, user_id: int | None = None
) -> Cancelled:
    """Cancel every Paddle subscription of a deleted account that Paddle may
    still charge, or only ``user_id``'s. Does not commit: commit what it
    cancelled even when some failed.

    ``SKIP LOCKED``: a webhook or another run holding one is left to them.
    """
    query = (
        select(Subscription)
        .join(AppUser, AppUser.id == Subscription.user_id)
        .where(
            AppUser.deleted_at.is_not(None),
            Subscription.paddle_subscription_id.is_not(None),
            Subscription.status.in_(BILLABLE_STATUSES),
        )
        .order_by(Subscription.id)
        .with_for_update(of=Subscription, skip_locked=True)
    )
    if user_id is not None:
        query = query.where(Subscription.user_id == user_id)
    plans, failed = [], 0
    for row in (await session.scalars(query)).all():
        assert row.paddle_subscription_id is not None  # selected so
        try:
            entity = await paddle.cancel_subscription(row.paddle_subscription_id)
        except PaddleError as exc:
            failed += 1
            logger.error(
                "billing.cancel_failed",
                user_id=row.user_id,
                subscription=row.paddle_subscription_id,
                error=str(exc),
                status=exc.status,
                code=exc.code,
            )
            continue
        # As Paddle answered; the subscription.canceled webhook says the same
        # and is applied over it, being newer than anything applied before.
        row.status = str(entity.get("status") or SubscriptionStatus.CANCELED)
        row.cancel_at = None
        _end_dunning(row)
        plan = await session.get(Plan, row.plan_id)
        plans.append(plan.name if plan else "")
        logger.info(
            "billing.cancelled_for_deletion",
            user_id=row.user_id,
            subscription=row.paddle_subscription_id,
        )
    await session.flush()
    return Cancelled(plans=plans, failed=failed)


def still_billing(user_id: Any) -> Any:
    """Whether the user ``user_id`` (a column) has a Paddle subscription
    Paddle may still charge: what holds a deleted account back from its purge."""
    return (
        select(Subscription.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.paddle_subscription_id.is_not(None),
            Subscription.status.in_(BILLABLE_STATUSES),
        )
        .exists()
    )


# --- what an account page shows -------------------------------------------------------


async def subscription_of(session: AsyncSession, user_id: int) -> Subscription | None:
    """The user's live subscription, else their latest: what ``GET /v1/billing`` shows."""
    row: Subscription | None = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.status.in_(LIVE_STATUSES).desc(), Subscription.id.desc())
        .limit(1)
    )
    return row


async def customer_of(session: AsyncSession, user_id: int) -> Subscription | None:
    """The user's latest subscription paid through Paddle: whose customer a
    checkout reuses and a portal session opens."""
    row: Subscription | None = await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.paddle_customer_id.is_not(None))
        .order_by(Subscription.status.in_(LIVE_STATUSES).desc(), Subscription.id.desc())
        .limit(1)
    )
    return row
