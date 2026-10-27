"""What an account may do: its entitlements, kept as rows and resolved without the database.

Every check of what someone may do is :func:`limit` or :func:`can`, here, and
nowhere else: ``limit(viewer, "watchlist_items")``, ``can(viewer,
"export_enabled")``. What each plan allows is rows in ``plan_entitlement``
(:mod:`app.db.models.plan`), so changing a limit is an UPDATE, not a deploy.

Who
---
``viewer`` is the signed-in :class:`app.accounts.sessions.Viewer`, or None for
someone signed out. Signed out is Free: there is one way through, not one per
kind of visitor. So is a signed-in account with no live subscription and no
live override, one whose subscription's ``cancel_at`` has passed, and one
whose unpaid renewal's grace has (``grace_ends_at``, :mod:`app.accounts.billing`).

Resolving a key
---------------
For a user with a live subscription or a live override, the first of these
that has the key:

1. the override's plan: one an admin gave the user, until it expires
   (``plan_override``, :mod:`app.admin.plans`);
2. the subscription's own value (``subscription_entitlement``);
3. its grandfathered plan's, the terms promised to the user;
4. its plan's.

Every plan has every key, so an override's plan wins whole, and so does a
grandfathered plan's terms over the plan's; the later ones are there for a
plan that does not have a key, which the catalog does not allow today. An
override needs no subscription, and a subscription keeps running under one:
when the override ends, the subscription's terms are back.

No database on the way
----------------------
What every plan allows is :class:`Catalog`, one snapshot in the process,
loaded at startup and every ``entitlements_refresh_seconds`` after it
(:func:`keep_fresh`). An UPDATE reaches every request within that.

What one user has is :class:`Grant`: their plan, grandfathered plan and
overrides. It is read once and kept on the session in Redis, which the request
reads anyway. A session re-reads it from Postgres after :data:`GRANT_EVERY`,
so a subscription changed by hand reaches the user within that; code that
changes one calls :meth:`app.accounts.sessions.SessionStore.forget_grants`, so
the next request re-reads it.

The catalog refuses rows that would leave a key unresolved: a plan missing a
key, a value of the wrong kind (Postgres's check refuses those already), or
no Free. A refused reload keeps the catalog it had and logs; before any
catalog has loaded, :func:`limit` and :func:`can` raise
:class:`EntitlementsUnavailableError`, a 503, never a guess at what someone may do.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol, get_args

from sqlalchemy import ColumnElement, ScalarSelect, and_, func, or_, select, text, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.logging import get_logger
from app.db.models import (
    ENTITLEMENT_KEYS,
    FLAG_KEYS,
    LIMIT_KEYS,
    LIVE_STATUSES,
    AppUser,
    Plan,
    PlanEntitlement,
    PlanOverride,
    Subscription,
    SubscriptionEntitlement,
)

logger = get_logger(__name__)

LimitKey = Literal[
    "history_quarters",
    "insider_months",
    "watchlist_items",
    "screener_saved_filters",
    "api_calls_month",
    "mcp_calls_month",
]
"""An entitlement that is a number of something: :func:`limit`."""

FlagKey = Literal["export_enabled", "alerts_instant", "mcp_enabled"]
"""An entitlement that is on or off: :func:`can`."""

Key = LimitKey | FlagKey

Value = int | bool | None
"""A limit's whole number, or None for no limit; a flag's boolean."""

#: Who has no live subscription is on this plan, signed in or not.
FREE: Final = "free"

#: The most any watchlist holds, whatever its plan says: a plan of no limit
#: is this many. Also the bound on a request that names followed things
#: itself, signed in or not (the timeline's ``ref``).
MAX_WATCHLIST_ITEMS: Final = 500

#: The most screener filters anyone keeps, whatever its plan says: a plan of
#: no limit is this many.
MAX_SAVED_FILTERS: Final = 100

#: How long a session keeps its grant before reading it again.
GRANT_EVERY: Final = timedelta(minutes=5)

#: How soon to try again while no catalog has loaded.
FIRST_LOAD_RETRY_SECONDS: Final = 5.0

# The table's keys and the types' must be one list: a key added to one alone
# would be refused by the database or unreachable from code.
assert set(get_args(LimitKey)) == set(LIMIT_KEYS)
assert set(get_args(FlagKey)) == set(FLAG_KEYS)


class EntitlementsUnavailableError(Exception):
    """No catalog has loaded yet, so nobody can be told what they may do."""


class CatalogError(Exception):
    """The rows would leave some key unresolved for some plan."""


def value_problem(key: str, value: object) -> str | None:
    """What is wrong with ``value`` for ``key``, or None: the table's check, in Python."""
    if key in FLAG_KEYS:
        return None if isinstance(value, bool) else "must be true or false"
    if key in LIMIT_KEYS:
        if value is None or (type(value) is int and value >= 0):
            return None
        return "must be a whole number of at least 0, or null for no limit"
    return "is not an entitlement"


