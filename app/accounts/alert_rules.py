"""Alert rules: which kinds a plan sends, how many rules it keeps on, and the presets.

What a rule is lives in :mod:`app.db.models.alert`; the routes that edit one
in :mod:`app.api.routers.alert_rules`. This is what both they and the
watchlist need to agree on.

What a plan sends
-----------------
Free is sent the season digest and nothing else; a plan with
``alerts_instant`` is sent every kind, as it happens (:func:`sent_kinds`). A
rule of a kind the plan does not send can be kept, off or on, as one kept
from a plan since left is, but cannot be made or turned on: that is a 402.

How many
--------
``alert_rules`` is how many rules a plan keeps on at once, counting the ones
of a kind it sends (:func:`rules_on`): a rule turned off costs nothing, and
neither does one the plan would not send, so a Pro account that drops to
Free can still turn its digest on. Past :data:`MAX_ALERT_RULES` rules, on or
off, which no plan lifts, nothing more is made.

Presets
-------
What one click turns on (:data:`INVESTOR_PRESETS`, :data:`STOCK_PRESETS`),
and what following an investor turns on by itself (:func:`on_follow`): a
filing, by email too, and a position change of at least
:data:`LARGE_CHANGE`, in the bell. Nothing else: following a stock or an
insider turns nothing on. On a plan that sends neither, following an
investor makes sure the season digest exists instead, once: one turned off is
left off. Unfollowing turns off every rule about what was unfollowed, and
keeps their settings for the next follow (:func:`on_unfollow`).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, func, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import entitlements
from app.accounts.entitlements import MAX_WATCHLIST_ITEMS, Entitled
from app.db.models import AlertKind, AlertRule, AppUser, Channel

#: The most rules any account keeps, on or off, whatever its plan says: every
#: kind about everything a full watchlist follows, and the digest.
MAX_ALERT_RULES: Final = 2 * MAX_WATCHLIST_ITEMS + 1

#: The kinds every plan sends.
EVERY_PLAN_KINDS: Final = frozenset({AlertKind.SEASON_DIGEST})

#: A position change worth telling by default: a trim of a few thousand
#: dollars is not news, a million is.
LARGE_CHANGE: Final = Decimal("1000000")

Target = dict[str, int]
"""The rule's target column and id: ``{"filer_id": 7}``, ``{"security_id":
12}``, or ``{}`` for the digest."""


@dataclass(frozen=True, slots=True)
class Preset:
    """A rule as one click makes it."""

    kind: AlertKind
    channels: tuple[Channel, ...]
    min_value: Decimal | None = None


#: "Alert me about this investor": the filing, and its large changes.
INVESTOR_PRESETS: Final = (
    Preset(AlertKind.FILER_FILED, (Channel.IN_APP, Channel.EMAIL)),
    Preset(AlertKind.FILER_POSITION_CHANGE, (Channel.IN_APP,), LARGE_CHANGE),
)

#: "Alert me about this stock": a large move in who owns it, and its insiders
#: buying together.
STOCK_PRESETS: Final = (
    Preset(AlertKind.STOCK_OWNERSHIP_CHANGE, (Channel.IN_APP,), LARGE_CHANGE),
    Preset(AlertKind.INSIDER_CLUSTER, (Channel.IN_APP,)),
)

#: The digest, which a mailbox is where people expect it.
DIGEST_PRESET: Final = Preset(AlertKind.SEASON_DIGEST, (Channel.IN_APP, Channel.EMAIL))


def sent_kinds(who: Entitled | None) -> frozenset[AlertKind]:
    """The kinds ``who``'s plan sends: every one with ``alerts_instant``, the
    digest without."""
    if entitlements.can(who, "alerts_instant"):
        return frozenset(AlertKind)
    return EVERY_PLAN_KINDS


def target_is(target: Target) -> list[ColumnElement[bool]]:
    """``alert_rule``'s conditions for a rule about ``target``: the digest's is
    both columns null."""
    filer, security = target.get("filer_id"), target.get("security_id")
    return [
        AlertRule.filer_id.is_(None) if filer is None else AlertRule.filer_id == filer,
        AlertRule.security_id.is_(None) if security is None else AlertRule.security_id == security,
    ]


async def lock(session: AsyncSession, user_id: int) -> None:
    """One change to the user's rules at a time, so two at once cannot both
    pass a count. The user's row rather than the rules': there may be none."""
    await session.execute(select(AppUser.id).where(AppUser.id == user_id).with_for_update())


async def rules_on(session: AsyncSession, user_id: int, kinds: frozenset[AlertKind]) -> int:
    """The user's rules that are on, of ``kinds``: what ``alert_rules`` counts."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(AlertRule)
            .where(
                AlertRule.user_id == user_id,
                AlertRule.is_active.is_(true()),
                AlertRule.kind.in_([str(kind) for kind in kinds]),
            )
        )
        or 0
    )


