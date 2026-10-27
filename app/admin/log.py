"""``admin_action``: one row for everything an admin does, never changed after.

Written in the same transaction as the change it records, so a change that
rolls back leaves no row, and a row that exists is a change that happened.
Postgres refuses any UPDATE, DELETE or TRUNCATE of the table (migration 0041).

A row names the account it was about by id, and never by email: the log
outlives an account the purge erases, and an id of an erased account is no
one's.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActionSource, AdminAction, AdminActionKind


async def record(
    session: AsyncSession,
    action: AdminActionKind,
    *,
    admin_id: int | None,
    user_id: int | None = None,
    target_admin_id: int | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AdminAction:
    """Log ``action`` by ``admin_id``, from the console; from the CLI when
    ``admin_id`` is None. Does not commit."""
    row = AdminAction(
        source=(ActionSource.CONSOLE if admin_id is not None else ActionSource.CLI).value,
        admin_id=admin_id,
        action=action.value,
        user_id=user_id,
        target_admin_id=target_admin_id,
        request_id=request_id,
        detail=detail or {},
    )
    session.add(row)
    await session.flush()
    return row


async def recent(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    admin_id: int | None = None,
    before: int | None = None,
    limit: int = 50,
) -> list[AdminAction]:
    """The newest actions first, about ``user_id`` or by ``admin_id`` when
    given, older than id ``before`` when given."""
    query = select(AdminAction).order_by(AdminAction.id.desc()).limit(limit)
    if user_id is not None:
        query = query.where(AdminAction.user_id == user_id)
    if admin_id is not None:
        query = query.where(AdminAction.admin_id == admin_id)
    if before is not None:
        query = query.where(AdminAction.id < before)
    return list(await session.scalars(query))