# --- the catalog ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanTerms:
    """One plan: its name, its price, and what it allows."""

    slug: str
    name: str
    price_cents: int
    currency: str
    interval: str | None
    is_public: bool
    values: Mapping[str, Value]
    """Every key's value."""


@dataclass(frozen=True, slots=True)
class Catalog:
    """Every plan, as the rows said when it was loaded."""

    plans: Mapping[str, PlanTerms]

    @property
    def free(self) -> PlanTerms:
        return self.plans[FREE]

    @classmethod
    def build(cls, plans: list[PlanTerms]) -> "Catalog":
        """The catalog of ``plans``, or :class:`CatalogError` naming every gap."""
        problems = []
        by_slug = {plan.slug: plan for plan in plans}
        if FREE not in by_slug:
            problems.append(f"there is no {FREE!r} plan, which everyone without one is on")
        for plan in plans:
            if missing := [key for key in ENTITLEMENT_KEYS if key not in plan.values]:
                problems.append(f"{plan.slug} has no {', '.join(missing)}")
            for key, value in plan.values.items():
                if problem := value_problem(key, value):
                    problems.append(f"{plan.slug}.{key} {problem}: {value!r}")
        if problems:
            raise CatalogError("; ".join(problems))
        return cls(plans=by_slug)


async def load_catalog(session: AsyncSession) -> Catalog:
    """Every plan and what it allows, from the tables.

    :raises CatalogError: when the rows leave a key unresolved.
    """
    plans = (await session.scalars(select(Plan).order_by(Plan.id))).all()
    rows = await session.execute(
        select(PlanEntitlement.plan_id, PlanEntitlement.key, PlanEntitlement.value)
    )
    values: dict[int, dict[str, Value]] = {plan.id: {} for plan in plans}
    for plan_id, key, value in rows:
        values[plan_id][key] = value
    return Catalog.build(
        [
            PlanTerms(
                slug=plan.slug,
                name=plan.name,
                price_cents=plan.price_cents,
                currency=plan.currency,
                interval=plan.interval,
                is_public=plan.is_public,
                values=values[plan.id],
            )
            for plan in plans
        ]
    )


_catalog: Catalog | None = None


def install(catalog: Catalog | None) -> None:
    """Make ``catalog`` the one every request resolves against. None: no catalog."""
    global _catalog
    _catalog = catalog


def current() -> Catalog:
    """The catalog in force.

    :raises EntitlementsUnavailableError: before one has loaded.
    """
    if _catalog is None:
        raise EntitlementsUnavailableError
    return _catalog


async def keep_fresh(factory: async_sessionmaker[AsyncSession], every: float) -> None:
    """Load the catalog, then again every ``every`` seconds, until cancelled.

    Until the first load succeeds it tries every few seconds: the app starts
    with Postgres down, as it does for everything else, and answers what needs
    a catalog 503 meanwhile. A reload that fails keeps the catalog it had.
    """
    while True:
        try:
            async with factory() as session:
                install(await load_catalog(session))
        except CatalogError as exc:
            logger.error("entitlements.catalog_refused", error=str(exc))
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("entitlements.catalog_unavailable", error=repr(exc))
        await asyncio.sleep(every if _catalog is not None else min(every, FIRST_LOAD_RETRY_SECONDS))


