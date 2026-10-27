"""Sessions and one-time tokens: random secrets the client holds and the server hashes.

A session's cookie and an emailed link both carry 32 random bytes. The server
keeps only their SHA-256, so a leaked copy of where they are kept, a Redis
snapshot or a database backup, is not a way in. A plain hash rather than a slow
one is enough: the secret has 256 bits, so there is nothing to guess, unlike a
password.

Sessions, in Redis
------------------
A session is one Redis key, ``ww:session:<sha256 of the cookie>``, holding the
user's id, and set to expire when the session does. Ending a session deletes
the key, and the next request with that cookie is refused: revocation is
instant, which a signed token (a JWT) cannot give without a lookup of its own.
``ww:user-sessions:<user id>`` is the set of a user's session keys, so that a
password change or reset can end every session the user has.

Both are outside ``ww:v1:``, which cache invalidation deletes wholesale, and
``ww:rl:``, the rate limiter's. Redis must keep them across a restart (the
append-only file, as docker-compose.yml runs it) and must not evict them under
memory pressure (``maxmemory-policy noeviction``), or a restart or a busy
cache signs everyone out.

``remember`` (the login form's "keep me signed in") is a cookie with a max-age
of :data:`REMEMBERED` and a session of that length; without it, the cookie
ends with the browser and the session after :data:`UNREMEMBERED` whatever the
browser does. Neither slides: a session ends when it was set to.

A session also keeps the user's entitlement grant
(:class:`app.accounts.entitlements.Grant`: their plan, grandfathered plan and
overrides), so that resolving what they may do reads nothing but the session.
It is read from Postgres on the session's first request and again after
:data:`app.accounts.entitlements.GRANT_EVERY`;
:meth:`SessionStore.forget_grants` has every session of a user read it again
on its next request, for code that has just changed their subscription.

A Redis that cannot be reached raises :class:`SessionStoreError`: a
request that needs to know who it is from is a 503, never a guess.

One-time tokens, in Postgres
----------------------------
Used once: redeeming deletes the token. A user has at most one live token per
purpose, so a second email change, reset or verification replaces the first
and its link stops working.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import activity
from app.accounts.entitlements import GRANT_EVERY, Grant, grant_for
from app.db.models import AccountToken, AppUser, TokenPurpose

REMEMBERED: Final = timedelta(days=30)
UNREMEMBERED: Final = timedelta(hours=12)

EMAIL_CHANGE_TTL: Final = timedelta(hours=24)
EMAIL_VERIFY_TTL: Final = timedelta(hours=24)
PASSWORD_RESET_TTL: Final = timedelta(hours=1)

#: How stale ``app_user.last_seen_at`` may get before a request writes it: a
#: write on every request would be a write on every page view.
LAST_SEEN_EVERY: Final = timedelta(minutes=5)

SESSION_KEY_PREFIX: Final = "ww:session:"
USER_SESSIONS_PREFIX: Final = "ww:user-sessions:"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


# --- sessions -----------------------------------------------------------------


class SessionStoreError(Exception):
    """Redis could not be reached, so nobody can be told who they are."""


@dataclass(frozen=True, slots=True)
class StartedSession:
    token: str
    """What goes in the cookie. Never stored."""
    expires_at: datetime
    remember: bool


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """What Redis keeps for one session."""

    id: str
    """The cookie's SHA-256: names the session without being a way in."""
    user_id: int
    remember: bool
    expires_at: datetime
    grant: Grant | None = None
    """The user's live subscription when it was last read: None for Free."""
    granted_at: datetime | None = None
    """When :attr:`grant` was read. None: it has not been, since the session
    began or since :meth:`SessionStore.forget_grants`."""

    def grant_is_stale(self, at: datetime) -> bool:
        if self.granted_at is None or at - self.granted_at >= GRANT_EVERY:
            return True
        # Kept before grants said when their subscription began.
        return self.grant is not None and self.grant.started_at is None

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": self.user_id,
                "remember": self.remember,
                "expires_at": self.expires_at.isoformat(),
                "grant": self.grant.to_json() if self.grant else None,
                "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            }
        )

    @classmethod
    def from_json(cls, session_id: str, value: str) -> "SessionRecord":
        stored = json.loads(value)
        grant, granted_at = stored.get("grant"), stored.get("granted_at")
        return cls(
            id=session_id,
            user_id=int(stored["user_id"]),
            remember=bool(stored["remember"]),
            expires_at=datetime.fromisoformat(stored["expires_at"]),
            grant=Grant.from_json(grant) if grant else None,
            granted_at=datetime.fromisoformat(granted_at) if granted_at else None,
        )


