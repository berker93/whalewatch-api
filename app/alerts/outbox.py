"""When what is waiting in ``alert_outbox`` is emailed: batches, the daily cap,
quiet hours and the evening digest.

Every minute, beat sends ``send_alert_emails``, and for each user with
something waiting this module decides what goes now (:func:`decide`), writes
it down, commits, and then emails it. The bell is not held back by any of
this: a notification is in it from the moment the emission writes it. Only
its email waits.

The rules, each in the user's own zone (``quiet_time_zone``)
------------------------------------------------------------
**Batches.** A notification of a rule that emails waits for the rest of its
window: :data:`BATCH_WINDOW` from the first one waiting, then everything
waiting goes as one email. A rule with a two-thousand-position manager is one
email an evening, not a few dozen.

**Quiet hours.** Nothing goes between ``quiet_start`` and ``quiet_end``, 22:00
to 08:00 unless the user says otherwise. What waited through them goes at
their end, as one email: its windows have long closed.

**The cap.** At most :data:`EMAILS_PER_DAY` alert emails in a user's day,
batches and digests together. A batch with no room left is not dropped: it
moves to the digest (``route`` becomes ``digest``), the overflow. While the
day's digest is still to come and has something to say, the last email of
the day is kept for it, so a third batch waits for the digest instead of
leaving it no room. A test alert (``POST /v1/me/alert-rules/{id}/test``) is
sent at once and not counted: the user asked for it.

**The digest.** Once a day, at :data:`DIGEST_AT`, everything on the digest
route queued before then goes, as one email: the season's filings, counted in
the subject (``7 of your investors filed today``), and the day's overflow.
Season items are only queued during a filing window, so the digest is silent
outside one (:mod:`app.alerts.firing`). When the digest's rule rings the bell
too, its filings are one notification there, ``season_digest:<day>``. At
:data:`DIGEST_AT` in quiet hours, or with the day's emails spent, it goes at
the first moment after that it may: the morning, as the next day's first.

Each item is checked again when it goes: its rule must still be on, of a kind
its owner's plan sends, and emailing (with a verified address) for anything
emailed. An item that may no longer go is settled without a delivery. The
courier checks the address and the list once more as it sends.

Two runs at once
----------------
Each user is decided in a transaction of their own, holding their
``app_user`` row ``FOR NO KEY UPDATE SKIP LOCKED``: a second run skips a user the
first is deciding, and finds nothing of theirs waiting once it commits. An
email is sent after the commit that records it, so a worker that dies between
the two loses that email rather than sending it twice: best effort, as every
email is.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import DateTime, delete, func, literal, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import mail
from app.accounts.alert_rules import sent_kinds
from app.accounts.delivery import Courier
from app.accounts.entitlements import Grant, grant_for
from app.accounts.mail import AlertItem, Message
from app.api.routers.notifications import forget_count
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.periods import in_filing_window
from app.db.models import (
    PAYLOAD_KEYS,
    AlertDelivery,
    AlertKind,
    AlertOutbox,
    AlertRule,
    AppUser,
    Channel,
    DeliveryKind,
    EmailStatus,
    Notification,
    Route,
)

logger = get_logger(__name__)

#: How long a batch waits for more, from the first notification waiting.
BATCH_WINDOW: Final = timedelta(minutes=15)

#: The most alert emails one user is sent in one of their days.
EMAILS_PER_DAY: Final = 3

#: When the digest goes, on the user's clock: an evening, after New York's
#: close and most of a US filing day, and before quiet hours.
DIGEST_AT: Final = time(19, 0)

#: How long a delivery, and an item dropped without one, are kept. Longer
#: than :data:`app.alerts.events.FRESH_FOR`, so no event can be emitted again
#: once the item that stops it being queued twice is gone.
KEEP_FOR: Final = timedelta(days=35)


# --- the clock ------------------------------------------------------------------


def is_quiet(clock: time, start: time, end: time) -> bool:
    """Whether ``clock`` is in quiet hours from ``start`` to ``end``, which run
    past midnight when ``start`` is the later. ``start == end`` is none."""
    if start == end:
        return False
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def last_digest_time(local_now: datetime) -> datetime:
    """The latest :data:`DIGEST_AT` at or before ``local_now``, a wall clock."""
    today = datetime.combine(local_now.date(), DIGEST_AT)
    return today if local_now >= today else today - timedelta(days=1)


def filed_when(days: set[date], today: date) -> str:
    """``today``, ``yesterday``, ``since Monday``: when the filings in a digest
    were filed, by the user's clock, said from ``today``."""
    if days == {today}:
        return "today"
    if days == {today - timedelta(days=1)}:
        return "yesterday"
    earliest = min(days)
    if earliest == today - timedelta(days=1):
        return "since yesterday"
    if today - earliest < timedelta(days=7):
        return f"since {earliest:%A}"
    return "recently"