# --- one user's grant -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """What one user has above Free, by plan slug: their live subscription,
    their live override, or both. What a session keeps, so a request resolves
    a key without Postgres."""

    plan: str | None
    """The subscription's plan. None: no live subscription, only an override."""
    grandfathered: str | None
    overrides: Mapping[str, Value]
    """The subscription's own values, by key."""
    ends_at: datetime | None
    """The sooner of ``cancel_at`` and ``grace_ends_at``: from then on, the
    subscription grants nothing."""
    started_at: datetime | None = None
    """When the subscription began, or for a user with none the account: its
    anniversary is when its quota months start (:mod:`app.accounts.usage`).
    None only in a grant a session kept from before it was read, which the
    session reads again."""
    override: str | None = None
    """The plan an admin gave the user, whole, above the subscription's."""
    override_ends_at: datetime | None = None
    """When the override expires: from then on, the subscription's terms, or Free."""

    def subscribed(self, at: datetime) -> bool:
        return self.plan is not None and (self.ends_at is None or at < self.ends_at)

    def overridden(self, at: datetime) -> bool:
        return self.override is not None and (
            self.override_ends_at is None or at < self.override_ends_at
        )

    def live(self, at: datetime) -> bool:
        return self.subscribed(at) or self.overridden(at)

    def to_json(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "grandfathered": self.grandfathered,
            "overrides": dict(self.overrides),
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "override": self.override,
            "override_ends_at": self.override_ends_at.isoformat()
            if self.override_ends_at
            else None,
        }

    @classmethod
    def from_json(cls, stored: dict[str, Any]) -> "Grant":
        ends_at, started_at = stored["ends_at"], stored.get("started_at")
        override_ends_at = stored.get("override_ends_at")
        return cls(
            plan=stored["plan"],
            grandfathered=stored["grandfathered"],
            overrides=stored["overrides"],
            ends_at=datetime.fromisoformat(ends_at) if ends_at else None,
            started_at=datetime.fromisoformat(started_at) if started_at else None,
            override=stored.get("override"),
            override_ends_at=datetime.fromisoformat(override_ends_at) if override_ends_at else None,
        )


def subscription_overrides() -> ScalarSelect[Any]:
    """A subscription's own entitlements as one JSON object, ``{}`` for none:
    a column for a query selecting from ``subscription``."""
    return (
        select(
            func.coalesce(
                func.jsonb_object_agg(SubscriptionEntitlement.key, SubscriptionEntitlement.value),
                text("'{}'::jsonb"),
                type_=JSONB,
            )
        )
        .where(SubscriptionEntitlement.subscription_id == Subscription.id)
        .scalar_subquery()
    )


def subscription_is_live() -> ColumnElement[bool]:
    """A ``subscription`` row that grants its plan now: a live status, and
    neither its ``cancel_at`` nor its grace passed."""
    return and_(
        Subscription.status.in_(LIVE_STATUSES),
        or_(Subscription.cancel_at.is_(None), Subscription.cancel_at > func.now()),
        or_(Subscription.grace_ends_at.is_(None), Subscription.grace_ends_at > func.now()),
    )


def override_is_live() -> ColumnElement[bool]:
    """A ``plan_override`` row that grants its plan now: begun, not expired,
    not ended."""
    return and_(
        PlanOverride.ended_at.is_(None),
        PlanOverride.starts_at <= func.now(),
        PlanOverride.expires_at > func.now(),
    )


