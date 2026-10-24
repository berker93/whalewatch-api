"""The account jobs: deleted accounts erased once their grace has run out.

``purge-accounts`` is this with a summary, and the ``purge_accounts`` task is
this in ``asyncio.run``, sent by beat once a day (app/jobs/schedule.py).

The run row records how many accounts were erased and how many rows of each
table went with them, never who: a run log that named people would be one
more copy of what the purge is erasing. Each account's ``account_deletion``
row says which id it was.
"""

from __future__ import annotations

from collections import Counter

from app.accounts.privacy import Purged, purge_due
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
