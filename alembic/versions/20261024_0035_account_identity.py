"""account identity

``app_user.email`` becomes a ``citext``, stored as typed, so ``Berk@`` and
``berk@`` are one account by the type rather than by every caller remembering
to lowercase. Its uniqueness is now among accounts not deleted, so a deleted
account's address can make a new one. ``signup_request.email`` and
``account_token.new_email`` become ``citext`` too, so comparing them to a login
is case-blind as well.

Three new columns on ``app_user``: ``email_verified_at`` (null until a link
sent to the address is followed; an unverified account reads but saves
nothing), ``last_seen_at`` and ``deleted_at``. Accounts that exist already were
made by ``create-user``, by hand, and count as verified.

``account_token`` gains the purpose ``email_verify``.

``user_session`` is dropped: sessions are in Redis now
(:mod:`app.accounts.sessions`), where ending one is instant. Everyone signed in
is signed out by this migration.

Passwords move from scrypt to argon2id with no conversion, which a one-way hash
cannot have: an account with a scrypt hash logs in again after a password
reset. No deployed database has one.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-24 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.drop_constraint("email_is_normalised", "app_user", type_="check")
    op.drop_constraint("uq_app_user_email", "app_user", type_="unique")
    op.alter_column("app_user", "email", type_=CITEXT(), existing_nullable=False)
    op.create_check_constraint("email_is_trimmed", "app_user", "email = btrim(email)")
    op.add_column("app_user", sa.Column("email_verified_at", sa.DateTime(timezone=True)))
    op.add_column("app_user", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column("app_user", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE app_user SET email_verified_at = created_at")
    op.create_index(
        "uq_app_user_email",
        "app_user",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.alter_column("signup_request", "email", type_=CITEXT(), existing_nullable=False)

    op.alter_column("account_token", "new_email", type_=CITEXT(), existing_nullable=True)
    op.drop_constraint("purpose_is_known", "account_token", type_="check")
    op.create_check_constraint(
        "purpose_is_known",
        "account_token",
        "purpose IN ('email_verify', 'email_change', 'password_reset')",
    )

    op.drop_table("user_session")


def downgrade() -> None:
    op.create_table(
        "user_session",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("remember", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_session"),
        sa.UniqueConstraint("token_hash", name="uq_user_session_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_user_session_user_id_app_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])

    op.execute("DELETE FROM account_token WHERE purpose = 'email_verify'")
    op.drop_constraint("purpose_is_known", "account_token", type_="check")
    op.create_check_constraint(
        "purpose_is_known", "account_token", "purpose IN ('email_change', 'password_reset')"
    )
    op.alter_column("account_token", "new_email", type_=sa.Text(), existing_nullable=True)

    op.alter_column("signup_request", "email", type_=sa.Text(), existing_nullable=False)

    # Deleted accounts cannot come back under a plain unique email, and an
    # address stored as typed has to be lowercased to pass the old check.
    op.execute("DELETE FROM app_user WHERE deleted_at IS NOT NULL")
    op.drop_index("uq_app_user_email", "app_user")
    op.drop_constraint("email_is_trimmed", "app_user", type_="check")
    op.drop_column("app_user", "deleted_at")
    op.drop_column("app_user", "last_seen_at")
    op.drop_column("app_user", "email_verified_at")
    op.alter_column("app_user", "email", type_=sa.Text(), existing_nullable=False)
    op.execute("UPDATE app_user SET email = lower(email)")
    op.create_unique_constraint("uq_app_user_email", "app_user", ["email"])
    op.create_check_constraint("email_is_normalised", "app_user", "email = lower(btrim(email))")
    # The citext extension stays: dropping it is a superuser's call, and
    # another schema may use it.
