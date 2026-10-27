"""Monthly quotas: what each account spends, counted in Redis and kept in Postgres.

Two things are metered, each bounded by the plan entitlement of its name:
``api_calls_month``, the requests made with the account's API keys
(:mod:`app.api.metering`), and ``mcp_calls_month``, the calls made through the
MCP server, which counts them the same way when it exists.

The quota month
---------------
A month runs from the subscription's anniversary to the next one, not from
the first of the calendar month: an account that subscribed on the 14th
starts each month on the 14th, at midnight UTC. On a month without the day,
the month's last day: a subscription from 31 January starts its months on 28
or 29 February, 31 March, 30 April. Free, having no subscription, counts from
the account's own anniversary. :func:`quota_period` is the rule;
``usage_counter.period`` is a month's first day.

Redis is the hot path
---------------------
A metered request is one Lua script (:data:`_SPEND`), run atomically by Redis:
refuse when the month's count has reached the limit, else add one, note the
counter as changed since the last flush, and say whether this request is the
one that took the account to 80%. A refused request is not counted, so
retrying through a 429 spends nothing. Postgres is read only for a counter
Redis does not have, once per account, thing and month: the stored count is
where Redis starts from, so a Redis that lost its data carries on from the
last flush rather than from nothing.

Flushed to Postgres
-------------------
:func:`flush` writes every counter that changed since the last flush: every
:attr:`app.core.config.Settings.usage_flush_seconds` from each API process
(:func:`keep_flushing`), and once more as the process exits. A flush writes
the counter's whole count, never a difference, and keeps the larger of the
row's and Redis's, so two processes flushing, a flush retried after a failure,
and a flush that arrives after a later one all leave the row right. A lock
keeps to one flush at a time. A flush that fails leaves its counters to the
next.

The 80% email
-------------
Sent once per account, thing and month, and never twice: the request that
takes the count to 80% claims ``usage_counter.warned_at`` with a conditional
upsert (:func:`claim_warning`), and only the request whose claim wrote it
sends the mail. A Redis flag in front of the claim keeps every request past
80% from asking Postgres again. The mail is the account's own, sent under the
contract like a password reset: it is about the service the user pays for,
not news about the product.

A Redis that cannot be reached is the caller's to decide about: the meter
raises.
"""

import asyncio
import calendar
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal, get_args

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.entitlements import Grant
from app.core.logging import get_logger
from app.db.models import METERED_KEYS, AppUser, UsageCounter

logger = get_logger(__name__)

MeteredKey = Literal["api_calls_month", "mcp_calls_month"]
"""A limit counted per quota month."""

# The table's keys and the type's must be one list.
assert set(get_args(MeteredKey)) == set(METERED_KEYS)

#: What each is called, for a person.
QUOTA_NAMES: Final[dict[str, str]] = {
    "api_calls_month": "API calls",
    "mcp_calls_month": "MCP calls",
}

#: The share of a month's quota whose spending sends the warning email.
WARN_AT: Final = 0.8

#: Where a counter is, ``ww:usage:<user id>:<key>:<month's first day>``.
#: Outside ``ww:v1:``, which cache invalidation deletes wholesale.
COUNTER_PREFIX: Final = "ww:usage:"
#: The counters changed since the last flush.
DIRTY_KEY: Final = "ww:usage-dirty"
#: The counters a flush is writing, kept until it has written them.
FLUSHING_KEY: Final = "ww:usage-flushing"
FLUSH_LOCK_KEY: Final = "ww:usage-flush-lock"
#: Set once a month's 80% email has been asked for.
WARNED_PREFIX: Final = "ww:usage-warned:"

#: How long a counter stays in Redis after its month ends: time for the
#: month's last flush, and for an account page showing the month just gone.
KEEP_AFTER: Final = timedelta(days=7)

#: How long a flush may hold the lock before another may take it: a flush
#: whose process died does not stop the others for longer.
FLUSH_LOCK_SECONDS: Final = 60


# --- the quota month ---------------------------------------------------------------


def _on(year: int, month: int, day: int) -> date:
    """``day`` of the month, or the month's last day when it has fewer."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _months_after(day: date, months: int, anniversary: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return _on(year, month + 1, anniversary)


@dataclass(frozen=True, slots=True)
class QuotaPeriod:
    """One quota month: from its first day to the next month's, at midnight UTC."""

    start: date
    end: date
    """The next month's first day: when this one's quota resets."""

    @property
    def resets_at(self) -> datetime:
        return datetime.combine(self.end, time(), tzinfo=UTC)

    def seconds_left(self, at: datetime) -> int:
        """Whole seconds until the reset, at least 1."""
        return max(1, math.ceil((self.resets_at - at).total_seconds()))


def quota_period(anchor: datetime, at: datetime) -> QuotaPeriod:
    """The quota month ``at`` is in, for a subscription that began at ``anchor``.

    Both are moments; the day is UTC's.
    """
    anniversary = anchor.astimezone(UTC).day
    today = at.astimezone(UTC).date()
    start = _on(today.year, today.month, anniversary)
    if start > today:
        start = _months_after(start, -1, anniversary)
    return QuotaPeriod(start=start, end=_months_after(start, 1, anniversary))


