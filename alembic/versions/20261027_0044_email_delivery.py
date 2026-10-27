"""email delivery: a log of every message, and the addresses not to send to

``email_log`` is one row per message the site tried to send: its kind, the
stream it went out on (transactional or broadcast), the provider's id, and a
status the provider's webhooks move on. Erased with the account, by its
foreign key. ``email_suppression`` is the addresses a hard bounce or a spam
complaint stopped, by a SHA-256 of the address and with no tie to an
account, so it outlives the account it came from. ``consent_record`` learns
a fourth source, ``unsubscribe``: an email's link or its one-click header.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-27 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | Sequence[str] | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.db.models: a migration says what
# the schema was when it ran, whatever the models say later.
KINDS = (
    "verify_email",
    "registration_attempt",
    "email_change_confirmation",
    "email_change_notice",
    "password_reset",
    "account_deleted",
    "quota_warning",
    "payment_failed",
    "grace_ending",
    "downgraded",
    "payment_recovered",
    "alert",
)
STREAMS = ("transactional", "broadcast")
STATUSES = (
    "sending",
    "sent",
    "soft_bounced",
    "delivered",
    "bounced",
    "complained",
    "failed",
    "suppressed",
)
SCOPES = ("all", "broadcast")
REASONS = ("hard_bounce", "complaint", "provider")
OLD_SOURCES = ("register", "create_user", "preferences")
SOURCES = (*OLD_SOURCES, "unsubscribe")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "email_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_email_log"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_email_log_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"kind IN ({_quoted(KINDS)})", name="kind_is_known"),
        sa.CheckConstraint(f"stream IN ({_quoted(STREAMS)})", name="stream_is_known"),
        sa.CheckConstraint(f"status IN ({_quoted(STATUSES)})", name="status_is_known"),
    )
    op.create_index(
        "uq_email_log_provider_id",
        "email_log",
        ["provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )
    op.create_index("ix_email_log_user_id", "email_log", ["user_id", "id"])

    op.create_table(
        "email_suppression",
        sa.Column("address_hash", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("address_hash", name="pk_email_suppression"),
        sa.CheckConstraint("address_hash ~ '^[0-9a-f]{64}$'", name="address_hash_is_sha256"),
        sa.CheckConstraint(f"scope IN ({_quoted(SCOPES)})", name="scope_is_known"),
        sa.CheckConstraint(f"reason IN ({_quoted(REASONS)})", name="reason_is_known"),
    )

    # Bare names: the ck template prefixes them (see 0002).
    op.drop_constraint("source_is_known", "consent_record", type_="check")
    op.create_check_constraint(
        "source_is_known", "consent_record", f"source IN ({_quoted(SOURCES)})"
    )


def downgrade() -> None:
    # Never deleted: an unsubscribe is a withdrawal, and deleting it would put
    # the user back on the list. It was the user's own choice, as a
    # preferences row is.
    op.drop_constraint("source_is_known", "consent_record", type_="check")
    op.execute("UPDATE consent_record SET source = 'preferences' WHERE source = 'unsubscribe'")
    op.create_check_constraint(
        "source_is_known", "consent_record", f"source IN ({_quoted(OLD_SOURCES)})"
    )
    op.drop_table("email_suppression")
    op.drop_index("ix_email_log_user_id", "email_log")
    op.drop_index("uq_email_log_provider_id", "email_log")
    op.drop_table("email_log")
