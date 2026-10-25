"""The people who use WhaleWatch: their accounts, sessions, and what they follow.

Everything else in this schema is about filers and filings, public and the
same for everyone. These tables are the only ones about the site's own users,
and the only ones a request writes to.

``app_user``
    One account. The email is its login, a ``citext``, so that
    ``Ada@Example.com`` and ``ada@example.com`` are one account however either
    was typed. Deleting an account marks it (``deleted_at``) rather than
    removing the row, and frees its address for a new account.
``account_token``
    A one-time link sent by email: verify an address, confirm an email change,
    reset a password. The table holds only the token's SHA-256, so a copy of it
    is not a set of working links.

Sessions are not here: they are in Redis (:mod:`app.accounts.sessions`), where
ending one is deleting a key.
``signup_request``
    Who asked for an account while signups were closed, and what they agreed
    to on the form, for ``create-user`` to carry over.
``watchlist_item``
    One thing one user follows: a stock, an investor or an insider.
``saved_filter``
    One screener filter one user saved: a name, and ``GET /v1/flows``'s
    parameters.

See :mod:`app.accounts` for the rules these are written by.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

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
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: SHA-256, hex: what a one-time token is stored as.
TOKEN_HASH = CHAR(64)


class AppUser(Base):
    """One account. ``app_user`` rather than ``user``, which is a reserved word."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    email: Mapped[str] = mapped_column(CITEXT)
    """The login, trimmed, as typed. Compared without case, by the type: unique
    without case among accounts not deleted (``uq_app_user_email``)."""

    name: Mapped[str] = mapped_column(Text)
    """The name on the account, trimmed. What the header's initials are from."""

    password_hash: Mapped[str] = mapped_column(Text)
    """An argon2id PHC string, from :func:`app.accounts.passwords.hash_password`."""

    time_zone: Mapped[str] = mapped_column(Text)
    """An IANA zone: what the timeline's days and Form 4 times are shown in."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """The last password change, or signup when it never changed."""

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the address was shown to be the user's, by a link sent to it. Null
    until then, and while null the account reads but saves nothing."""

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """The last signed-in request, to within :data:`app.accounts.sessions.LAST_SEEN_EVERY`."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the account was deleted. A deleted account signs nobody in and
    holds no address: its email may make a new account."""

    __table_args__ = (
        CheckConstraint("email = btrim(email)", name="email_is_trimmed"),
        CheckConstraint("length(btrim(name)) >= 2", name="name_is_named"),
        Index(
            "uq_app_user_email",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class TokenPurpose(StrEnum):
    """What a one-time link does."""

    EMAIL_VERIFY = "email_verify"
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

    new_email: Mapped[str | None] = mapped_column(CITEXT)
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

    email: Mapped[str] = mapped_column(CITEXT, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    terms_version: Mapped[str | None] = mapped_column(Text)
    """The terms accepted on the form. Null on a request from before the form asked."""
    privacy_version: Mapped[str | None] = mapped_column(Text)
    """The privacy policy the form showed. Null on the same terms."""
    marketing_email: Mapped[bool] = mapped_column(Boolean, server_default=false())
    """Whether the form's marketing box was ticked."""


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


#: The longest a saved filter's name may be.
MAX_FILTER_NAME: int = 80


class SavedFilter(Base):
    """One screener filter one user saved, listed in the order saved (:attr:`id`)."""

    __tablename__ = "saved_filter"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(Text)
    """What the user called it, trimmed."""

    query: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """``GET /v1/flows``'s parameters, as :class:`app.api.schemas.saved_filter.ScreenerQuery`
    validated them, without the ones left at their defaults."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            f"name = btrim(name) AND length(name) BETWEEN 1 AND {MAX_FILTER_NAME}",
            name="name_is_named",
        ),
        CheckConstraint("jsonb_typeof(query) = 'object'", name="query_is_an_object"),
        Index("ix_saved_filter_user_id", "user_id", "id"),
    )
