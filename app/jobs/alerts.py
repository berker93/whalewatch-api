"""The alerts jobs: the events in the pairs a rebuild touched, told to the rules
that ask; and, every minute, what is waiting to be emailed, sent when it may.

``emit-alerts`` is :func:`emit_alerts` with a summary, and the ``emit_alerts``
task is it in ``asyncio.run``, sent by every rebuild once it commits
(:mod:`app.alerts.dispatch`). It writes the bell's notifications and queues
the emails; it sends none. ``send-alert-emails`` and its task, which beat
sends every minute, are :func:`send_alert_emails`: batches, the daily cap,
quiet hours and the digest (:mod:`app.alerts.outbox`).

Neither is a ``@tracked_task``, like ``ping``. A lock would skip a run while
another holds it, and a skipped emission is events nobody is told about: two
emissions at once are safe instead, since each event is written once by its
dedupe key, and two sends at once skip each other's users
(``SKIP LOCKED``). And a backfill sends an emission per filing, and beat a
send every minute, so a row apiece would bury the runs worth reading in
``ingestion_run``. What a task's run did is its log line. The CLI verbs, which
a person runs, record a row of their own around it, as ``detect-splits`` does.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from app.accounts import entitlements
from app.accounts.delivery import courier_scope
from app.alerts.events import Event, read_events
from app.alerts.firing import Told, fire, ring
from app.alerts.outbox import Decided, decide_due, send_due
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import create_redis
from app.db.models import EmailStatus
from app.db.session import session_scope
from app.derived.scope import Pair

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Emitted:
    """What one emission read and wrote."""

    events: list[Event]
    told: list[Told]
    """The notifications written by this run: none for an event told before."""
    queued: int
    """The outbox items queued: notifications to email, filings for digests."""


async def emit_alerts(settings: Settings, *, pairs: Collection[Pair] | None) -> Emitted:
    """Read the events in ``pairs``, or in every recent pair when ``None``,
    write a notification for each rule that hears one and queue its email,
    and queue the season's filings for the digests that count them. Once that
    has committed, the bell counts again.

    When there is something to tell, the plan catalog is loaded first, as the
    API's lifespan loads it: which kinds a rule's owner is sent is its plan's.
    """
    now = datetime.now(UTC)
    async with session_scope(settings) as session:
        events = await read_events(session, None if pairs is None else frozenset(pairs), now=now)
        if events:
            entitlements.install(await entitlements.load_catalog(session))
        fired = await fire(session, events)
    if fired.told:
        redis = create_redis(settings)
        try:
            await ring(fired.told, redis=redis)
        finally:
            await redis.aclose()
    emitted = Emitted(events=events, told=fired.told, queued=fired.queued)
    logger.info(
        "alerts.emitted",
        pairs=None if pairs is None else len(pairs),
        events=len(events),
        notifications=len(fired.told),
        queued=fired.queued,
    )
    return emitted


@dataclass(frozen=True, slots=True)
class Sent:
    """What one send decided, and what the courier said of each email."""

    decided: Decided
    emails: list[EmailStatus | None]

    @property
    def sent(self) -> int:
        return sum(status is EmailStatus.SENT for status in self.emails)


async def send_alert_emails(settings: Settings, *, now: datetime | None = None) -> Sent:
    """Decide what of what is waiting goes now, user by user, and send it.

    :param now: The moment to decide at; the clock's when None. Only a test,
        or a person checking what a given evening would send, passes one.
    """
    now = now or datetime.now(UTC)
    async with session_scope(settings) as session:
        entitlements.install(await entitlements.load_catalog(session))
        decided = await decide_due(session, settings, now=now)
        emails: list[EmailStatus | None] = []
        if decided.due or decided.rang:
            redis = create_redis(settings)
            try:
                async with courier_scope(settings) as courier:
                    emails = await send_due(session, decided, courier=courier, redis=redis)
            finally:
                await redis.aclose()
    sent = Sent(decided=decided, emails=emails)
    logger.info(
        "alerts.sent",
        users=decided.users,
        batches=decided.batches,
        digests=decided.digests,
        overflowed=decided.overflowed,
        dropped=decided.dropped,
        emails=len(emails),
        emails_sent=sent.sent,
    )
    return sent
