"""alerts: what each user asked to hear about, and what they were told

``alert_rule`` is one thing one user wants to hear about: a kind of event
and, by its kind, the filer or stock it is about, a dollar threshold, and
where to send it. ``notification`` is one thing one user was told, with a
payload that renders it on its own. ``(user_id, dedupe_key)`` is unique: the
key names the event, so a recompute that emits it again writes nothing. A
partial index on the unread rows serves the unread count. Both are erased
with the account, by their foreign keys.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-27 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.db.models: a migration says what
# the schema was when it ran, whatever the models say later.
KINDS = (
    "filer_filed",
    "filer_position_change",
    "stock_ownership_change",
    "insider_cluster",
    "season_digest",
)
FILER_KINDS = ("filer_filed", "filer_position_change")
SECURITY_KINDS = ("stock_ownership_change", "insider_cluster")
CHANNELS = ("in_app", "email")
PAYLOAD_KEYS = ("ticker", "filer_slug", "period", "delta", "confidence", "as_of")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "alert_rule",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filer_id", sa.BigInteger(), nullable=True),
        sa.Column("security_id", sa.BigInteger(), nullable=True),
        sa.Column("min_value", sa.Numeric(20, 2), nullable=True),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{in_app}'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_rule"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_alert_rule_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["filer_id"],
            ["filer.id"],
            name="fk_alert_rule_filer_id_filer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_alert_rule_security_id_security",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"kind IN ({_quoted(KINDS)})", name="kind_is_known"),
        sa.CheckConstraint(
            f"(filer_id IS NOT NULL) = (kind IN ({_quoted(FILER_KINDS)}))",
            name="filer_kinds_name_a_filer",
        ),
        sa.CheckConstraint(
            f"(security_id IS NOT NULL) = (kind IN ({_quoted(SECURITY_KINDS)}))",
            name="security_kinds_name_a_security",
        ),
        sa.CheckConstraint("min_value > 0", name="min_value_is_positive"),
        sa.CheckConstraint(
            f"cardinality(channels) >= 1 AND channels <@ ARRAY[{_quoted(CHANNELS)}]",
            name="channels_are_known",
        ),
    )
    op.create_index(
        "uq_alert_rule_user_id_kind_target",
        "alert_rule",
        ["user_id", "kind", "filer_id", "security_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "ix_alert_rule_filer_id",
        "alert_rule",
        ["filer_id", "kind"],
        postgresql_where=sa.text("filer_id IS NOT NULL"),
    )
    op.create_index(
        "ix_alert_rule_security_id",
        "alert_rule",
        ["security_id", "kind"],
        postgresql_where=sa.text("security_id IS NOT NULL"),
    )

    op.create_table(
        "notification",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notification"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_notification_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_notification_user_id_dedupe_key"),
        sa.CheckConstraint(f"kind IN ({_quoted(KINDS)})", name="kind_is_known"),
        sa.CheckConstraint("length(btrim(title)) > 0", name="title_is_titled"),
        sa.CheckConstraint(
            f"jsonb_typeof(payload) = 'object' AND payload ?& ARRAY[{_quoted(PAYLOAD_KEYS)}]",
            name="payload_renders",
        ),
        sa.CheckConstraint(
            "starts_with(dedupe_key, kind || ':') AND length(dedupe_key) > length(kind) + 1",
            name="dedupe_key_names_its_kind",
        ),
    )
    op.create_index(
        "ix_notification_unread",
        "notification",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notification_unread", "notification")
    op.drop_table("notification")
    op.drop_index("ix_alert_rule_security_id", "alert_rule")
    op.drop_index("ix_alert_rule_filer_id", "alert_rule")
    op.drop_index("uq_alert_rule_user_id_kind_target", "alert_rule")
    op.drop_table("alert_rule")
