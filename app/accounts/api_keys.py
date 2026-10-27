"""API keys: what an account's programs call the data endpoints with.

A key is ``ww_`` and 32 random bytes, sent as ``Authorization: Bearer <key>``.
As with a session's cookie, the server keeps only its SHA-256
(:func:`app.accounts.sessions.digest`): the secret has 256 bits, so a slow hash
would guard nothing a fast one does not. The key is shown once, when it is
made, and never again; the account page tells keys apart by their name and
:data:`PREFIX_LENGTH` characters of each.

A key is its account, on the data endpoints and nowhere else: its plan's
history and insider windows (:mod:`app.api.entitlements`), and each request it
makes counted against the plan's ``api_calls_month`` (:mod:`app.api.metering`).
It signs nobody in: the account, the watchlist and the keys themselves want
the session cookie, so a leaked key reads data and changes nothing.

A key works until it is revoked, and stops working at once when it is: each
request reads the key's row, with its account and the account's grant, from
Postgres. Revoking marks the row rather than deleting it, and deleting the
account revokes every key it had.
"""

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.entitlements import Grant, grant_for
from app.accounts.sessions import digest, now
from app.db.models import ApiKey, AppUser

#: What every key starts with: a program, and a secret scanner, can tell one
#: from any other secret by it.
KEY_PREFIX: Final = "ww_"

#: How much of a key the account page shows: the prefix and eight more.
PREFIX_LENGTH: Final = len(KEY_PREFIX) + 8

#: The most keys one account holds at once. Revoked keys do not count.
MAX_API_KEYS: Final = 10

#: How stale ``api_key.last_used_at`` may get before a request writes it: a
#: write on every request would be a write on every call.
LAST_USED_EVERY: Final = timedelta(minutes=5)


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class KeyHolder:
    """Who a request made with an API key is from: the key's account, which
    key, and the account's grant (:mod:`app.accounts.entitlements` reads it)."""

    user: AppUser
    key_id: int
    grant: Grant | None
    """The account's live subscription: None for Free."""


async def find_key(session: AsyncSession, token: str) -> KeyHolder | None:
    """The live key ``token`` is and its account, or None: unknown, revoked,
    or its account deleted. Marks the key used, at most every
    :data:`LAST_USED_EVERY`, and commits when it does."""
    if not token.startswith(KEY_PREFIX):
        return None
    row = (
        await session.execute(
            select(ApiKey.id, ApiKey.last_used_at, AppUser)
            .join(AppUser, AppUser.id == ApiKey.user_id)
            .where(
                ApiKey.token_hash == digest(token),
                ApiKey.revoked_at.is_(None),
                AppUser.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    key_id, last_used_at, user = row
    used = now()
    if last_used_at is None or used - last_used_at >= LAST_USED_EVERY:
        await session.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=used))
        await session.commit()
    return KeyHolder(user=user, key_id=key_id, grant=await grant_for(session, user.id))


def _mine(user_id: int) -> ColumnElement[bool]:
    return and_(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))


async def live_keys(session: AsyncSession, user_id: int) -> list[ApiKey]:
    """The account's keys that work, oldest first."""
    return list(await session.scalars(select(ApiKey).where(_mine(user_id)).order_by(ApiKey.id)))


async def count_live(session: AsyncSession, user_id: int) -> int:
    return await session.scalar(select(func.count()).select_from(ApiKey).where(_mine(user_id))) or 0


async def create_key(session: AsyncSession, user_id: int, name: str) -> tuple[ApiKey, str]:
    """A new key for the account, and the key itself, which is not kept.
    Does not commit."""
    token = new_key()
    row = ApiKey(user_id=user_id, name=name, prefix=token[:PREFIX_LENGTH], token_hash=digest(token))
    session.add(row)
    await session.flush()
    return row, token


async def revoke_key(session: AsyncSession, user_id: int, key_id: int) -> None:
    """Stop the account's key ``key_id`` working. Nothing for a key already
    revoked, or another account's. Does not commit."""
    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id, _mine(user_id)).values(revoked_at=now())
    )


async def revoke_all(session: AsyncSession, user_id: int) -> None:
    """Stop every key of the account working: it is being deleted. Does not commit."""
    await session.execute(update(ApiKey).where(_mine(user_id)).values(revoked_at=now()))