class SessionStore:
    """The live sessions, in Redis, by the hash of their cookie.

    The users' by default. The admin console's are another store over the
    same Redis, under other keys and with a lifetime of its own
    (:mod:`app.admin.auth`): a user's cookie names no key there, nor an
    admin's one here.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = SESSION_KEY_PREFIX,
        index_prefix: str = USER_SESSIONS_PREFIX,
        unremembered: timedelta = UNREMEMBERED,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._index_prefix = index_prefix
        self._unremembered = unremembered

    async def start(self, user_id: int, *, remember: bool) -> StartedSession:
        token = new_secret()
        session_id = digest(token)
        expires_at = now() + (REMEMBERED if remember else self._unremembered)
        value = SessionRecord(
            id=session_id, user_id=user_id, remember=remember, expires_at=expires_at
        ).to_json()
        index = f"{self._index_prefix}{user_id}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(self._prefix + session_id, value, exat=int(expires_at.timestamp()))
                pipe.sadd(index, session_id)
                # As long as the longest a session can be, from now: never
                # shorter than any member's remaining life.
                pipe.expire(index, int(REMEMBERED.total_seconds()))
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc
        return StartedSession(token=token, expires_at=expires_at, remember=remember)

    async def find(self, token: str) -> SessionRecord | None:
        """The live session ``token`` is the cookie of, or None: unknown, ended or expired."""
        session_id = digest(token)
        try:
            value = await self._redis.get(self._prefix + session_id)
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc
        if value is None:
            return None
        record = SessionRecord.from_json(session_id, value)
        # Redis expires the key at the same second; this is for the second itself.
        if record.expires_at <= now():
            return None
        return record

    async def keep_grant(self, record: SessionRecord, grant: Grant | None) -> SessionRecord:
        """``record`` holding ``grant``, read now, in Redis and returned.

        Only while the session lives (``XX``), and expiring when it did
        (``KEEPTTL``): a session ended meanwhile stays ended.
        """
        kept = replace(record, grant=grant, granted_at=now())
        try:
            await self._redis.set(self._prefix + record.id, kept.to_json(), xx=True, keepttl=True)
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc
        return kept

    async def forget_grants(self, user_id: int) -> None:
        """Have every session of the user read their grant again on its next
        request: for code that has just changed their subscription."""
        index = f"{self._index_prefix}{user_id}"
        try:
            members: set[str] = await self._redis.smembers(index)  # type: ignore[misc]
            ids = sorted(members)
            values = await self._redis.mget([self._prefix + member for member in ids])
            async with self._redis.pipeline(transaction=True) as pipe:
                for session_id, value in zip(ids, values, strict=True):
                    if value is None:
                        continue
                    forgotten = replace(
                        SessionRecord.from_json(session_id, value), grant=None, granted_at=None
                    )
                    pipe.set(
                        self._prefix + session_id,
                        forgotten.to_json(),
                        xx=True,
                        keepttl=True,
                    )
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc

    async def end(self, token: str) -> None:
        """End the session ``token`` is the cookie of. Nothing when there is none."""
        record = await self.find(token)
        if record is None:
            return
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(self._prefix + record.id)
                pipe.srem(f"{self._index_prefix}{record.user_id}", record.id)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc

    async def list_for(self, user_id: int) -> list[SessionRecord]:
        """The user's live sessions, soonest to end first: what an export lists."""
        index = f"{self._index_prefix}{user_id}"
        try:
            members: set[str] = await self._redis.smembers(index)  # type: ignore[misc]
            ids = sorted(members)
            values = await self._redis.mget([self._prefix + member for member in ids])
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc
        records = []
        for session_id, value in zip(ids, values, strict=True):
            # The index outlives a member that expired on its own.
            if value is None:
                continue
            records.append(SessionRecord.from_json(session_id, value))
        return sorted(records, key=lambda record: record.expires_at)

    async def end_all(self, user_id: int, *, keep: str | None) -> None:
        """Sign the user out everywhere, but for session ``keep`` when given."""
        index = f"{self._index_prefix}{user_id}"
        try:
            # redis-py types the set commands as sync-or-async; on the asyncio
            # client they are always awaitable.
            members: set[str] = await self._redis.smembers(index)  # type: ignore[misc]
            ending = [member for member in members if member != keep]
            if not ending:
                return
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(*(self._prefix + member for member in ending))
                pipe.srem(index, *ending)
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SessionStoreError from exc


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who a request is from: the signed-in user, which of their sessions, and
    their entitlement grant (:mod:`app.accounts.entitlements` reads it)."""

    user: AppUser
    session_id: str
    expires_at: datetime
    grant: Grant | None = None
    """The user's live subscription, as the session keeps it: None for Free."""


