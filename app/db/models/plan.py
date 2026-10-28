"""Plans, what each allows, and who is on which: entitlements as rows.

A limit lives in a row, not in an ``if``, so changing what a plan allows is
an UPDATE, not a deploy (:mod:`app.accounts.entitlements` reads them).

``plan``
    One thing a user can be on: Free, Pro, an API plan, or a private plan
    that holds terms promised to someone (``is_public`` false). The slug is
    what code and the session name it by. ``free`` is the plan of everyone
    with no live subscription, signed in or not.
``plan_entitlement``
    One capability of one plan: a key from :data:`ENTITLEMENT_KEYS` and its
    value. Every plan has a row for every key; the catalog refuses a set of
    rows that misses one.
``subscription``
    A user on a plan. At most one live (:data:`LIVE_STATUSES`) per user.
    ``grandfathered_plan_id`` names a plan whose terms the user keeps while
    ``plan_id`` changes price or limits for everyone else: a launch promise is
    a private plan and this column, not a copy of the plan table.
``subscription_entitlement``
    One subscription's own value for a key, above both plans: a comp, a
    raised limit for one customer.
``plan_override``
    A plan given to one user by an admin, with a reason and an expiry: for
    support, for friends. Above the subscription, and needing none: Paddle
    knows nothing of it, so it never collides with a payment.
``paddle_event``
    Every Paddle webhook event, once, by its ``event_id``: what makes a
    redelivered event a no-op (:mod:`app.accounts.billing`).

A value is JSON. A limit (:data:`LIMIT_KEYS`) is a whole number of at least
0, or null for no limit; a flag (:data:`FLAG_KEYS`) is true or false. The
check on each table holds rows to that, so an UPDATE that would break a
reader is refused by Postgres rather than discovered by a request.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: Entitlements that are a number of something, null for no limit.
LIMIT_KEYS: Final = (
    "history_quarters",
    "insider_months",
    "watchlist_items",
    "screener_saved_filters",
    "alert_rules",
    "api_calls_month",
    "mcp_calls_month",
)

#: Entitlements that are on or off.
FLAG_KEYS: Final = (
    "export_enabled",
    "alerts_instant",
    "mcp_enabled",
)

ENTITLEMENT_KEYS: Final = LIMIT_KEYS + FLAG_KEYS


def _quoted(keys: tuple[str, ...]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


KEY_CHECK: Final = f"key IN ({_quoted(ENTITLEMENT_KEYS)})"

#: A flag is a boolean; a limit is null or a whole number of at least 0. A
#: CASE, so the cast to numeric only ever sees a number.
VALUE_CHECK: Final = (
    f"CASE WHEN key IN ({_quoted(FLAG_KEYS)}) THEN jsonb_typeof(value) = 'boolean' "
    "WHEN jsonb_typeof(value) = 'null' THEN true "
    "WHEN jsonb_typeof(value) = 'number' "
    "THEN value::numeric >= 0 AND value::numeric = trunc(value::numeric) "
    "ELSE false END"
)


class PlanInterval(StrEnum):
    """How often a paid plan bills."""

    MONTH = "month"
    YEAR = "year"


INTERVAL_CHECK: Final = "interval IN ({})".format(", ".join(f"'{i.value}'" for i in PlanInterval))


class Plan(Base):
    """One plan: its name and price. What it allows is in :class:`PlanEntitlement`."""

    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    slug: Mapped[str] = mapped_column(Text, unique=True)
    """Stable, for a program to branch on, and what a session names it by."""

    name: Mapped[str] = mapped_column(Text)
    """For a person."""

    price_cents: Mapped[int] = mapped_column(Integer)
    """Per :attr:`interval`, in :attr:`currency`'s smallest unit. 0 for Free."""

    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'USD'"))

    interval: Mapped[str | None] = mapped_column(Text)
    """One :class:`PlanInterval` value. Null for a plan that costs nothing."""

    is_public: Mapped[bool] = mapped_column(Boolean, server_default=false())
    """Offered on the pricing page. A plan held only by grandfathered users is not."""

    snapshot_of_plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plan.id", ondelete="RESTRICT")
    )
    """The plan this is a copy of, as its terms stood at :attr:`snapshot_at`:
    what grandfathering pins a subscription to (:mod:`app.admin.plans`). A
    subscription moved to another plan stops being grandfathered to a snapshot
    of this one (:mod:`app.accounts.billing`)."""

    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z][a-z0-9_]*$'", name="slug_is_a_slug"),
        CheckConstraint("price_cents >= 0", name="price_is_not_negative"),
        CheckConstraint(INTERVAL_CHECK, name="interval_is_known"),
        CheckConstraint("price_cents = 0 OR interval IS NOT NULL", name="paid_has_an_interval"),
        CheckConstraint(
            "(snapshot_of_plan_id IS NULL) = (snapshot_at IS NULL)", name="snapshot_says_when"
        ),
        CheckConstraint("snapshot_of_plan_id IS NULL OR NOT is_public", name="snapshot_is_private"),
    )


