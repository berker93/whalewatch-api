"""API keys, and what each account has spent of its monthly quotas.

``api_key``
    A secret one account's programs call the API with, as
    ``Authorization: Bearer ww_...``. The table holds only the secret's
    SHA-256 and a few characters of it to tell keys apart by, so a copy of it
    is not a set of working keys. Revoking a key marks it (``revoked_at``);
    the row stays, so the account page can say a key was revoked rather than
    that it never was.
``usage_counter``
    How many of one metered thing (:data:`METERED_KEYS`) one account spent in
    one quota month: ``period`` is the month's first day. The count is
    Redis's (:mod:`app.accounts.usage`), written here every few minutes; a
    flush writes the larger of the two, so a flush that arrives late never
    takes a count back. ``warned_at`` is when the 80% email went: once per
    account, thing and month, and the row is what makes it once.
"""

from datetime import date, datetime
from typing import Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: The limits counted per month, by the plan entitlement that bounds them.
METERED_KEYS: Final = ("api_calls_month", "mcp_calls_month")

METERED_KEY_CHECK: Final = "key IN ({})".format(", ".join(f"'{key}'" for key in METERED_KEYS))

#: The longest a key's name may be.
MAX_KEY_NAME: Final = 80


class ApiKey(Base):
    """One API key of one account."""

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    name: Mapped[str] = mapped_column(Text)
    """What the user called it, trimmed: "laptop", "nightly job"."""

    prefix: Mapped[str] = mapped_column(Text)
    """The key's first characters, ``ww_Ab12Cd34``: enough to tell it from the
    others on a list, far too few to be it."""

    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    """The key's SHA-256, hex. The key itself is shown once, when made."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """The last request made with it, to within :data:`app.accounts.api_keys.LAST_USED_EVERY`."""

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When it stopped working. Null while it works."""

    __table_args__ = (
        CheckConstraint(
            f"name = btrim(name) AND length(name) BETWEEN 1 AND {MAX_KEY_NAME}",
            name="name_is_named",
        ),
        Index("ix_api_key_user_id", "user_id", "id"),
    )


class UsageCounter(Base):
    """What one account spent of one metered thing in one quota month."""

    __tablename__ = "usage_counter"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    """One of :data:`METERED_KEYS`."""
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    """The quota month's first day, UTC: the subscription's anniversary in its month."""
    count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the 80% email was sent for this month. Null: not yet."""
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(METERED_KEY_CHECK, name="key_is_metered"),
        CheckConstraint("count >= 0", name="count_is_not_negative"),
    )
