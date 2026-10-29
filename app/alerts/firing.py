"""Events against rules: who is told what, written down, then emailed.

A rule hears an event when all of these hold, each checked here, at the time
the task runs, not when the rule was made:

* it is on, its kind is the event's and its target is the event's filer or
  stock;
* its owner's account is not deleted, and its plan sends the kind
  (:func:`app.accounts.alert_rules.sent_kinds`): a rule kept from a plan
  since left stays quiet;
* it was made before the filing behind the event was filed, so that following
  an investor today does not deliver what they filed last week, and
  ``recompute --all`` does not deliver years of it;
* the event's dollars reach its ``min_value``, when it has one.

A change heard by a user's rule about the filer and their rule about the
stock is told once, as the filer's. And a filing is told in at most
:data:`MAX_CHANGES_PER_FILING` changes per user, the largest: a rule with no
threshold on a manager of two thousand positions is otherwise two thousand
notifications in one evening.

Writing is ``INSERT ... ON CONFLICT DO NOTHING`` on the dedupe key, and only
the rows written now are emailed. A second emission of an event already told
writes nothing and sends nothing; two emissions at once write it once, and
the one that wrote it sends it. Emailing comes after the commit (:func:`deliver`),
through the courier, which checks the address and the rule's list once more
as it sends. A worker that dies between the two has told the bell and not the
mailbox: the email is best effort, as every email is.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import and_, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import mail
from app.accounts.alert_rules import sent_kinds
from app.accounts.delivery import Courier
from app.accounts.entitlements import Grant, grant_for
from app.alerts.events import Event
from app.api.routers.notifications import forget_count
from app.core.config import Settings
from app.db.models import (
    FILER_KINDS,
    AlertKind,
    AlertRule,
    AppUser,
    Channel,
    EmailStatus,
    Notification,
)

#: The most changes from one filing one user is told about, the largest first.
MAX_CHANGES_PER_FILING: Final = 10


@dataclass(frozen=True, slots=True)
class _Rule:
    id: int
    user_id: int
    kind: AlertKind
    target: int
    min_value: Decimal | None
    emails: bool
    created_at: datetime
    email: str


@dataclass(frozen=True, slots=True)
class Told:
    """One notification this emission wrote: what the bell shows, and the email's
    address when the rule emails."""

    user_id: int
    rule_id: int
    notification_id: int
    kind: AlertKind
    title: str
    body: str
    payload: dict[str, Any]
    email: str | None


@dataclass(frozen=True, slots=True)
class _Account:
    """What :func:`sent_kinds` reads off a viewer, for an account with none."""

    grant: Grant | None


async def fire(session: AsyncSession, events: list[Event]) -> list[Told]:
    """Write a notification for every rule that hears an event, once per user
    and event. The notifications written now, not those written before. Does
    not commit."""
    if not events:
        return []
    rules = await _rules(session, events)
    if not rules:
        return []
    heard = await _heard(session, events, rules)
    return await _write(session, heard)


async def _rules(session: AsyncSession, events: list[Event]) -> dict[tuple[str, int], list[_Rule]]:
    """The rules on about the events' filers and stocks, by kind and target."""
    filers = sorted({event.filer_id for event in events})
    securities = sorted({event.security_id for event in events if event.security_id is not None})
    rows = await session.execute(
        select(
            AlertRule.id,
            AlertRule.user_id,
            AlertRule.kind,
            AlertRule.filer_id,
            AlertRule.security_id,
            AlertRule.min_value,
            AlertRule.channels,
            AlertRule.created_at,
            AppUser.email,
            AppUser.email_verified_at,
        )
        .join(AppUser, AppUser.id == AlertRule.user_id)
        .where(
            AlertRule.is_active.is_(true()),
            AppUser.deleted_at.is_(None),
            or_(
                and_(
                    AlertRule.kind.in_([str(kind) for kind in FILER_KINDS]),
                    AlertRule.filer_id.in_(filers),
                ),
                and_(
                    AlertRule.kind == AlertKind.STOCK_OWNERSHIP_CHANGE.value,
                    AlertRule.security_id.in_(securities),
                ),
            ),
        )
    )
    found: defaultdict[tuple[str, int], list[_Rule]] = defaultdict(list)
    for row in rows:
        kind = AlertKind(row.kind)
        target = row.filer_id if kind in FILER_KINDS else row.security_id
        found[(kind, target)].append(
            _Rule(
                id=row.id,
                user_id=row.user_id,
                kind=kind,
                target=target,
                min_value=row.min_value,
                # An address nobody has shown is theirs is told nothing.
                emails=Channel.EMAIL in row.channels and row.email_verified_at is not None,
                created_at=row.created_at,
                email=row.email,
            )
        )
    return found