def anchor_of(user: AppUser, grant: Grant | None) -> datetime:
    """When the account's quota months are counted from: its live
    subscription's start, or for Free the account's."""
    if grant is not None and grant.started_at is not None:
        return grant.started_at
    return user.created_at


def warn_threshold(limit: int | None) -> int | None:
    """The count at which a month's quota of ``limit`` is 80% spent, or None
    when nothing warns: no limit, or none to spend."""
    if not limit:
        return None
    return max(1, math.ceil(limit * WARN_AT))


# --- the meter ---------------------------------------------------------------------

# KEYS: the counter, the changed set, the 80% flag.
# ARGV: the limit (-1 for none), the 80% count (-1 for none), when the keys expire.
# Returns {status, count, warn}: status -1 when the counter is not in Redis
# (the caller seeds it and asks again), 0 refused, 1 counted; count after this
# request; warn 1 for the one request that should send the 80% email.
_SPEND: Final = """
local count = redis.call('GET', KEYS[1])
if not count then return {-1, 0, 0} end
count = tonumber(count)
local limit = tonumber(ARGV[1])
if limit >= 0 and count >= limit then return {0, count, 0} end
count = redis.call('INCR', KEYS[1])
redis.call('SADD', KEYS[2], KEYS[1])
local warn = 0
local threshold = tonumber(ARGV[2])
if threshold >= 0 and count >= threshold then
  if redis.call('SET', KEYS[3], '1', 'NX', 'EXAT', ARGV[3]) then warn = 1 end
end
return {1, count, warn}
"""


def counter_key(user_id: int, key: MeteredKey, period: QuotaPeriod) -> str:
    return f"{COUNTER_PREFIX}{user_id}:{key}:{period.start.isoformat()}"


def _parse_counter(name: str) -> tuple[int, str, date] | None:
    try:
        user_id, key, start = name.removeprefix(COUNTER_PREFIX).split(":")
        return int(user_id), key, date.fromisoformat(start)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Spent:
    """One metered request: counted or refused, and where the month stands after it."""

    admitted: bool
    used: int
    """The month's count after this request: its own included when admitted."""
    limit: int | None
    period: QuotaPeriod
    warn: bool = False
    """This request took the month to 80%: it claims the email (:func:`claim_warning`)."""

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


async def stored_count(session: AsyncSession, user_id: int, key: str, start: date) -> int:
    """The month's count as last flushed: 0 for a month with no row."""
    count = await session.scalar(
        select(UsageCounter.count).where(
            UsageCounter.user_id == user_id, UsageCounter.key == key, UsageCounter.period == start
        )
    )
    return count or 0


class UsageMeter:
    """The quota counters, in Redis."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script: AsyncScript | None = None

    async def spend(
        self,
        session: AsyncSession,
        user_id: int,
        key: MeteredKey,
        *,
        limit: int | None,
        period: QuotaPeriod,
    ) -> Spent:
        """Count one ``key`` for the account in ``period``, if ``limit`` leaves
        room for it. None for no limit: counted all the same.

        :raises RedisError: when Redis cannot be reached.
        """
        if self._script is None:
            self._script = self._redis.register_script(_SPEND)
        counter = counter_key(user_id, key, period)
        expires = int((period.resets_at + KEEP_AFTER).timestamp())
        threshold = warn_threshold(limit)
        keys = [counter, DIRTY_KEY, f"{WARNED_PREFIX}{counter.removeprefix(COUNTER_PREFIX)}"]
        args = [-1 if limit is None else limit, -1 if threshold is None else threshold, expires]
        status, used, warn = await self._script(keys=keys, args=args)
        if status == -1:
            # NX: of two requests seeding at once, the first one's stands.
            seed = await stored_count(session, user_id, key, period.start)
            await self._redis.set(counter, seed, nx=True, exat=expires)
            status, used, warn = await self._script(keys=keys, args=args)
        return Spent(
            admitted=status == 1, used=int(used), limit=limit, period=period, warn=warn == 1
        )

    async def unwarn(self, user_id: int, key: MeteredKey, period: QuotaPeriod) -> None:
        """Let the next request past 80% claim the email again: this one's
        claim could not be made."""
        counter = counter_key(user_id, key, period)
        await self._redis.delete(f"{WARNED_PREFIX}{counter.removeprefix(COUNTER_PREFIX)}")

    async def used(
        self, session: AsyncSession, user_id: int, keys: Iterable[MeteredKey], period: QuotaPeriod
    ) -> dict[MeteredKey, int]:
        """What the account has spent of each of ``keys`` this month: Redis's
        count, or the last flush's where Redis has none or cannot be reached."""
        wanted = list(keys)
        try:
            values = await self._redis.mget([counter_key(user_id, k, period) for k in wanted])
        except (RedisError, OSError) as exc:
            logger.warning("usage.unavailable", error=repr(exc))
            values = [None] * len(wanted)
        counts: dict[MeteredKey, int] = {}
        for key, value in zip(wanted, values, strict=True):
            if value is None:
                counts[key] = await stored_count(session, user_id, key, period.start)
            else:
                counts[key] = int(value)
        return counts


