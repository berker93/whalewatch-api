"""alert delivery: quiet hours, batches, a daily cap and the season digest

``app_user`` learns the zone its owner sleeps in, ``quiet_time_zone``, and
their quiet hours, 22:00 to 08:00 unless they say otherwise. The zone is the
browser's, sent at signup; an account made before then gets the zone it
already has, which is New York's unless its owner changed it. A signup
request made while signups are closed keeps the browser's zone too, for
``create-user`` to carry over.

``alert_outbox`` is what is waiting to be emailed: each notification of a rule
that emails, until its batch goes, and each filing for the season digest.
``alert_delivery`` is each batch or digest that went, which the daily cap
counts in the user's own day.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-29 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
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
ROUTES = ("instant", "digest")
DELIVERY_KINDS = ("batch", "digest")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "quiet_time_zone",
            sa.Text(),
            server_default=sa.text("'America/New_York'"),
            nullable=False,
        ),
    )
    op.execute("UPDATE app_user SET quiet_time_zone = time_zone")
    op.add_column(
        "app_user",
        sa.Column("quiet_start", sa.Time(), server_default=sa.text("'22:00'"), nullable=False),
    )
    op.add_column(
        "app_user",
        sa.Column("quiet_end", sa.Time(), server_default=sa.text("'08:00'"), nullable=False),
    )
    op.create_check_constraint(
        "quiet_hours_are_minutes",
        "app_user",
        "extract(second FROM quiet_start) = 0 AND extract(second FROM quiet_end) = 0",
    )
    op.add_column("signup_request", sa.Column("quiet_time_zone", sa.Text(), nullable=True))

    op.create_table(
        "alert_delivery",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("local_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("emailed", sa.Boolean(), nullable=False),
        sa.Column("email_status", sa.Text(), nullable=True),
        sa.Column("notification_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_alert_delivery"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_alert_delivery_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.id"],
            name="fk_alert_delivery_notification_id_notification",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(f"kind IN ({_quoted(DELIVERY_KINDS)})", name="kind_is_known"),
        sa.CheckConstraint("items > 0", name="delivers_something"),
        sa.CheckConstraint("emailed OR email_status IS NULL", name="only_an_email_has_a_status"),
    )
    op.create_index("ix_alert_delivery_user_id", "alert_delivery", ["user_id", "local_at"])

    op.create_table(
        "alert_outbox",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("delivery_id", sa.BigInteger(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_alert_outbox"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_alert_outbox_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rule.id"],
            name="fk_alert_outbox_rule_id_alert_rule",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["alert_delivery.id"],
            name="fk_alert_outbox_delivery_id_alert_delivery",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"route IN ({_quoted(ROUTES)})", name="route_is_known"),
        sa.CheckConstraint(f"kind IN ({_quoted(KINDS)})", name="kind_is_known"),
        sa.CheckConstraint(
            "delivery_id IS NULL OR settled_at IS NOT NULL", name="delivered_is_settled"
        ),
        sa.UniqueConstraint("user_id", "dedupe_key", name="uq_alert_outbox_user_id_dedupe_key"),
    )
    op.create_index(
        "ix_alert_outbox_waiting",
        "alert_outbox",
        ["user_id", "queued_at"],
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.create_index("ix_alert_outbox_rule_id", "alert_outbox", ["rule_id"])
    op.create_index("ix_alert_outbox_delivery_id", "alert_outbox", ["delivery_id"])


def downgrade() -> None:
    op.drop_index("ix_alert_outbox_delivery_id", "alert_outbox")
    op.drop_index("ix_alert_outbox_rule_id", "alert_outbox")
    op.drop_index("ix_alert_outbox_waiting", "alert_outbox")
    op.drop_table("alert_outbox")
    op.drop_index("ix_alert_delivery_user_id", "alert_delivery")
    op.drop_table("alert_delivery")
    op.drop_column("signup_request", "quiet_time_zone")
    op.drop_constraint("quiet_hours_are_minutes", "app_user", type_="check")
    op.drop_column("app_user", "quiet_end")
    op.drop_column("app_user", "quiet_start")
    op.drop_column("app_user", "quiet_time_zone")