async def grant_for(session: AsyncSession, user_id: int) -> Grant | None:
    """The user's live subscription and live override as a :class:`Grant`, or
    None: Free. One query."""
    sub = (
        select(
            Subscription.plan_id,
            Subscription.grandfathered_plan_id,
            # LEAST ignores nulls: null only when neither is set.
            func.least(Subscription.cancel_at, Subscription.grace_ends_at).label("ends_at"),
            Subscription.created_at,
            subscription_overrides().label("overrides"),
        )
        .where(Subscription.user_id == AppUser.id, subscription_is_live())
        .lateral("sub")
    )
    override = (
        select(PlanOverride.plan_id, PlanOverride.expires_at)
        .where(PlanOverride.user_id == AppUser.id, override_is_live())
        # One is live at a time; the newest, should two ever overlap.
        .order_by(PlanOverride.id.desc())
        .limit(1)
        .lateral("override")
    )
    plan, grandfathered, overriding = aliased(Plan), aliased(Plan), aliased(Plan)
    row = (
        await session.execute(
            select(
                plan.slug,
                grandfathered.slug,
                sub.c.ends_at,
                func.coalesce(sub.c.created_at, AppUser.created_at),
                sub.c.overrides,
                overriding.slug,
                override.c.expires_at,
            )
            .select_from(AppUser)
            .outerjoin(sub, true())
            .outerjoin(plan, plan.id == sub.c.plan_id)
            .outerjoin(grandfathered, grandfathered.id == sub.c.grandfathered_plan_id)
            .outerjoin(override, true())
            .outerjoin(overriding, overriding.id == override.c.plan_id)
            .where(AppUser.id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    plan_slug, grandfathered_slug, ends_at, started_at, values, override_slug, expires_at = row
    if plan_slug is None and override_slug is None:
        return None
    return Grant(
        plan=plan_slug,
        grandfathered=grandfathered_slug,
        overrides=values or {},
        ends_at=ends_at,
        started_at=started_at,
        override=override_slug,
        override_ends_at=expires_at,
    )


# --- resolving ----------------------------------------------------------------------


class Entitled(Protocol):
    """Someone signed in: what :func:`limit` and :func:`can` read off a viewer."""

    @property
    def grant(self) -> Grant | None: ...


def live_grant(who: Entitled | None) -> Grant | None:
    """``who``'s grant while some part of it is live; None for Free."""
    grant = who.grant if who is not None else None
    if grant is None or not grant.live(datetime.now(UTC)):
        return None
    return grant


def _terms(catalog: Catalog, slug: str) -> PlanTerms | None:
    terms = catalog.plans.get(slug)
    if terms is None:
        # A plan made since the catalog loaded: passed over until the next load.
        logger.warning("entitlements.unknown_plan", plan=slug)
    return terms


def _resolve(catalog: Catalog, grant: Grant | None, key: str) -> Value:
    if grant is None:
        return catalog.free.values[key]
    at = datetime.now(UTC)
    subscribed = grant.subscribed(at)
    if grant.overridden(at):
        assert grant.override is not None
        terms = _terms(catalog, grant.override)
        if terms is not None and key in terms.values:
            return terms.values[key]
    if not subscribed:
        return catalog.free.values[key]
    if key in grant.overrides:
        return grant.overrides[key]
    for slug in (grant.grandfathered, grant.plan):
        if slug is None:
            continue
        terms = _terms(catalog, slug)
        if terms is not None and key in terms.values:
            return terms.values[key]
    return catalog.free.values[key]


def limit(who: Entitled | None, key: LimitKey) -> int | None:
    """How many of ``key`` ``who`` may have: None for no limit.

    :raises EntitlementsUnavailableError: before a catalog has loaded.
    """
    if key not in LIMIT_KEYS:
        raise ValueError(f"{key!r} is not a limit: ask can()")
    value = _resolve(current(), live_grant(who), key)
    assert value is None or type(value) is int
    return value


def can(who: Entitled | None, key: FlagKey) -> bool:
    """Whether ``who`` may do ``key``.

    :raises EntitlementsUnavailableError: before a catalog has loaded.
    """
    if key not in FLAG_KEYS:
        raise ValueError(f"{key!r} is not a flag: ask limit()")
    value = _resolve(current(), live_grant(who), key)
    assert isinstance(value, bool)
    return value


def entitlements(who: Entitled | None) -> dict[str, Value]:
    """Every key's value for ``who``: what ``GET /v1/me`` lists."""
    catalog, grant = current(), live_grant(who)
    return {key: _resolve(catalog, grant, key) for key in ENTITLEMENT_KEYS}


def lifting_plan(key: LimitKey, needed: int) -> PlanTerms | None:
    """The cheapest public paid plan whose ``key`` is at least ``needed``, or
    no limit: what a refusal offers. None when no plan goes that far.

    :raises EntitlementsUnavailableError: before a catalog has loaded.
    """
    if key not in LIMIT_KEYS:
        raise ValueError(f"{key!r} is not a limit")
    offers = []
    for plan in current().plans.values():
        value = plan.values[key]
        if plan.slug != FREE and plan.is_public and (value is None or value >= needed):
            offers.append(plan)
    return min(offers, key=lambda plan: (plan.price_cents, plan.slug), default=None)


def plan_of(who: Entitled | None) -> PlanTerms:
    """The plan ``who`` is on: an override's while it lasts, else what they
    are billed for, not the terms they keep. Free for anyone with neither."""
    catalog, grant = current(), live_grant(who)
    if grant is None:
        return catalog.free
    at = datetime.now(UTC)
    slug = grant.override if grant.overridden(at) else grant.plan if grant.subscribed(at) else None
    if slug is None:
        return catalog.free
    return catalog.plans.get(slug, catalog.free)
