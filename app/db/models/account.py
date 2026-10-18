"""The people who use WhaleWatch: their accounts, sessions, and what they follow.

Everything else in this schema is about filers and filings, public and the
same for everyone. These tables are the only ones about the site's own users,
and the only ones a request writes to.

``app_user``
    One account. The email is its login and is stored lowercased, so that
    ``Ada@Example.com`` and ``ada@example.com`` are one account.
``user_session``
    One signed-in browser. The cookie carries a random token; the table holds
    only its SHA-256, so a copy of the table is not a set of live sessions.
``account_token``
    A one-time link sent by email: confirm an email change, reset a password.
    Hashed for the same reason as a session.
``signup_request``
    Who asked for an account while signups were closed.
``watchlist_item``
    One thing one user follows: a stock, an investor or an insider.

See :mod:`app.accounts` for the rules these are written by.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: SHA-256, hex: what a session or a token is stored as.
TOKEN_HASH = CHAR(64)


class AppUser(Base):
    """One account. ``app_user`` rather than ``user``, which is a reserved word."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    email: Mapped[str] = mapped_column(Text, unique=True)
    """The login, trimmed and lowercased, as login and registration read it."""

    name: Mapped[str] = mapped_column(Text)
    """The name on the account, trimmed. What the header's initials are from."""

    password_hash: Mapped[str] = mapped_column(Text)
    """``scrypt$n$r$p$salt$hash``, from :func:`app.accounts.passwords.hash_password`."""

    time_zone: Mapped[str] = mapped_column(Text)
    """An IANA zone: what the timeline's days and Form 4 times are shown in."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """The last password change, or signup when it never changed."""

    __table_args__ = (
        CheckConstraint("email = lower(btrim(email))", name="email_is_normalised"),
        CheckConstraint("length(btrim(name)) >= 2", name="name_is_named"),
    )


class UserSession(Base):
    """One signed-in browser."""

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        # A deleted account takes its sessions with it.
        ForeignKey("app_user.id", ondelete="CASCADE"),
    )

    token_hash: Mapped[str] = mapped_column(TOKEN_HASH, unique=True)
    """SHA-256 of the cookie's token. The token itself is never stored."""

    remember: Mapped[bool] = mapped_column(Boolean, server_default=false())
    """Whether the cookie outlives the browser session (it has a max-age)."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """After this the token is refused, whatever the browser still holds."""

    __table_args__ = (
        # Signing out every other session, on a password change.
        Index("ix_user_session_user_id", "user_id"),
    )


class TokenPurpose(StrEnum):
    """What a one-time link does."""

    EMAIL_CHANGE = "email_change"
    PASSWORD_RESET = "password_reset"


PURPOSE_CHECK = "purpose IN ({})".format(", ".join(f"'{p.value}'" for p in TokenPurpose))


class AccountToken(Base):
    """A one-time link, sent by email."""

    __tablename__ = "account_token"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    purpose: Mapped[str] = mapped_column(Text)
    """One :class:`TokenPurpose` value."""

    token_hash: Mapped[str] = mapped_column(TOKEN_HASH, unique=True)

    new_email: Mapped[str | None] = mapped_column(Text)
    """For an email change, the address it changes to: the pending email."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(PURPOSE_CHECK, name="purpose_is_known"),
        CheckConstraint(
            "(purpose = 'email_change') = (new_email IS NOT NULL)",
            name="email_change_names_the_email",
        ),
        Index("ix_account_token_user_id", "user_id"),
    )


class SignupRequest(Base):
    """Someone who asked for an account while signups were closed. Once each."""

    __tablename__ = "signup_request"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WatchlistItem(Base):
    """One thing one user follows. Exactly one of the three references is set.

    Kept in the order added (:attr:`id`), which is the order the watchlist
    lists them: the frontend appends a new item before the answer arrives, and
    a reordered answer would make rows jump.
    """

    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    # CASCADE on each: a followed thing that is deleted is no longer followed,
    # rather than a row that blocks the delete.
    security_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )
    filer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("filer.id", ondelete="CASCADE")
    )
    insider_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("insider.id", ondelete="CASCADE")
    )

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(security_id, filer_id, insider_id) = 1", name="follows_one_thing"
        ),
        Index("ix_watchlist_item_user_id", "user_id", "id"),
        # Each thing once per user. Partial, so the other two kinds' nulls
        # never collide.
        Index(
            "uq_watchlist_item_user_security",
            "user_id",
            "security_id",
            unique=True,
            postgresql_where=text("security_id IS NOT NULL"),
        ),
        Index(
            "uq_watchlist_item_user_filer",
            "user_id",
            "filer_id",
            unique=True,
            postgresql_where=text("filer_id IS NOT NULL"),
        ),
        Index(
            "uq_watchlist_item_user_insider",
            "user_id",
            "insider_id",
            unique=True,
            postgresql_where=text("insider_id IS NOT NULL"),
        ),
    )
