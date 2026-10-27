"""The account jobs: deleted accounts erased once their grace has run out, and
billing's upkeep: deleted accounts' subscriptions cancelled, and the dunning
of renewals whose payment failed.

``purge-accounts`` is :func:`purge_accounts` with a summary, and the
``purge_accounts`` task is it in ``asyncio.run``, sent by beat once a day
(app/jobs/schedule.py). ``billing`` and the ``run_billing`` task are
:func:`run_billing`, sent by beat every hour. ``snapshot-metrics`` and the
``snapshot_metrics`` task are :func:`snapshot_metrics`, the admin console's
daily numbers, sent by beat just after midnight UTC.

The run row records how many accounts were erased and how many rows of each
table went with them, never who: a run log that named people would be one
more copy of what the purge is erasing. Each account's ``account_deletion``
row says which id it was.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.accounts.billing import Cancelled, DunningRun, cancel_for_deleted
from app.accounts.billing import run_dunning as due_dunning
from app.accounts.mail import deliver, mailer_for
from app.accounts.paddle import PaddleClient, http_client
from app.accounts.privacy import Purged, purge_due
from app.admin import metrics
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.jobs.tracking import TrackedRun, tracked_task

logger = get_logger(__name__)


@tracked_task("purge-accounts")
async def purge_accounts(run: TrackedRun, settings: Settings) -> list[Purged]:
    """Erase every deleted account past its thirty days, in one transaction."""
    async with session_scope(settings) as session:
        purged = await purge_due(session)
    run.items_seen = run.items_written = len(purged)
    erased: Counter[str] = Counter()
    for account in purged:
        erased.update(account.erased)
    run.metrics["erased"] = dict(sorted(erased.items()))
    logger.info("accounts.purged", accounts=len(purged), **dict(erased))
    return purged


@dataclass(frozen=True, slots=True)
class BillingRun:
    cancelled: Cancelled
    dunning: DunningRun


@tracked_task("billing")
async def run_billing(run: TrackedRun, settings: Settings) -> BillingRun:
    """Cancel the Paddle subscriptions of deleted accounts that are still
    billing, then send the dunning emails due.

    The cancellations are the retries: deleting an account cancels at once,
    and only one Paddle refused or could not be reached for is left here.
    Where billing is not configured, nothing is cancelled; the purge holds
    any such account back and logs it.

    A renewal's plan stops at ``grace_ends_at`` without this (the grant ends
    then); this sends the reminder before and the notice after. Each is
    claimed in the transaction and sent once it has committed, so a run that
    fails sends nothing and the next sends it, and no email is sent twice.
    """
    cancelled = Cancelled(plans=[], failed=0)
    if settings.billing_enabled:
        async with session_scope(settings) as session, http_client(settings) as http:
            cancelled = await cancel_for_deleted(session, PaddleClient(http))
    async with session_scope(settings) as session:
        dunning = await due_dunning(session, settings, datetime.now(UTC))
    mailer = mailer_for(settings)
    for message in dunning.mail:
        await deliver(mailer, message)
    run.items_seen = len(cancelled.plans) + cancelled.failed + dunning.reminded + dunning.downgraded
    run.items_written = len(cancelled.plans) + len(dunning.mail)
    run.metrics.update(
        cancelled=len(cancelled.plans),
        cancel_failed=cancelled.failed,
        reminded=dunning.reminded,
        downgraded=dunning.downgraded,
    )
    logger.info("billing.upkeep", **run.metrics)
    return BillingRun(cancelled=cancelled, dunning=dunning)


@tracked_task("snapshot-metrics")
async def snapshot_metrics(run: TrackedRun, settings: Settings) -> list[date]:
    """Write the admin console's numbers for every UTC day since the last
    written, through yesterday: the days written. Counts only, never who."""
    async with session_scope(settings) as session:
        written = await metrics.snapshot(session, metrics.utc_today())
    run.items_seen = run.items_written = len(written)
    run.metrics["days"] = [day.isoformat() for day in written]
    logger.info("admin.metrics_snapshot", days=len(written))
    return written