async def rules_kept(session: AsyncSession, user_id: int) -> int:
    """Every rule of the user's, on or off: what :data:`MAX_ALERT_RULES` bounds."""
    return (
        await session.scalar(
            select(func.count()).select_from(AlertRule).where(AlertRule.user_id == user_id)
        )
        or 0
    )


@dataclass(frozen=True, slots=True)
class Pending:
    """What turning presets on would do."""

    presets: list[Preset]
    """The ones whose rule is off or missing, in order."""
    missing: frozenset[AlertKind]
    """The kinds of those that have no rule yet."""

    @property
    def new(self) -> int:
        """How many rules turning them on makes."""
        return len(self.missing)


async def pending(
    session: AsyncSession,
    user_id: int,
    presets: tuple[Preset, ...],
    target: Target,
    *,
    reactivate: bool = True,
) -> Pending:
    """Which of ``presets`` need a rule turned on or made for ``target``.
    Without ``reactivate``, a rule that exists, off, is left as it is."""
    rows = await session.execute(
        select(AlertRule.kind, AlertRule.is_active).where(
            AlertRule.user_id == user_id,
            AlertRule.kind.in_([str(preset.kind) for preset in presets]),
            *target_is(target),
        )
    )
    found = {AlertKind(kind): active for kind, active in rows}
    todo = [
        preset
        for preset in presets
        if preset.kind not in found or (reactivate and not found[preset.kind])
    ]
    return Pending(todo, frozenset(preset.kind for preset in todo if preset.kind not in found))


async def write(session: AsyncSession, user_id: int, presets: list[Preset], target: Target) -> None:
    """Make each preset's rule for ``target``, or turn the one there is on,
    keeping its threshold and channels: they are the user's."""
    if not presets:
        return
    statement = insert(AlertRule).values(
        [
            {
                "user_id": user_id,
                "kind": str(preset.kind),
                "filer_id": target.get("filer_id"),
                "security_id": target.get("security_id"),
                "min_value": preset.min_value,
                "channels": [str(channel) for channel in preset.channels],
            }
            for preset in presets
        ]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "kind", "filer_id", "security_id"],
            set_={"is_active": True},
        )
    )


async def room(session: AsyncSession, who: Entitled | None, user_id: int) -> int | None:
    """How many more rules ``who`` may turn on: None for no limit."""
    allowed = entitlements.limit(who, "alert_rules")
    if allowed is None:
        return None
    return max(0, allowed - await rules_on(session, user_id, sent_kinds(who)))


async def on_follow(
    session: AsyncSession, who: Entitled | None, user_id: int, target: Target
) -> None:
    """Turn on what following ``target`` turns on (the module's Presets), as
    far as the plan has room: following is never refused for it. Does not
    commit."""
    if "filer_id" not in target:
        return
    presets: tuple[Preset, ...]
    if AlertKind.FILER_FILED in sent_kinds(who):
        presets, reactivate = INVESTOR_PRESETS, True
    else:
        presets, reactivate, target = (DIGEST_PRESET,), False, {}
    await lock(session, user_id)
    todo = await pending(session, user_id, presets, target, reactivate=reactivate)
    fits = todo.presets
    space = await room(session, who, user_id)
    if space is not None:
        fits = fits[:space]
    if todo.new and await rules_kept(session, user_id) + todo.new > MAX_ALERT_RULES:
        # Turning on what exists never passes the bound; making more might.
        fits = [preset for preset in fits if preset.kind not in todo.missing]
    await write(session, user_id, fits, target)


async def on_unfollow(session: AsyncSession, user_id: int, target: Target) -> None:
    """Turn off every rule about ``target``, keeping their settings. Does not commit."""
    if not target:
        return
    await session.execute(
        update(AlertRule)
        .where(AlertRule.user_id == user_id, AlertRule.is_active.is_(true()), *target_is(target))
        .values(is_active=False)
    )
