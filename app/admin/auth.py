"""Admins: their own accounts, their own passwords, their own sessions.

Not a boolean on ``app_user``. An admin is an ``admin_user`` row, and nothing
a user can do to their account reaches it: a user's stolen session, a
password reset sent to a user's inbox, an email changed to an admin's address
all leave the console where it was. Admins are made and disabled from the
CLI only (``create-admin``, ``disable-admin``), by whoever has a shell on a
server, and both are logged (:mod:`app.admin.log`).

Sessions are a :class:`~app.accounts.sessions.SessionStore` of their own over
the same Redis, under ``ww:admin-session:``. They last
``ADMIN_SESSION_HOURS`` from sign-in and never longer: there is no "keep me
signed in". The cookie (:mod:`app.api.admin_auth`) is another name, path
``/admin``, ``SameSite=Strict``, and never shared across subdomains, so it
travels only with the console's own requests.

Disabling an admin ends their sessions at once and keeps the row, which
their logged actions and overrides name.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from redis.asyncio import Redis
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import rules
from app.accounts.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.accounts.sessions import SessionStore, now
from app.admin import log
from app.core.config import Settings
from app.db.models import AdminActionKind, AdminUser

ADMIN_SESSION_PREFIX: Final = "ww:admin-session:"
ADMIN_SESSIONS_PREFIX: Final = "ww:admin-sessions:"


class AdminError(Exception):
    """An admin could not be made or disabled: the message says why."""


def admin_store(redis: Redis, settings: Settings) -> SessionStore:
    """The admins' sessions: apart from the users', and shorter."""
    return SessionStore(
        redis,
        prefix=ADMIN_SESSION_PREFIX,
        index_prefix=ADMIN_SESSIONS_PREFIX,
        unremembered=timedelta(hours=settings.admin_session_hours),
    )


@dataclass(frozen=True, slots=True)
class Admin:
    """Who a console request is from."""

    admin: AdminUser
    session_id: str
    expires_at: datetime


def _live(email: str) -> Select[tuple[AdminUser]]:
    return select(AdminUser).where(AdminUser.email == email, AdminUser.disabled_at.is_(None))


async def authenticate(session: AsyncSession, email: str, password: str) -> AdminUser | None:
    """The admin ``email`` and ``password`` sign in as, or None. As long
    either way: an unknown address does a wrong password's argon2 work.
    Marks the admin signed in now; does not commit."""
    admin = await session.scalar(_live(rules.normalise_email(email)))
    matches = await verify_password(password, admin.password_hash if admin else DUMMY_HASH)
    if admin is None or not matches:
        return None
    if needs_rehash(admin.password_hash):
        admin.password_hash = await hash_password(password)
    admin.last_login_at = now()
    return admin


async def find_admin(session: AsyncSession, store: SessionStore, token: str) -> Admin | None:
    """The admin of the live session ``token`` is the cookie of, or None:
    unknown, ended or expired, or the admin disabled since.

    :raises SessionStoreError: when Redis cannot be reached.
    """
    record = await store.find(token)
    if record is None:
        return None
    admin = await session.scalar(
        select(AdminUser).where(AdminUser.id == record.user_id, AdminUser.disabled_at.is_(None))
    )
    if admin is None:
        await store.end(token)
        return None
    return Admin(admin=admin, session_id=record.id, expires_at=record.expires_at)


async def create_admin(session: AsyncSession, email: str, name: str, password: str) -> AdminUser:
    """A new admin, logged as made from the CLI. Does not commit.

    :raises AdminError: for a name, email or password the user rules refuse,
        or an address any admin, disabled or not, already has.
    """
    email, name = rules.normalise_email(email), rules.normalise_name(name)
    for field, problem in (
        ("--name", rules.name_problem(name)),
        ("--email", rules.email_problem(email)),
        ("password", rules.password_problem(password)),
    ):
        if problem:
            raise AdminError(f"{field} {problem}")
    if await session.scalar(select(AdminUser.id).where(AdminUser.email == email)) is not None:
        raise AdminError(f"{email} is an admin already, or was one")
    admin = AdminUser(email=email, name=name, password_hash=await hash_password(password))
    session.add(admin)
    await session.flush()
    await log.record(
        session, AdminActionKind.ADMIN_CREATED, admin_id=None, target_admin_id=admin.id
    )
    return admin


async def disable_admin(session: AsyncSession, store: SessionStore, email: str) -> AdminUser:
    """Take the console away from the admin ``email``, and end their sessions,
    logged as done from the CLI. Does not commit.

    :raises AdminError: when no admin with access has that address.
    :raises SessionStoreError: when Redis cannot be reached: nothing is disabled.
    """
    admin = await session.scalar(_live(rules.normalise_email(email)).with_for_update())
    if admin is None:
        raise AdminError(f"no admin with access has the login {email}")
    await store.end_all(admin.id, keep=None)
    admin.disabled_at = now()
    await log.record(
        session, AdminActionKind.ADMIN_DISABLED, admin_id=None, target_admin_id=admin.id
    )
    return admin
