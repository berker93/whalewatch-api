"""The alerts job: the events in the pairs a rebuild touched, told to the rules that ask.

``emit-alerts`` is :func:`emit_alerts` with a summary, and the ``emit_alerts``
task is it in ``asyncio.run``, sent by every rebuild once it commits
(:mod:`app.alerts.dispatch`).

Not a ``@tracked_task``, like ``ping``, for two reasons. Its lock would skip
a run while another holds it, and a skipped emission is events nobody is
told about: two emissions at once are safe instead, since each event is
written once by its dedupe key. And a backfill sends one per filing, so a row
apiece would bury the runs worth reading in ``ingestion_run``. What a task's
run did is its log line, ``alerts.emitted``. The CLI verb, which a person
runs, records a row of its own around it, as ``detect-splits`` does.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from app.accounts import entitlements
from app.accounts.delivery import courier_scope
from app.alerts.events import Event, read_events
from app.alerts.firing import Told, deliver, fire
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import create_redis
from app.db.models import EmailStatus
from app.db.session import session_scope
from app.derived.scope import Pair

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Emitted:
    """What one run read and wrote."""

    events: list[Event]
    told: list[Told]
    """The notifications written by this run: none for an event told before."""
    emails: list[EmailStatus | None]

    @property
    def sent(self) -> int:
        return sum(status is EmailStatus.SENT for status in self.emails)


async def emit_alerts(settings: Settings, *, pairs: Collection[Pair] | None) -> Emitted:
    """Read the events in ``pairs``, or in every recent pair when ``None``,
    write a notification for each rule that hears one, and once that has
    committed, email the ones whose rule emails.

    When there is something to tell, the plan catalog is loaded first, as the
    API's lifespan loads it: which kinds a rule's owner is sent is its plan's.
    """
    now = datetime.now(UTC)
    async with session_scope(settings) as session:
        events = await read_events(session, None if pairs is None else frozenset(pairs), now=now)
        if events:
            entitlements.install(await entitlements.load_catalog(session))
        told = await fire(session, events)
    emails: list[EmailStatus | None] = []
    if told:
        redis = create_redis(settings)
        try:
            async with courier_scope(settings) as courier:
                emails = await deliver(settings, told, courier=courier, redis=redis)
        finally:
            await redis.aclose()
    emitted = Emitted(events=events, told=told, emails=emails)
    logger.info(
        "alerts.emitted",
        pairs=None if pairs is None else len(pairs),
        events=len(events),
        notifications=len(told),
        emails=len(emails),
        emails_sent=emitted.sent,
    )
    return emitted
