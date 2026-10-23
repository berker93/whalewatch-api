"""Plans, and what each lets an account do: its entitlements.

There is one plan today, :data:`FREE`, and every account is on it. The point
of naming it now is that every check of what an account may do goes through
:func:`entitlements_for`, so a paid plan later is a new :class:`Plan` and a
column saying who is on it, not a hunt for limits written into routes.

An entitlement is the plan's allowance narrowed by the account's state: an
account whose email is not verified reads everything its plan does but saves
nothing (:attr:`Entitlements.save`).
"""

from dataclasses import dataclass
from typing import Final

from app.db.models import AppUser


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    """Stable, for a program to branch on."""
    name: str
    """For a person."""
    watchlist_max_items: int


FREE: Final = Plan(id="free", name="Free", watchlist_max_items=500)

PLANS: Final = (FREE,)

#: The longest watchlist any plan holds: the bound on a request that names
#: followed things itself, signed in or not (the timeline's ``ref``).
MAX_WATCHLIST_ITEMS: Final = max(plan.watchlist_max_items for plan in PLANS)


@dataclass(frozen=True, slots=True)
class Entitlements:
    save: bool
    """Whether the account may write anything of its own: follow and unfollow.
    False until its email is verified."""
    watchlist_max_items: int


def plan_for(user: AppUser) -> Plan:
    """The plan ``user`` is on. Everyone's is :data:`FREE` until there is another."""
    return FREE


def entitlements_for(user: AppUser) -> Entitlements:
    plan = plan_for(user)
    return Entitlements(
        save=user.email_verified_at is not None,
        watchlist_max_items=plan.watchlist_max_items,
    )
