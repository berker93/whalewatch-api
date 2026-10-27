"""quota metering: API keys, monthly usage counters, and an MCP call quota

``api_key`` holds each account's API keys as SHA-256 hashes, with a few
characters of each to tell them apart by. ``usage_counter`` is what an account
spent of a metered thing in one quota month, written from Redis every few
minutes, with when its 80% email went.

Adds ``mcp_calls_month`` to the entitlement keys, beside ``api_calls_month``.
Its values are placeholders, as the API plans' are: the API plans' call quota,
and none on Free or Pro. Changing them is an UPDATE.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-25 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.db.models: a migration says what
# the schema was when it ran, whatever the models say later.
FLAGS = ("export_enabled", "alerts_instant", "mcp_enabled")
LIMITS = (
    "history_quarters",
    "insider_months",
    "watchlist_items",
    "screener_saved_filters",
    "api_calls_month",
)
NEW_LIMIT = "mcp_calls_month"
METERED = ("api_calls_month", "mcp_calls_month")

#: What each plan allows of the new key, by slug.
MCP_CALLS = {"free": 0, "pro": 0, "api_hobbyist": 10_000, "api_pro": 100_000}

ENTITLEMENT_TABLES = ("plan_entitlement", "subscription_entitlement")


def _quoted(keys: Sequence[str]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


def _key_check(keys: Sequence[str]) -> str:
    return f"key IN ({_quoted(keys)})"


def upgrade() -> None:
    for table in ENTITLEMENT_TABLES:
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint("key_is_known", table, _key_check((*LIMITS, NEW_LIMIT, *FLAGS)))
    rows = ", ".join(f"('{slug}', {value})" for slug, value in MCP_CALLS.items())
    # Every plan has every key, or the catalog refuses the lot: plans made by
    # hand since 0037 get none, as Free does.
    op.execute(f"""
        INSERT INTO plan_entitlement (plan_id, key, value)
        SELECT plan.id, '{NEW_LIMIT}', to_jsonb(coalesce(granted.calls, 0))
        FROM plan
        LEFT JOIN (VALUES {rows}) AS granted (slug, calls) USING (slug)
    """)

    op.create_table(
        "api_key",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_key"),
        sa.UniqueConstraint("token_hash", name="uq_api_key_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_api_key_user_id_app_user", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND length(name) BETWEEN 1 AND 80", name="name_is_named"
        ),
    )
    op.create_index("ix_api_key_user_id", "api_key", ["user_id", "id"])

    op.create_table(
        "usage_counter",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "key", "period", name="pk_usage_counter"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_usage_counter_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_key_check(METERED), name="key_is_metered"),
        sa.CheckConstraint("count >= 0", name="count_is_not_negative"),
    )


def downgrade() -> None:
    op.drop_table("usage_counter")
    op.drop_index("ix_api_key_user_id", "api_key")
    op.drop_table("api_key")
    for table in ENTITLEMENT_TABLES:
        op.execute(f"DELETE FROM {table} WHERE key = '{NEW_LIMIT}'")
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint("key_is_known", table, _key_check((*LIMITS, *FLAGS)))