class PlanEntitlement(Base):
    """What one plan allows of one capability."""

    __tablename__ = "plan_entitlement"

    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plan.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    """One of :data:`ENTITLEMENT_KEYS`."""
    value: Mapped[Any] = mapped_column(JSONB)
    """JSON: a whole number or null for a limit, a boolean for a flag."""

    __table_args__ = (
        CheckConstraint(KEY_CHECK, name="key_is_known"),
        CheckConstraint(VALUE_CHECK, name="value_fits_key"),
    )


class SubscriptionStatus(StrEnum):
    """Where a subscription is, in the words the payment provider uses."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    """A payment failed and is being retried: the plan holds meanwhile."""
    PAUSED = "paused"
    UNPAID = "unpaid"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"


#: The statuses a subscription grants its plan in. Any other is Free.
LIVE_STATUSES: Final = (
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
)

SUBSCRIPTION_STATUS_CHECK: Final = "status IN ({})".format(
    ", ".join(f"'{s.value}'" for s in SubscriptionStatus)
)
LIVE_CHECK: Final = "status IN ({})".format(", ".join(f"'{s.value}'" for s in LIVE_STATUSES))


class DunningStep(StrEnum):
    """The last email a failed renewal's dunning sent (:mod:`app.accounts.billing`)."""

    PAYMENT_FAILED = "payment_failed"
    GRACE_ENDING = "grace_ending"
    DOWNGRADED = "downgraded"


DUNNING_STEP_CHECK: Final = "dunning_notified IS NULL OR dunning_notified IN ({})".format(
    ", ".join(f"'{s.value}'" for s in DunningStep)
)


class Subscription(Base):
    """One user on one plan, for as long as its status is live.

    A paid one mirrors a Paddle subscription: its status, plan and period are
    whatever Paddle's last webhook said, never what a checkout redirect did.
    One made by hand (a comp) has no Paddle ids.
    """

    __tablename__ = "subscription"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("plan.id", ondelete="RESTRICT"))
    """What the user is on: what they are billed for and what the account page names."""

    status: Mapped[str] = mapped_column(Text)
    """One :class:`SubscriptionStatus` value."""

    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the period paid for ends. Null for a subscription nobody pays for."""

    cancel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When it ends. From then on the user is on Free, whatever the status says."""

    grandfathered_plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plan.id", ondelete="RESTRICT")
    )
    """A plan whose terms the user keeps, above :attr:`plan_id`'s."""

    paddle_subscription_id: Mapped[str | None] = mapped_column(Text, unique=True)
    """``sub_...``: the Paddle subscription this mirrors. Null for one made by hand."""

    paddle_customer_id: Mapped[str | None] = mapped_column(Text)
    """``ctm_...``: who pays, in Paddle. What a customer portal session is opened for."""

    paddle_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """``occurred_at`` of the last Paddle event applied: an older one arriving
    later changes nothing."""

    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When a renewal's payment first failed, for as long as it stays unpaid."""

    grace_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When an unpaid renewal stops granting its plan: from then on, Free,
    whatever the status says. Set with :attr:`past_due_since`."""

    dunning_notified: Mapped[str | None] = mapped_column(Text)
    """One :class:`DunningStep` value: the last dunning email sent."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(SUBSCRIPTION_STATUS_CHECK, name="status_is_known"),
        CheckConstraint(DUNNING_STEP_CHECK, name="dunning_step_is_known"),
        CheckConstraint(
            "(past_due_since IS NULL) = (grace_ends_at IS NULL)", name="grace_has_a_start"
        ),
        Index("ix_subscription_user_id", "user_id"),
        Index("ix_subscription_paddle_customer_id", "paddle_customer_id"),
        # One live subscription per user: resolving a user's plan finds one or none.
        Index(
            "uq_subscription_user_live",
            "user_id",
            unique=True,
            postgresql_where=text(LIVE_CHECK),
        ),
    )


