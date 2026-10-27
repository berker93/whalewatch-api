"""The people who run WhaleWatch, what they did, and the site's numbers by day.

``admin_user``
    One operator of the admin console. Not an ``app_user`` with a flag: a
    separate account, with its own password, its own sessions in Redis
    (``ww:admin-session:``) and its own cookie, so that no bug in a user's
    account (a stolen session, an email change, a password reset) is a way
    into the console. Made only from the CLI (``create-admin``). Never
    deleted: disabling one (``disabled_at``) ends its sessions and keeps the
    row, which its actions name.
``admin_action``
    Everything an admin did, one row each: signing in and out, every change to
    a user's plan, and the CLI's admin management. Append-only, by a trigger
    that refuses UPDATE, DELETE and TRUNCATE (migration 0041): a log that can
    be edited by the people it records is not a log. ``user_id`` is not a
    foreign key, so the row outlives an account the purge erases, and it never
    holds an email.
``metric_day``
    One UTC day's signups, deletions, active accounts and plan mix, written
    once by the daily ``snapshot-metrics`` job (:mod:`app.admin.metrics`).
    The plan mix is a snapshot: it cannot be rebuilt for a day gone by, so a
    day written late has none.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AdminUser(Base):
    """One operator of the admin console."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    """The login. Unique among every admin ever made, disabled ones included:
    the log names an admin by id, and one address is one person."""

    name: Mapped[str] = mapped_column(Text)

    password_hash: Mapped[str] = mapped_column(Text)
    """argon2id, as a user's (:mod:`app.accounts.passwords`)."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the admin lost access. Null while they have it."""

    __table_args__ = (
        CheckConstraint("email = btrim(email)", name="email_is_trimmed"),
        CheckConstraint("length(btrim(name)) >= 2", name="name_is_named"),
    )


class AdminActionKind(StrEnum):
    """What an admin did."""

    LOGIN = "login"
    LOGOUT = "logout"
    OVERRIDE_SET = "override_set"
    """A user given a plan, with a reason and an expiry."""
    OVERRIDE_ENDED = "override_ended"
    """A user's override ended before its expiry."""
    GRANDFATHERED = "grandfathered"
    """A subscription pinned to a snapshot of its plan's terms."""
    UNGRANDFATHERED = "ungrandfathered"
    ADMIN_CREATED = "admin_created"
    ADMIN_DISABLED = "admin_disabled"


class ActionSource(StrEnum):
    """Where an action was taken."""

    CONSOLE = "console"
    """The admin API, signed in as ``admin_id``."""
    CLI = "cli"
    """A shell on a server: whoever could run it. ``admin_id`` is null."""


ACTION_CHECK: Final = "action IN ({})".format(", ".join(f"'{a.value}'" for a in AdminActionKind))
SOURCE_CHECK: Final = "source IN ({})".format(", ".join(f"'{s.value}'" for s in ActionSource))


class AdminAction(Base):
    """One thing one admin did. Written once, never changed."""

    __tablename__ = "admin_action"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped[str] = mapped_column(Text)
    """One :class:`ActionSource` value."""

    admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )
    """Who, from the console. Null from the CLI."""

    action: Mapped[str] = mapped_column(Text)
    """One :class:`AdminActionKind` value."""

    user_id: Mapped[int | None] = mapped_column(BigInteger)
    """The ``app_user.id`` acted on, if any. Not a foreign key: the row outlives
    the account."""

    target_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )
    """The admin acted on, for the CLI's admin management."""

    request_id: Mapped[str | None] = mapped_column(Text)
    """The request it was made in, to find its log lines by."""

    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    """What changed: plans by slug, the reason, the expiry. Never an email."""

    __table_args__ = (
        CheckConstraint(ACTION_CHECK, name="action_is_known"),
        CheckConstraint(SOURCE_CHECK, name="source_is_known"),
        CheckConstraint(
            "(source = 'console') = (admin_id IS NOT NULL)", name="console_names_its_admin"
        ),
        CheckConstraint("jsonb_typeof(detail) = 'object'", name="detail_is_an_object"),
        Index("ix_admin_action_user_id", "user_id", "id"),
        Index("ix_admin_action_admin_id", "admin_id", "id"),
    )


class MetricDay(Base):
    """One UTC day of the site's numbers, written once, by the day after."""

    __tablename__ = "metric_day"

    day: Mapped[date] = mapped_column(Date, primary_key=True)

    signups: Mapped[int] = mapped_column(Integer)
    """Accounts made that day."""

    deletions: Mapped[int] = mapped_column(Integer)
    """Accounts whose deletion was asked for that day."""

    accounts: Mapped[int | None] = mapped_column(Integer)
    """Accounts not deleted, as the day ended. Null for a day written late."""

    dau: Mapped[int | None] = mapped_column(Integer)
    """Accounts active that day: signed in, or an API key used. Null for a
    day before activity was recorded."""

    wau: Mapped[int | None] = mapped_column(Integer)
    """Accounts active in the 7 days ending that day."""

    mau: Mapped[int | None] = mapped_column(Integer)
    """Accounts active in the 30 days ending that day."""

    plan_mix: Mapped[dict[str, int] | None] = mapped_column(JSONB(none_as_null=True))
    """Accounts not deleted, by the plan they had as the day ended, slug to
    count, Free included. Null for a day written late."""

    comped: Mapped[int | None] = mapped_column(Integer)
    """Of those, how many had their plan from an override, not a payment."""

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(accounts IS NULL) = (plan_mix IS NULL) AND (plan_mix IS NULL) = (comped IS NULL)",
            name="snapshot_is_whole",
        ),
    )