def _plural(count: int, word: str) -> str:
    return f"{count} {word if count == 1 else word + 's'}"


def digest_subject(investors: int, when: str, overflow: int) -> str:
    """``7 of your investors filed today``, and the overflow counted after it;
    ``4 more alerts`` when there are no filings."""
    if not investors:
        return f"{_plural(overflow, 'more alert')}"
    subject = f"{investors} of your investors filed {when}"
    if overflow:
        subject += f", and {_plural(overflow, 'more alert')}"
    return subject


# --- deciding ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Waiting:
    """One outbox item, with what its rule is now."""

    id: int
    rule_id: int
    route: Route
    kind: AlertKind
    title: str
    body: str
    payload: dict[str, Any]
    queued_at: datetime
    local_queued_at: datetime
    """``queued_at`` on the user's clock."""
    local_filed_at: datetime | None
    """A season item's filing, on the user's clock."""
    rule_kind: AlertKind
    rule_active: bool
    rule_channels: frozenset[Channel]

    @property
    def season(self) -> bool:
        return self.rule_kind is AlertKind.SEASON_DIGEST


@dataclass(frozen=True, slots=True)
class Account:
    """What deciding needs of one user, read as their row is locked."""

    now: datetime
    local_now: datetime
    """``now`` on the user's clock: a wall clock, no zone."""
    quiet_start: time
    quiet_end: time
    verified: bool
    sent: frozenset[AlertKind]
    """The kinds the user's plan sends now."""
    emailed_today: int
    """Alert emails already sent in the user's day: the cap's count."""
    last_digest: datetime | None
    """When the last digest went, on the user's clock."""
    digest_emails: bool
    """Whether the user's season digest is on and emails a verified address:
    whether today's digest may need the day's last email."""
    waiting: Sequence[Waiting]


@dataclass(frozen=True, slots=True)
class Digest:
    filed: list[Waiting]
    """Season items: the filings."""
    overflow: list[Waiting]
    emailed: list[Waiting]
    """What the email lists: the overflow, and the filings when the digest emails."""
    rings: bool
    """Whether the filings are a notification in the bell."""
    at: datetime
    """The :data:`DIGEST_AT` it is the digest of, on the user's clock."""

    @property
    def items(self) -> list[Waiting]:
        return [*self.filed, *self.overflow]


@dataclass(slots=True)
class Decision:
    dropped: list[Waiting] = field(default_factory=list)
    overflowed: list[Waiting] = field(default_factory=list)
    """Instant items moved to the digest: the day's emails are spent."""
    batch: list[Waiting] = field(default_factory=list)
    digest: Digest | None = None


def _emails(item: Waiting, verified: bool) -> bool:
    return Channel.EMAIL in item.rule_channels and verified


def _may_go(item: Waiting, account: Account) -> bool:
    """Whether the item's rule still wants it, as it is now."""
    if not item.rule_active or item.rule_kind not in account.sent:
        return False
    if item.season:
        return Channel.IN_APP in item.rule_channels or _emails(item, account.verified)
    return _emails(item, account.verified)


