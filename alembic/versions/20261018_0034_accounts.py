"""accounts

The site's own users, for the first time: ``app_user``, ``user_session``,
``account_token``, ``signup_request`` and ``watchlist_item``. See
:mod:`app.db.models.account`.

Created empty. While signups are closed (``SIGNUPS_OPEN``, false by default),
accounts are made with ``create-user``.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-18 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
        sa.UniqueConstraint("email", name="uq_app_user_email"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("email = lower(btrim(email))", name="email_is_normalised"),
        sa.CheckConstraint("length(btrim(name)) >= 2", name="name_is_named"),
    )

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

    op.create_table(
        "account_token",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("new_email", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_account_token"),
        sa.UniqueConstraint("token_hash", name="uq_account_token_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_account_token_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "purpose IN ('email_change', 'password_reset')", name="purpose_is_known"
        ),
        sa.CheckConstraint(
            "(purpose = 'email_change') = (new_email IS NOT NULL)",
            name="email_change_names_the_email",
        ),
    )
    op.create_index("ix_account_token_user_id", "account_token", ["user_id"])

    op.create_table(
        "signup_request",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("email", name="pk_signup_request"),
    )

    op.create_table(
        "watchlist_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=True),
        sa.Column("filer_id", sa.BigInteger(), nullable=True),
        sa.Column("insider_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_watchlist_item"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_watchlist_item_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_watchlist_item_security_id_security",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["filer_id"],
            ["filer.id"],
            name="fk_watchlist_item_filer_id_filer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["insider_id"],
            ["insider.id"],
            name="fk_watchlist_item_insider_id_insider",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "num_nonnulls(security_id, filer_id, insider_id) = 1", name="follows_one_thing"
        ),
    )
    op.create_index("ix_watchlist_item_user_id", "watchlist_item", ["user_id", "id"])
    for column, name in (
        ("security_id", "uq_watchlist_item_user_security"),
        ("filer_id", "uq_watchlist_item_user_filer"),
        ("insider_id", "uq_watchlist_item_user_insider"),
    ):
        op.create_index(
            name,
            "watchlist_item",
            ["user_id", column],
            unique=True,
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    op.drop_table("watchlist_item")
    op.drop_table("signup_request")
    op.drop_table("account_token")
    op.drop_table("user_session")
    op.drop_table("app_user")
