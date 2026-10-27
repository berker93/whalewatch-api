"""Which days each account was active, and how many data exports it took.

What the admin console's actives (:mod:`app.admin.metrics`) and per-user usage
(:mod:`app.admin.users`) are counted from: one ``user_activity_day`` row per
account per UTC day it did anything. A signed-in request and a request with an
API key both count, written where each already marks the account or the key
used (:func:`app.accounts.sessions.touch`, :func:`app.accounts.api_keys.find_key`):
at most every five minutes, and on the first request of a new day, so no
request pays for a write it would not have made anyway but that one.

The rows are the account's, erased with it, and listed in its data export.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserActivityDay


def utc_day(at: datetime) -> date:
    return at.astimezone(UTC).date()


def due(last: datetime | None, at: datetime, every: timedelta) -> bool:
    """Whether a request at ``at`` should write what was last written at
    ``last``: never written, ``every`` gone, or a new UTC day."""
    if last is None:
        return True
    return at - last >= every or utc_day(last) != utc_day(at)


async def mark_active(session: AsyncSession, user_id: int, at: datetime) -> None:
    """Record the account active on ``at``'s UTC day. Does not commit."""
    await session.execute(
        insert(UserActivityDay)
        .values(user_id=user_id, day=utc_day(at))
        .on_conflict_do_nothing(index_elements=["user_id", "day"])
    )


async def count_export(session: AsyncSession, user_id: int, at: datetime) -> None:
    """Count one data export on ``at``'s UTC day, which also marks it active.
    Does not commit."""
    statement = insert(UserActivityDay).values(user_id=user_id, day=utc_day(at), data_exports=1)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "day"],
            set_={"data_exports": UserActivityDay.data_exports + 1},
        )
    )
