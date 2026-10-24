"""What the site may do with each user's data, and the deletions it has done.

``consent_record``
    The ledger of lawful bases: one row each time a user's position on a
    purpose is set, by registering, by an operator making the account, or by
    the user changing it. Never updated; the latest row per purpose is where
    the user stands. Each row names its basis as well as its purpose, so the
    record says what the basis was when it was written, whatever it is later.
    Goes with the account when it is purged.
``account_deletion``
    One row per deletion: asked for, then purged or restored. No foreign key
    to ``app_user`` and no address or name, so it outlives the account it
    describes and holds nothing about the person but the id they had.

See :mod:`app.accounts.privacy` for the purposes and the rules.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Purpose(StrEnum):
    """Why the site processes a user's data."""

    ACCOUNT = "account"
    """Running the account the user signed up for: login, watchlist, timeline,
    and the emails the account cannot work without (verify, reset, change)."""
    PRODUCT_EMAIL = "product_email"
    """Emails about the product the user already uses: what changed, what is new."""
    MARKETING_EMAIL = "marketing_email"
    """Promotional email: offers, paid plans, anything selling."""


class Basis(StrEnum):
    """The lawful basis (GDPR Art. 6(1)) a purpose rests on."""

    CONTRACT = "contract"
    CONSENT = "consent"
    LEGITIMATE_INTEREST = "legitimate_interest"


#: Each purpose and the basis it rests on. The table's check holds rows to it.
BASES: dict[Purpose, Basis] = {
    Purpose.ACCOUNT: Basis.CONTRACT,
    Purpose.PRODUCT_EMAIL: Basis.LEGITIMATE_INTEREST,
    Purpose.MARKETING_EMAIL: Basis.CONSENT,
}


class ConsentSource(StrEnum):
    """What wrote a ``consent_record`` row."""

    REGISTER = "register"
    """The registration form, signups open."""
    CREATE_USER = "create_user"
    """An operator's ``create-user``, carrying over a signup request's answers if any."""
    PREFERENCES = "preferences"
    """The user, from their account: ``PATCH /v1/me/privacy``."""


class DeletionRequester(StrEnum):
    """Who asked for an account to be deleted."""

    USER = "user"
    """The user, signed in: ``DELETE /v1/account``."""
    OPERATOR = "operator"
    """An operator, on the user's request by other means: ``delete-user``."""


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


PURPOSE_BASIS_CHECK = " OR ".join(
    f"(purpose = '{purpose.value}' AND basis = '{basis.value}')" for purpose, basis in BASES.items()
)
SOURCE_CHECK = f"source IN ({_values(ConsentSource)})"
REQUESTED_BY_CHECK = f"requested_by IN ({_values(DeletionRequester)})"


class ConsentRecord(Base):
    """One user's position on one purpose, from one moment on."""

    __tablename__ = "consent_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    purpose: Mapped[str] = mapped_column(Text)
    """One :class:`Purpose` value."""

    basis: Mapped[str] = mapped_column(Text)
    """The :class:`Basis` the purpose rests on, as of this row."""

    granted: Mapped[bool] = mapped_column(Boolean)
    """Whether the purpose applies from now: consent given, or not objected to.
    For ``account``, always true: the contract ends by deleting the account."""

    terms_version: Mapped[str | None] = mapped_column(Text)
    """For ``account``, the terms the user accepted. Null when none were: an
    account an operator made without a signup request's acceptance."""

    privacy_version: Mapped[str | None] = mapped_column(Text)
    """The privacy policy in force when the row was written, which the user
    was shown. Null on the same terms as ``terms_version``."""

    source: Mapped[str] = mapped_column(Text)
    """One :class:`ConsentSource` value."""

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(PURPOSE_BASIS_CHECK, name="purpose_has_its_basis"),
        CheckConstraint(SOURCE_CHECK, name="source_is_known"),
        CheckConstraint("purpose <> 'account' OR granted", name="account_is_granted"),
        Index("ix_consent_record_user_id", "user_id", "purpose", "id"),
    )


class AccountDeletion(Base):
    """One account's deletion, from the request to the purge."""

    __tablename__ = "account_deletion"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger)
    """The ``app_user.id`` the account had. Not a foreign key: the row outlives it."""

    requested_by: Mapped[str] = mapped_column(Text)
    """One :class:`DeletionRequester` value."""

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When the purge job may erase the account: thirty days after the request."""

    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the account and everything hanging off it were erased."""

    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When an operator undid the deletion, before the purge."""

    erased: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """How many rows of each table the purge erased, by table name."""

    __table_args__ = (
        CheckConstraint(REQUESTED_BY_CHECK, name="requested_by_is_known"),
        CheckConstraint(
            "purged_at IS NULL OR restored_at IS NULL", name="purged_or_restored_not_both"
        ),
        CheckConstraint("(purged_at IS NULL) = (erased IS NULL)", name="purge_says_what"),
        # At most one deletion waiting per account.
        Index(
            "uq_account_deletion_user_id",
            "user_id",
            unique=True,
            postgresql_where=text("purged_at IS NULL AND restored_at IS NULL"),
        ),
    )