async def find_session(session: AsyncSession, store: SessionStore, token: str) -> Viewer | None:
    """The live session ``token`` is the cookie of and its user, or None: the
    session is unknown, ended or expired, or its account was deleted.

    The user's grant comes from the session, read from Postgres only when the
    session's copy is missing or stale.

    :raises SessionStoreError: when Redis cannot be reached.
    """
    record = await store.find(token)
    if record is None:
        return None
    user = await session.scalar(
        select(AppUser).where(AppUser.id == record.user_id, AppUser.deleted_at.is_(None))
    )
    if user is None:
        # Deleted since it signed in: the session goes with it.
        await store.end(token)
        return None
    if record.grant_is_stale(now()):
        record = await store.keep_grant(record, await grant_for(session, user.id))
    return Viewer(user=user, session_id=record.id, expires_at=record.expires_at, grant=record.grant)


async def touch(session: AsyncSession, user: AppUser) -> None:
    """Mark ``user`` seen now, and active today, when :data:`LAST_SEEN_EVERY`
    has passed since the last time or the UTC day has changed. Commits: the
    request may be a read that commits nothing."""
    seen = now()
    if not activity.due(user.last_seen_at, seen, LAST_SEEN_EVERY):
        return
    await session.execute(update(AppUser).where(AppUser.id == user.id).values(last_seen_at=seen))
    await activity.mark_active(session, user.id, seen)
    await session.commit()
    user.last_seen_at = seen


# --- one-time tokens ------------------------------------------------------------


async def issue_token(
    session: AsyncSession,
    user_id: int,
    purpose: TokenPurpose,
    ttl: timedelta,
    *,
    new_email: str | None = None,
) -> str:
    """A fresh token for ``purpose``, replacing any the user had for it."""
    await revoke_tokens(session, user_id, purpose)
    token = new_secret()
    session.add(
        AccountToken(
            user_id=user_id,
            purpose=purpose.value,
            token_hash=digest(token),
            new_email=new_email,
            expires_at=now() + ttl,
        )
    )
    await session.flush()
    return token


async def revoke_tokens(session: AsyncSession, user_id: int, purpose: TokenPurpose) -> None:
    await session.execute(
        delete(AccountToken).where(
            AccountToken.user_id == user_id, AccountToken.purpose == purpose.value
        )
    )


async def redeem_token(
    session: AsyncSession, token: str, purpose: TokenPurpose
) -> AccountToken | None:
    """The live token ``token`` is, deleted so it cannot be used again; None
    when it is unknown, used, replaced or expired.

    The DELETE ... RETURNING is the single use: of two requests racing with one
    token, one deletes the row and the other finds nothing."""
    found = await session.scalar(
        delete(AccountToken)
        .where(
            AccountToken.token_hash == digest(token),
            AccountToken.purpose == purpose.value,
            AccountToken.expires_at > now(),
        )
        .returning(AccountToken)
    )
    return found


async def pending_email(session: AsyncSession, user_id: int) -> str | None:
    """Where the user's email is changing to, while a confirmation is waiting."""
    return await session.scalar(
        select(AccountToken.new_email).where(
            AccountToken.user_id == user_id,
            AccountToken.purpose == TokenPurpose.EMAIL_CHANGE.value,
            AccountToken.expires_at > now(),
        )
    )
