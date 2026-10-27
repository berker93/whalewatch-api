"""The signed-in user's notifications: the bell, and the panel it opens.

``GET /v1/me/notifications``
    Newest first, paged by cursor, every one or ``?unread_only=true``. Read
    off ``ix_notification_feed`` or ``ix_notification_unread``, both in the
    keyset's order, so a page is one index range however deep it is.
``POST /v1/me/notifications/read``
    Mark some read, by id, or all of them. A notification already read keeps
    the time it was first read. Answers with the new unread count, which the
    bell shows at once rather than wait for its next poll.
``GET /v1/me/notifications/count``
    The unread count, for the badge, which polls it. Kept in Redis for
    :data:`COUNT_TTL` per user, and replaced there by a mark-read, so the badge
    never counts what the user just read. A notification written in the
    meantime shows on the badge within the TTL: the writer can drop the count
    sooner with :func:`forget_count`.

Notifications are written by the jobs that see events happen, never here
(:mod:`app.db.models.alert`); nothing here deletes one.
"""

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import UNAUTHORIZED, NoStoreDep, ViewerDep
from app.api.deps import PageParamsDep, RedisDep, SessionDep
from app.api.errors import INVALID, INVALID_CURSOR
from app.api.pagination import Keyset, SortKey, paginate
from app.api.schemas.envelope import Meta
from app.api.schemas.notification import (
    MAX_COUNTED,
    MarkRead,
    NotificationCount,
    NotificationEnvelope,
)
from app.api.schemas.notification import Notification as NotificationOut
from app.core.logging import get_logger
from app.db.models import Notification

logger = get_logger(__name__)

router = APIRouter(prefix="/me/notifications", tags=["notifications"], dependencies=[NoStoreDep])

#: Seconds a user's unread count is kept in Redis.
COUNT_TTL: Final = 30

#: Newest first; ``id`` breaks a tie in ``created_at``, which a batch written
#: in one transaction shares.
FEED: Final = Keyset(
    "notifications.newest",
    (
        SortKey("created_at", Notification.created_at.expression, datetime, descending=True),
        SortKey("id", Notification.id.expression, int, descending=True),
    ),
)


def count_key(user_id: int) -> str:
    return f"ww:notifications:unread:{user_id}"


async def forget_count(redis: Redis, user_id: int) -> None:
    """Drop ``user_id``'s kept count, so the next poll counts again. A Redis
    that cannot be reached is logged and otherwise ignored: the count expires
    on its own."""
    try:
        await redis.delete(count_key(user_id))
    except RedisError as exc:
        logger.warning("notifications.count_forget_failed", user_id=user_id, exc_info=exc)


async def count_unread(session: AsyncSession, user_id: int) -> int:
    """Unread notifications, up to :data:`MAX_COUNTED`: an index-only read of
    at most that many entries of ``ix_notification_unread``."""
    unread = (
        select(Notification.id)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .limit(MAX_COUNTED)
        .subquery()
    )
    return await session.scalar(select(func.count()).select_from(unread)) or 0


async def _kept_count(redis: Redis, user_id: int) -> int | None:
    try:
        kept = await redis.get(count_key(user_id))
    except RedisError as exc:
        logger.warning("notifications.count_read_failed", user_id=user_id, exc_info=exc)
        return None
    return None if kept is None else int(kept)


async def _keep_count(redis: Redis, user_id: int, unread: int) -> None:
    try:
        await redis.set(count_key(user_id), str(unread), ex=COUNT_TTL)
    except RedisError as exc:
        logger.warning("notifications.count_write_failed", user_id=user_id, exc_info=exc)


@router.get(
    "",
    operation_id="getNotifications",
    response_model=NotificationEnvelope,
    summary="The signed-in user's notifications, newest first",
    responses={**UNAUTHORIZED, **INVALID_CURSOR, **INVALID},
)
async def read_notifications(
    viewer: ViewerDep,
    session: SessionDep,
    page: PageParamsDep,
    unread_only: Annotated[
        bool, Query(description="Only the notifications not yet marked read.")
    ] = False,
) -> NotificationEnvelope:
    """Newest first, ties by `id`. None is `[]`, never a 404. A page with
    `unread_only` and the one after it are the unread as they were when each
    was asked for: one marked read in between is not on the next."""
    statement = select(
        Notification.id,
        Notification.kind,
        Notification.title,
        Notification.body,
        Notification.payload,
        Notification.created_at,
        Notification.read_at,
    ).where(Notification.user_id == viewer.user.id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows, next_page = await paginate(session, statement, FEED, page)
    return NotificationEnvelope(
        data=[
            NotificationOut(
                id=str(row.id),
                kind=row.kind,
                title=row.title,
                body=row.body,
                payload=row.payload,
                created_at=row.created_at,
                read_at=row.read_at,
            )
            for row in rows
        ],
        meta=Meta(generated_at=datetime.now(UTC)),
        page=next_page,
    )


@router.get(
    "/count",
    operation_id="getNotificationCount",
    response_model=NotificationCount,
    summary="How many of the signed-in user's notifications are unread",
    responses=UNAUTHORIZED,
)
async def read_notification_count(
    viewer: ViewerDep, session: SessionDep, redis: RedisDep
) -> NotificationCount:
    """For the bell's badge, which polls it. Counted up to 100: 100 is 100 or
    more. Up to 30 seconds behind a notification sent, never behind one marked
    read through `POST /v1/me/notifications/read`."""
    user_id = viewer.user.id
    unread = await _kept_count(redis, user_id)
    if unread is None:
        unread = await count_unread(session, user_id)
        await _keep_count(redis, user_id, unread)
    return NotificationCount(unread=unread)


@router.post(
    "/read",
    operation_id="markNotificationsRead",
    response_model=NotificationCount,
    summary="Mark notifications read, by id or all",
    responses={**UNAUTHORIZED, **INVALID},
)
async def mark_notifications_read(
    body: MarkRead, viewer: ViewerDep, session: SessionDep, redis: RedisDep
) -> NotificationCount:
    """`ids`, or `all`. Idempotent: one already read keeps when it was first
    read, and an id that is not the user's is no change, not a 404. Answers
    with the unread count after."""
    user_id = viewer.user.id
    mark = (
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=func.now())
    )
    if body.ids is not None:
        mark = mark.where(Notification.id.in_([int(id_) for id_ in body.ids]))
    await session.execute(mark)
    await session.commit()
    # Kept, not just dropped: the badge's next poll is answered from it.
    unread = await count_unread(session, user_id)
    await _keep_count(redis, user_id, unread)
    return NotificationCount(unread=unread)