def decide(account: Account) -> Decision:
    """What goes now of what ``account`` has waiting, by the module's rules."""
    decision = Decision()
    live: list[Waiting] = []
    for item in account.waiting:
        (live if _may_go(item, account) else decision.dropped).append(item)
    clock = account.local_now
    if is_quiet(clock.time(), account.quiet_start, account.quiet_end):
        return decision
    emailed = account.emailed_today

    # The digest first: what it takes, the batch cannot.
    due_at = last_digest_time(clock)
    done = account.last_digest is not None and account.last_digest >= due_at
    on_digest = [item for item in live if item.route is Route.DIGEST]
    due = [item for item in on_digest if item.local_queued_at <= due_at]
    if due and not done:
        filed = [item for item in due if item.season]
        overflow = [item for item in due if not item.season]
        emails_filed = bool(filed) and _emails(filed[0], account.verified)
        listed = [*(filed if emails_filed else []), *overflow]
        if not listed or emailed < EMAILS_PER_DAY:
            rings = bool(filed) and Channel.IN_APP in filed[0].rule_channels
            decision.digest = Digest(filed, overflow, listed, rings, due_at)
            emailed += bool(listed)

    instant = [item for item in live if item.route is Route.INSTANT]
    if instant and min(item.queued_at for item in instant) + BATCH_WINDOW <= account.now:
        # Today's digest is ahead and will have something to email (overflow
        # waiting, or the season, which it emails): the day's last email is its.
        taken = set() if decision.digest is None else {item.id for item in decision.digest.items}
        ahead = [item for item in on_digest if item.id not in taken]
        digest_ahead = clock.time() < DIGEST_AT and (
            any(not item.season or account.digest_emails for item in ahead)
            or (account.digest_emails and in_filing_window(clock.date()))
        )
        if emailed + digest_ahead < EMAILS_PER_DAY:
            decision.batch = instant
        else:
            decision.overflowed = instant
    return decision


# --- reading and writing ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    """What :func:`sent_kinds` reads off a viewer, for an account with none."""

    grant: Grant | None


@dataclass(frozen=True, slots=True)
class Due:
    """One email decided and recorded, to send once the decision has committed."""

    delivery_id: int
    message: Message


@dataclass(slots=True)
class Decided:
    """What one run decided, over every user."""

    users: int = 0
    due: list[Due] = field(default_factory=list)
    batches: int = 0
    digests: int = 0
    overflowed: int = 0
    dropped: int = 0
    rang: list[int] = field(default_factory=list)
    """The users a digest put in the bell: their kept counts are stale."""


async def _account(
    session: AsyncSession, user_id: int, now: datetime
) -> tuple[Account, str] | None:
    """The user's :class:`Account` and address, their row locked; None when
    another run holds it or the account is deleted."""
    at = literal(now, DateTime(timezone=True))
    user = (
        await session.execute(
            select(
                AppUser.email,
                AppUser.email_verified_at,
                AppUser.quiet_start,
                AppUser.quiet_end,
                func.timezone(AppUser.quiet_time_zone, at).label("local_now"),
                AppUser.quiet_time_zone,
            )
            .where(AppUser.id == user_id, AppUser.deleted_at.is_(None))
            # NO KEY: an emission inserting this user's outbox rows takes a
            # key share on the row for their foreign key, and must not wait.
            .with_for_update(key_share=True, skip_locked=True)
        )
    ).one_or_none()
    if user is None:
        return None
    zone = user.quiet_time_zone
    local_now: datetime = user.local_now
    rows = await session.execute(
        select(
            AlertOutbox.id,
            AlertOutbox.rule_id,
            AlertOutbox.route,
            AlertOutbox.kind,
            AlertOutbox.title,
            AlertOutbox.body,
            AlertOutbox.payload,
            AlertOutbox.queued_at,
            func.timezone(zone, AlertOutbox.queued_at).label("local_queued_at"),
            func.timezone(zone, AlertOutbox.filed_at).label("local_filed_at"),
            AlertRule.kind.label("rule_kind"),
            AlertRule.is_active,
            AlertRule.channels,
        )
        .join(AlertRule, AlertRule.id == AlertOutbox.rule_id)
        .where(AlertOutbox.user_id == user_id, AlertOutbox.settled_at.is_(None))
        .order_by(AlertOutbox.id.desc())
    )
    waiting = [
        Waiting(
            id=row.id,
            rule_id=row.rule_id,
            route=Route(row.route),
            kind=AlertKind(row.kind),
            title=row.title,
            body=row.body,
            payload=row.payload,
            queued_at=row.queued_at,
            local_queued_at=row.local_queued_at,
            local_filed_at=row.local_filed_at,
            rule_kind=AlertKind(row.rule_kind),
            rule_active=row.is_active,
            rule_channels=frozenset(Channel(channel) for channel in row.channels),
        )
        for row in rows
    ]
    day_start = datetime.combine(local_now.date(), time.min)
    emailed_today = await session.scalar(
        select(func.count())
        .select_from(AlertDelivery)
        .where(
            AlertDelivery.user_id == user_id,
            AlertDelivery.emailed.is_(true()),
            AlertDelivery.local_at >= day_start,
            AlertDelivery.local_at < day_start + timedelta(days=1),
        )
    )
    last_digest = await session.scalar(
        select(func.max(AlertDelivery.local_at)).where(
            AlertDelivery.user_id == user_id,
            AlertDelivery.kind == DeliveryKind.DIGEST.value,
        )
    )
    digest_emails = await session.scalar(
        select(func.count())
        .select_from(AlertRule)
        .where(
            AlertRule.user_id == user_id,
            AlertRule.kind == AlertKind.SEASON_DIGEST.value,
            AlertRule.is_active.is_(true()),
            AlertRule.channels.contains([Channel.EMAIL.value]),
        )
    )
    verified = user.email_verified_at is not None
    account = Account(
        now=now,
        local_now=local_now,
        quiet_start=user.quiet_start,
        quiet_end=user.quiet_end,
        verified=verified,
        sent=sent_kinds(_Plan(await grant_for(session, user_id))),
        emailed_today=emailed_today or 0,
        last_digest=last_digest,
        digest_emails=bool(digest_emails) and verified,
        waiting=waiting,
    )
    return account, user.email


