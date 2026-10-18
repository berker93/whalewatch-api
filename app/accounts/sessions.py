"""Sessions and one-time tokens: random secrets the client holds and the database hashes.

A session's cookie and an emailed link both carry 32 random bytes. The table
keeps only their SHA-256, so a leaked copy of it, a backup or a replica, is not
a way in. A plain hash rather than a slow one is enough: the secret has 256
bits, so there is nothing to guess, unlike a password.

Sessions
--------
``remember`` (the login form's "keep me signed in") is a cookie with a max-age
of :data:`REMEMBERED` and a session of that length; without it, the cookie
ends with the browser and the session after :data:`UNREMEMBERED` whatever the
browser does. Neither slides: a session ends when it was set to.

One-time tokens
---------------
Used once: confirming deletes the token. An email change keeps at most one
pending per user, so a second change replaces the first and its link stops
working.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountToken, AppUser, TokenPurpose, UserSession

REMEMBERED: Final = timedelta(days=30)
UNREMEMBERED: Final = timedelta(hours=12)

EMAIL_CHANGE_TTL: Final = timedelta(hours=24)
PASSWORD_RESET_TTL: Final = timedelta(hours=1)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


# --- sessions -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartedSession:
    token: str
    """What goes in the cookie. Never stored."""
    expires_at: datetime
    remember: bool


async def start_session(session: AsyncSession, user_id: int, *, remember: bool) -> StartedSession:
    token = new_secret()
    expires_at = now() + (REMEMBERED if remember else UNREMEMBERED)
    session.add(
        UserSession(
            user_id=user_id, token_hash=digest(token), remember=remember, expires_at=expires_at
        )
    )
    await session.flush()
    return StartedSession(token=token, expires_at=expires_at, remember=remember)


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who a request is from: the signed-in user, and which of their sessions."""

    user: AppUser
    session_id: int
    expires_at: datetime


async def find_session(session: AsyncSession, token: str) -> Viewer | None:
    """The live session ``token`` is the cookie of, or None: unknown, ended or expired."""
    row = (
        await session.execute(
            select(AppUser, UserSession.id, UserSession.expires_at)
            .join(UserSession, UserSession.user_id == AppUser.id)
            .where(UserSession.token_hash == digest(token), UserSession.expires_at > now())
        )
    ).first()
    if row is None:
        return None
    user, session_id, expires_at = row
    return Viewer(user=user, session_id=session_id, expires_at=expires_at)


async def end_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(UserSession).where(UserSession.token_hash == digest(token)))


async def end_other_sessions(session: AsyncSession, user_id: int, *, keep: int | None) -> None:
    """Sign the user out everywhere, but for session ``keep`` when given."""
    statement = delete(UserSession).where(UserSession.user_id == user_id)
    if keep is not None:
        statement = statement.where(UserSession.id != keep)
    await session.execute(statement)


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
    when it is unknown, used, replaced or expired."""
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
