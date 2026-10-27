"""admin console: admins, their append-only log, plan overrides, snapshots, metrics

``admin_user`` is the console's own accounts: not ``app_user`` with a flag.
``admin_action`` records everything they do, and a trigger refuses any
UPDATE, DELETE or TRUNCATE of it. ``plan_override`` gives one user a plan,
with a reason and an expiry, above their subscription. ``plan`` gains
``snapshot_of_plan_id`` and ``snapshot_at``: grandfathering pins a
subscription to a private copy of its plan's terms. ``user_activity_day`` is
which days each user was active and how many data exports they took, and
``metric_day`` the daily snapshot of signups, actives and plan mix.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.db.models: a migration says what
# the schema was when it ran, whatever the models say later.
ACTIONS = (
    "login",
    "logout",
    "override_set",
    "override_ended",
    "grandfathered",
    "ungrandfathered",
    "admin_created",
    "admin_disabled",
)
SOURCES = ("console", "cli")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "admin_user",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_admin_user"),
        sa.UniqueConstraint("email", name="uq_admin_user_email"),
        sa.CheckConstraint("email = btrim(email)", name="email_is_trimmed"),
        sa.CheckConstraint("length(btrim(name)) >= 2", name="name_is_named"),
    )

    op.create_table(
        "admin_action",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_action"),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin_user.id"],
            name="fk_admin_action_admin_id_admin_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_admin_id"],
            ["admin_user.id"],
            name="fk_admin_action_target_admin_id_admin_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"action IN ({_quoted(ACTIONS)})", name="action_is_known"),
        sa.CheckConstraint(f"source IN ({_quoted(SOURCES)})", name="source_is_known"),
        sa.CheckConstraint(
            "(source = 'console') = (admin_id IS NOT NULL)", name="console_names_its_admin"
        ),
        sa.CheckConstraint("jsonb_typeof(detail) = 'object'", name="detail_is_an_object"),
    )
    op.create_index("ix_admin_action_user_id", "admin_action", ["user_id", "id"])
    op.create_index("ix_admin_action_admin_id", "admin_action", ["admin_id", "id"])
    # Append-only: whoever can write the log cannot rewrite it. A trigger
    # rather than a REVOKE, because the app connects as the table's owner,
    # whom a REVOKE does not bind.
    op.execute("""
        CREATE FUNCTION admin_action_is_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'admin_action is append-only: % refused', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER admin_action_append_only
        BEFORE UPDATE OR DELETE ON admin_action
        FOR EACH ROW EXECUTE FUNCTION admin_action_is_append_only()
    """)
    op.execute("""
        CREATE TRIGGER admin_action_no_truncate
        BEFORE TRUNCATE ON admin_action
        FOR EACH STATEMENT EXECUTE FUNCTION admin_action_is_append_only()
    """)

    op.add_column("plan", sa.Column("snapshot_of_plan_id", sa.BigInteger(), nullable=True))
    op.add_column("plan", sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_plan_snapshot_of_plan_id_plan",
        "plan",
        "plan",
        ["snapshot_of_plan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "snapshot_says_when", "plan", "(snapshot_of_plan_id IS NULL) = (snapshot_at IS NULL)"
    )
    op.create_check_constraint(
        "snapshot_is_private", "plan", "snapshot_of_plan_id IS NULL OR NOT is_public"
    )

    op.create_table(
        "plan_override",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("ended_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_plan_override"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_plan_override_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plan.id"], name="fk_plan_override_plan_id_plan", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["admin_user.id"],
            name="fk_plan_override_created_by_admin_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by"],
            ["admin_user.id"],
            name="fk_plan_override_ended_by_admin_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "reason = btrim(reason) AND length(reason) >= 3", name="reason_is_given"
        ),
        sa.CheckConstraint("expires_at > starts_at", name="expires_after_it_starts"),
        sa.CheckConstraint("(ended_at IS NULL) = (ended_by IS NULL)", name="ending_says_who"),
    )
    op.create_index("ix_plan_override_user_id", "plan_override", ["user_id", "id"])

    op.create_table(
        "user_activity_day",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("data_exports", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "day", name="pk_user_activity_day"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_user_activity_day_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("data_exports >= 0", name="data_exports_is_not_negative"),
    )
    op.create_index("ix_user_activity_day_day", "user_activity_day", ["day"])

    op.create_table(
        "metric_day",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("signups", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.Column("accounts", sa.Integer(), nullable=True),
        sa.Column("dau", sa.Integer(), nullable=True),
        sa.Column("wau", sa.Integer(), nullable=True),
        sa.Column("mau", sa.Integer(), nullable=True),
        sa.Column("plan_mix", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("comped", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("day", name="pk_metric_day"),
        sa.CheckConstraint(
            "(accounts IS NULL) = (plan_mix IS NULL) AND (plan_mix IS NULL) = (comped IS NULL)",
            name="snapshot_is_whole",
        ),
    )


def downgrade() -> None:
    op.drop_table("metric_day")
    op.drop_index("ix_user_activity_day_day", table_name="user_activity_day")
    op.drop_table("user_activity_day")
    op.drop_index("ix_plan_override_user_id", table_name="plan_override")
    op.drop_table("plan_override")
    op.drop_constraint("snapshot_is_private", "plan", type_="check")
    op.drop_constraint("snapshot_says_when", "plan", type_="check")
    op.drop_constraint("fk_plan_snapshot_of_plan_id_plan", "plan", type_="foreignkey")
    op.drop_column("plan", "snapshot_at")
    op.drop_column("plan", "snapshot_of_plan_id")
    # Snapshot plans are ordinary private plans once their columns are gone.
    op.execute("DROP TRIGGER admin_action_no_truncate ON admin_action")
    op.execute("DROP TRIGGER admin_action_append_only ON admin_action")
    op.execute("DROP FUNCTION admin_action_is_append_only()")
    op.drop_index("ix_admin_action_admin_id", table_name="admin_action")
    op.drop_index("ix_admin_action_user_id", table_name="admin_action")
    op.drop_table("admin_action")
    op.drop_table("admin_user")