def _item(waiting: Waiting) -> AlertItem:
    return AlertItem(
        rule_id=waiting.rule_id,
        kind=waiting.kind,
        title=waiting.title,
        body=waiting.body,
        payload=waiting.payload,
    )


def _period(filed: Sequence[Waiting]) -> str | None:
    """The quarter most of the filings are for: the latest, on a tie."""
    periods = Counter(item.payload.get("period") for item in filed if item.payload.get("period"))
    if not periods:
        return None
    return max(periods, key=lambda period: (periods[period], period))


def _digest_title(digest: Digest, today: date) -> str:
    investors = {item.payload.get("filer_slug") for item in digest.filed}
    days = {item.local_filed_at.date() for item in digest.filed if item.local_filed_at}
    when = filed_when(days, today) if days else "today"
    return digest_subject(len(investors), when, len(digest.overflow))


def _digest_body(digest: Digest) -> str:
    """``Berkshire Hathaway, Pershing Square and 5 more filed.``"""
    names = sorted(
        {str(item.payload.get("filer_name") or item.payload["filer_slug"]) for item in digest.filed}
    )
    shown = names[:3]
    if len(names) > len(shown):
        shown.append(f"{len(names) - len(shown)} more")
    said = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
    return f"{said} filed their 13F."


async def _ring(
    session: AsyncSession, user_id: int, digest: Digest, title: str, period: str | None
) -> int | None:
    """The digest's notification in the bell: the filings, counted. None when
    there is one for that evening already."""
    payload: dict[str, Any] = dict.fromkeys(PAYLOAD_KEYS) | {
        "period": period,
        "as_of": digest.at.date().isoformat(),
        "investors": sorted({str(item.payload.get("filer_slug")) for item in digest.filed}),
    }
    notification_id: int | None = await session.scalar(
        insert(Notification)
        .values(
            user_id=user_id,
            kind=AlertKind.SEASON_DIGEST.value,
            title=title,
            body=_digest_body(digest),
            payload=payload,
            dedupe_key=f"{AlertKind.SEASON_DIGEST}:{digest.at.date().isoformat()}",
        )
        .on_conflict_do_nothing(constraint="uq_notification_user_id_dedupe_key")
        .returning(Notification.id)
    )
    return notification_id


async def _deliver(
    session: AsyncSession,
    user_id: int,
    kind: DeliveryKind,
    account: Account,
    items: Sequence[Waiting],
    *,
    emailed: bool,
    notification_id: int | None = None,
) -> int:
    delivery_id = await session.scalar(
        insert(AlertDelivery)
        .values(
            user_id=user_id,
            kind=kind.value,
            local_at=account.local_now,
            items=len(items),
            emailed=emailed,
            notification_id=notification_id,
        )
        .returning(AlertDelivery.id)
    )
    assert delivery_id is not None
    await _settle(session, items, account.now, delivery_id)
    return delivery_id