class SubscriptionEntitlement(Base):
    """One subscription's own value for one key, above its plans'."""

    __tablename__ = "subscription_entitlement"

    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscription.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    """Why: for whoever finds it later."""

    __table_args__ = (
        CheckConstraint(KEY_CHECK, name="key_is_known"),
        CheckConstraint(VALUE_CHECK, name="value_fits_key"),
    )


#: The shortest reason an override may give.
MIN_OVERRIDE_REASON: Final = 3


class PlanOverride(Base):
    """One user given one plan by an admin, from :attr:`starts_at` until the
    sooner of :attr:`expires_at` and :attr:`ended_at`.

    At most one is live per user: setting one ends the one before
    (:mod:`app.admin.plans`). An override that ran out stays, as the history
    of what the user was given and why.
    """

    __tablename__ = "plan_override"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("plan.id", ondelete="RESTRICT"))
    """The plan whose terms the user has meanwhile, whole, above their subscription's."""

    reason: Mapped[str] = mapped_column(Text)
    """Why, for whoever finds it later: "friend of the founders", "ticket 412"."""

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When it ends on its own. Every override has one: a comp nobody remembers
    giving is a comp nobody ends."""

    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When an admin ended it early, or replaced it with another."""

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )

    ended_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            f"reason = btrim(reason) AND length(reason) >= {MIN_OVERRIDE_REASON}",
            name="reason_is_given",
        ),
        CheckConstraint("expires_at > starts_at", name="expires_after_it_starts"),
        CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ending_says_who"),
        Index("ix_plan_override_user_id", "user_id", "id"),
    )


class PaddleEventOutcome(StrEnum):
    """What a webhook event did."""

    APPLIED = "applied"
    STALE = "stale"
    """Older than what its subscription already reflects: changed nothing."""
    IGNORED = "ignored"
    """A type not acted on, or a failed payment for no subscription of ours."""
    UNMATCHED = "unmatched"
    """A subscription no account could be found for: one not bought through
    our checkout. Logged as an error, for a person to look at."""


PADDLE_EVENT_OUTCOME_CHECK: Final = "outcome IN ({})".format(
    ", ".join(f"'{o.value}'" for o in PaddleEventOutcome)
)


class PaddleEvent(Base):
    """One Paddle webhook event, received once. No payload: nothing personal."""

    __tablename__ = "paddle_event"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    """``evt_...``: the same on every delivery of one event."""

    event_type: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entity_id: Mapped[str | None] = mapped_column(Text, index=True)
    """The ``sub_...`` or ``txn_...`` it was about."""

    outcome: Mapped[str] = mapped_column(Text)
    """One :class:`PaddleEventOutcome` value."""

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (CheckConstraint(PADDLE_EVENT_OUTCOME_CHECK, name="outcome_is_known"),)