async def _heard(
    session: AsyncSession, events: list[Event], rules: dict[tuple[str, int], list[_Rule]]
) -> list[tuple[_Rule, Event]]:
    """Each event with each rule that hears it, by the module's conditions."""
    kinds: dict[int, frozenset[AlertKind]] = {}
    heard: list[tuple[_Rule, Event]] = []
    for event in events:
        target = event.filer_id if event.kind in FILER_KINDS else event.security_id
        if target is None:
            continue
        for rule in rules.get((event.kind, target), ()):
            if rule.created_at > event.filed_at:
                continue
            if rule.min_value is not None and (
                event.dollars is None or event.dollars < rule.min_value
            ):
                continue
            if rule.user_id not in kinds:
                kinds[rule.user_id] = sent_kinds(_Account(await grant_for(session, rule.user_id)))
            if event.kind not in kinds[rule.user_id]:
                continue
            heard.append((rule, event))
    return _once_each(heard)


def _once_each(heard: list[tuple[_Rule, Event]]) -> list[tuple[_Rule, Event]]:
    """At most :data:`MAX_CHANGES_PER_FILING` of a filing's changes per user,
    and a change told about the filer not told again about the stock."""
    by_filing: defaultdict[tuple[int, Any], list[tuple[_Rule, Event]]] = defaultdict(list)
    kept: list[tuple[_Rule, Event]] = []
    for rule, event in heard:
        if event.kind is AlertKind.FILER_POSITION_CHANGE and event.change is not None:
            by_filing[(rule.user_id, event.change[:2])].append((rule, event))
        else:
            kept.append((rule, event))
    told: set[tuple[int, Any]] = set()
    for changes in by_filing.values():
        changes.sort(key=lambda heard: (-(heard[1].dollars or 0), heard[1].dedupe_key))
        for rule, event in changes[:MAX_CHANGES_PER_FILING]:
            kept.append((rule, event))
            told.add((rule.user_id, event.change))
    return [
        (rule, event)
        for rule, event in kept
        if not (
            event.kind is AlertKind.STOCK_OWNERSHIP_CHANGE and (rule.user_id, event.change) in told
        )
    ]


async def _write(session: AsyncSession, heard: list[tuple[_Rule, Event]]) -> list[Told]:
    """The notifications, those already there left as they are. The ones written."""
    if not heard:
        return []
    # In the order the bell should read them, newest first: each filing,
    # then its changes, the largest first.
    heard = sorted(heard, key=lambda one: _feed_order(one[1]))
    by_key = {(rule.user_id, event.dedupe_key): (rule, event) for rule, event in heard}
    written = await session.execute(
        insert(Notification)
        .values(
            [
                {
                    "user_id": rule.user_id,
                    "kind": str(event.kind),
                    "title": event.title,
                    "body": event.body,
                    "payload": event.payload,
                    "dedupe_key": event.dedupe_key,
                }
                for rule, event in by_key.values()
            ]
        )
        .on_conflict_do_nothing(constraint="uq_notification_user_id_dedupe_key")
        .returning(Notification.id, Notification.user_id, Notification.dedupe_key)
    )
    told = []
    for notification_id, user_id, dedupe_key in written.tuples():
        rule, event = by_key[(user_id, dedupe_key)]
        told.append(
            Told(
                user_id=user_id,
                rule_id=rule.id,
                notification_id=notification_id,
                kind=event.kind,
                title=event.title,
                body=event.body,
                payload=event.payload,
                email=rule.email if rule.emails else None,
            )
        )
    return sorted(told, key=lambda one: one.notification_id)


def _feed_order(event: Event) -> tuple[Any, ...]:
    """Written in this order, so read newest first, a filing comes before its
    changes, and a larger change before a smaller."""
    return (
        event.period,
        event.filer_id,
        event.kind is AlertKind.FILER_FILED,
        event.dollars or 0,
        event.dedupe_key,
    )


async def deliver(
    settings: Settings, told: Iterable[Told], *, courier: Courier, redis: Redis
) -> list[EmailStatus | None]:
    """After the commit: each user's kept unread count dropped, so the bell
    counts again, and each email sent. What the courier said of each email."""
    told = list(told)
    for user_id in sorted({one.user_id for one in told}):
        await forget_count(redis, user_id)
    statuses = []
    for one in told:
        if one.email is None:
            continue
        statuses.append(
            await courier.send(
                mail.alert(
                    settings,
                    to=one.email,
                    user_id=one.user_id,
                    rule_id=one.rule_id,
                    kind=one.kind,
                    title=one.title,
                    body=one.body,
                    payload=one.payload,
                )
            )
        )
    return statuses