async def _settle(
    session: AsyncSession, items: Sequence[Waiting], now: datetime, delivery_id: int | None
) -> None:
    if items:
        await session.execute(
            update(AlertOutbox)
            .where(AlertOutbox.id.in_([item.id for item in items]))
            .values(settled_at=now, delivery_id=delivery_id)
        )


async def _decide_for(
    session: AsyncSession, settings: Settings, user_id: int, now: datetime, decided: Decided
) -> None:
    """Decide for one user and write it down, in the session's transaction."""
    found = await _account(session, user_id, now)
    if found is None:
        return
    account, address = found
    decision = decide(account)
    decided.users += 1
    await _settle(session, decision.dropped, now, None)
    decided.dropped += len(decision.dropped)
    if decision.overflowed:
        await session.execute(
            update(AlertOutbox)
            .where(AlertOutbox.id.in_([item.id for item in decision.overflowed]))
            .values(route=Route.DIGEST.value)
        )
        decided.overflowed += len(decision.overflowed)

    if (digest := decision.digest) is not None:
        title = _digest_title(digest, account.local_now.date())
        period = _period(digest.filed)
        rang = await _ring(session, user_id, digest, title, period) if digest.rings else None
        if rang is not None:
            decided.rang.append(user_id)
        delivery_id = await _deliver(
            session,
            user_id,
            DeliveryKind.DIGEST,
            account,
            digest.items,
            emailed=bool(digest.emailed),
            notification_id=rang,
        )
        decided.digests += 1
        if digest.emailed:
            listed = {item.id for item in digest.emailed}
            decided.due.append(
                Due(
                    delivery_id,
                    mail.alert_digest(
                        settings,
                        to=address,
                        user_id=user_id,
                        subject=title,
                        filed=[_item(item) for item in digest.filed if item.id in listed],
                        overflow=[_item(item) for item in digest.overflow],
                        period=period,
                    ),
                )
            )

    if decision.batch:
        delivery_id = await _deliver(
            session, user_id, DeliveryKind.BATCH, account, decision.batch, emailed=True
        )
        decided.batches += 1
        decided.due.append(
            Due(
                delivery_id,
                mail.alert_batch(
                    settings,
                    to=address,
                    user_id=user_id,
                    items=[_item(item) for item in decision.batch],
                ),
            )
        )


async def decide_due(session: AsyncSession, settings: Settings, *, now: datetime) -> Decided:
    """Decide, for every user with something waiting, what goes now, and
    commit each user's decision on its own. The emails to send, which nothing
    has sent yet. Forgets what is past :data:`KEEP_FOR` too."""
    users = await session.scalars(
        select(AlertOutbox.user_id)
        .where(AlertOutbox.settled_at.is_(None))
        .distinct()
        .order_by(AlertOutbox.user_id)
    )
    decided = Decided()
    for user_id in users.all():
        try:
            await _decide_for(session, settings, user_id, now, decided)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    await _forget(session, now)
    await session.commit()
    return decided


async def _forget(session: AsyncSession, now: datetime) -> None:
    """Deliveries past :data:`KEEP_FOR`, with their items, and items dropped as long ago."""
    before = now - KEEP_FOR
    await session.execute(delete(AlertDelivery).where(AlertDelivery.sent_at < before))
    await session.execute(
        delete(AlertOutbox).where(
            AlertOutbox.delivery_id.is_(None), AlertOutbox.settled_at < before
        )
    )


async def send_due(
    session: AsyncSession, decided: Decided, *, courier: Courier, redis: Redis
) -> list[EmailStatus | None]:
    """After the decisions have committed: the bell counts dropped where a
    digest rang it, and each email sent, its delivery told how it went."""
    for user_id in sorted(set(decided.rang)):
        await forget_count(redis, user_id)
    statuses = []
    for due in decided.due:
        status = await courier.send(due.message)
        statuses.append(status)
        if status is not None:
            await session.execute(
                update(AlertDelivery)
                .where(AlertDelivery.id == due.delivery_id)
                .values(email_status=status.value)
            )
            await session.commit()
    return statuses