# --- the 80% email -------------------------------------------------------------------

_CLAIM: Final = text("""
    INSERT INTO usage_counter (user_id, key, period, count, warned_at)
    VALUES (:user_id, :key, :period, :count, now())
    ON CONFLICT (user_id, key, period) DO UPDATE
        SET warned_at = now(),
            count = GREATEST(usage_counter.count, EXCLUDED.count),
            updated_at = now()
        WHERE usage_counter.warned_at IS NULL
    RETURNING warned_at
""")


async def claim_warning(
    session: AsyncSession, user_id: int, key: MeteredKey, period: QuotaPeriod, count: int
) -> bool:
    """Mark the month's 80% email sent, if nothing has: True for the one
    caller that should send it. Does not commit.

    One statement: of two requests claiming at once, Postgres lets one write
    ``warned_at`` and finds the other's row already warned, so it returns
    nothing and that one sends nothing.
    """
    claimed = await session.scalar(
        _CLAIM, {"user_id": user_id, "key": key, "period": period.start, "count": count}
    )
    return claimed is not None


# --- the flush -----------------------------------------------------------------------

# Only users that still exist: an account erased while its counter waited
# loses the counter with it, rather than failing the flush for everyone.
_WRITE: Final = text("""
    INSERT INTO usage_counter (user_id, key, period, count)
    SELECT counted.user_id, counted.key, counted.period, counted.count
    FROM unnest(
        CAST(:user_ids AS bigint[]),
        CAST(:keys AS text[]),
        CAST(:periods AS date[]),
        CAST(:counts AS bigint[])
    ) AS counted (user_id, key, period, count)
    JOIN app_user ON app_user.id = counted.user_id
    ON CONFLICT (user_id, key, period) DO UPDATE
        SET count = EXCLUDED.count, updated_at = now()
        WHERE usage_counter.count < EXCLUDED.count
""")

# Deletes the lock only while it is still this flush's.
_RELEASE: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


async def write_counts(session: AsyncSession, counts: list[tuple[int, str, date, int]]) -> None:
    """Upsert each ``(user id, key, month's first day, count)``, keeping the
    larger count. Does not commit."""
    if not counts:
        return
    user_ids, keys, periods, values = (list(column) for column in zip(*counts, strict=True))
    await session.execute(
        _WRITE, {"user_ids": user_ids, "keys": keys, "periods": periods, "counts": values}
    )


async def flush(redis: Redis, factory: async_sessionmaker[AsyncSession]) -> int:
    """Write every counter changed since the last flush to Postgres. Returns
    how many were written; 0 when another flush holds the lock.

    The changed set is moved aside first, in one transaction, so a request
    counting meanwhile notes its counter for the next flush. The set moved
    aside is deleted only once Postgres has committed: a flush that fails, or
    whose process dies, leaves it for the next, which adds what has changed
    since and writes both.

    :raises RedisError: when Redis cannot be reached.
    :raises SQLAlchemyError: when Postgres cannot take the write.
    """
    token = uuid.uuid4().hex
    if not await redis.set(FLUSH_LOCK_KEY, token, nx=True, ex=FLUSH_LOCK_SECONDS):
        return 0
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.sunionstore(FLUSHING_KEY, [FLUSHING_KEY, DIRTY_KEY])
            pipe.delete(DIRTY_KEY)
            await pipe.execute()
        members: set[str] = await redis.smembers(FLUSHING_KEY)  # type: ignore[misc]
        names = sorted(members)
        values = await redis.mget(names) if names else []
        counts = []
        for name, value in zip(names, values, strict=True):
            parsed = _parse_counter(name)
            # Expired since it was counted: its month's last flush wrote it.
            if parsed is None or value is None:
                continue
            counts.append((*parsed, int(value)))
        if counts:
            async with factory() as session:
                await write_counts(session, counts)
                await session.commit()
        await redis.delete(FLUSHING_KEY)
        return len(counts)
    finally:
        await redis.eval(_RELEASE, 1, FLUSH_LOCK_KEY, token)  # type: ignore[misc]


async def flush_quietly(redis: Redis, factory: async_sessionmaker[AsyncSession]) -> None:
    """:func:`flush`, logging rather than raising: the counters wait in Redis
    for the next."""
    try:
        written = await flush(redis, factory)
    except (RedisError, OSError, SQLAlchemyError) as exc:
        logger.warning("usage.flush_failed", error=repr(exc))
        return
    if written:
        logger.info("usage.flushed", counters=written)


async def keep_flushing(
    redis: Redis, factory: async_sessionmaker[AsyncSession], every: float
) -> None:
    """Flush every ``every`` seconds, until cancelled. The lifespan flushes
    once more after cancelling it, as the process exits."""
    while True:
        await asyncio.sleep(every)
        await flush_quietly(redis, factory)
