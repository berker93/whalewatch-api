"""entitlements: plans, what each allows, and who is on which, as rows

``plan`` names a plan and its price; ``plan_entitlement`` is one row per
capability per plan; ``subscription`` puts a user on a plan, at most one live
per user, with ``grandfathered_plan_id`` for terms promised to them;
``subscription_entitlement`` is one subscription's own value for a key.

Seeds the four launch plans. Free and Pro are the prototype's (the web
repo's docs/design): the latest four quarters against five years, ten
watchlist items and three saved filters against no limit, export and same-day
alerts on Pro only, and six months of insider trades on Free. The two API
plans' prices and call quotas are placeholders until they are agreed:
changing them is an UPDATE.

Nobody is subscribed: every account stays on Free.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-24 20:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
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


def _quoted(keys: Sequence[str]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


KEY_CHECK = f"key IN ({_quoted(LIMITS + FLAGS)})"
VALUE_CHECK = (
    f"CASE WHEN key IN ({_quoted(FLAGS)}) THEN jsonb_typeof(value) = 'boolean' "
    "WHEN jsonb_typeof(value) = 'null' THEN true "
    "WHEN jsonb_typeof(value) = 'number' "
    "THEN value::numeric >= 0 AND value::numeric = trunc(value::numeric) "
    "ELSE false END"
)
STATUSES = (
    "trialing",
    "active",
    "past_due",
    "paused",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
)
LIVE = ("trialing", "active", "past_due")

#: slug, name, price in cents, interval, public.
PLANS = (
    ("free", "Free", 0, None, True),
    ("pro", "Pro", 1400, "month", True),
    ("api_hobbyist", "API Hobbyist", 2900, "month", True),
    ("api_pro", "API Pro", 9900, "month", True),
)

#: What each plan allows. None is no limit.
_APP_PAID = {
    "history_quarters": 20,
    "insider_months": None,
    "watchlist_items": None,
    "screener_saved_filters": None,
    "export_enabled": True,
    "alerts_instant": True,
}
ENTITLEMENTS: dict[str, dict[str, int | bool | None]] = {
    "free": {
        "history_quarters": 4,
        "insider_months": 6,
        "watchlist_items": 10,
        "screener_saved_filters": 3,
        "export_enabled": False,
        "alerts_instant": False,
        "api_calls_month": 0,
        "mcp_enabled": False,
    },
    "pro": {**_APP_PAID, "api_calls_month": 0, "mcp_enabled": False},
    "api_hobbyist": {**_APP_PAID, "api_calls_month": 10_000, "mcp_enabled": True},
    "api_pro": {**_APP_PAID, "api_calls_month": 100_000, "mcp_enabled": True},
}


def _sql(value: object) -> str:
    return "NULL" if value is None else f"'{value}'" if isinstance(value, str) else str(value)


def upgrade() -> None:
    op.create_table(
        "plan",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("interval", sa.Text(), nullable=True),
        sa.Column("is_public", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan"),
        sa.UniqueConstraint("slug", name="uq_plan_slug"),
        sa.CheckConstraint("slug ~ '^[a-z][a-z0-9_]*$'", name="slug_is_a_slug"),
        sa.CheckConstraint("price_cents >= 0", name="price_is_not_negative"),
        sa.CheckConstraint("interval IN ('month', 'year')", name="interval_is_known"),
        sa.CheckConstraint("price_cents = 0 OR interval IS NOT NULL", name="paid_has_an_interval"),
    )

    op.create_table(
        "plan_entitlement",
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "key", name="pk_plan_entitlement"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plan.id"], name="fk_plan_entitlement_plan_id_plan", ondelete="CASCADE"
        ),
        sa.CheckConstraint(KEY_CHECK, name="key_is_known"),
        sa.CheckConstraint(VALUE_CHECK, name="value_fits_key"),
    )

    op.create_table(
        "subscription",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grandfathered_plan_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscription"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_subscription_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plan.id"], name="fk_subscription_plan_id_plan", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grandfathered_plan_id"],
            ["plan.id"],
            name="fk_subscription_grandfathered_plan_id_plan",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"status IN ({_quoted(STATUSES)})", name="status_is_known"),
    )
    op.create_index("ix_subscription_user_id", "subscription", ["user_id"])
    op.create_index(
        "uq_subscription_user_live",
        "subscription",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_quoted(LIVE)})"),
    )

    op.create_table(
        "subscription_entitlement",
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("subscription_id", "key", name="pk_subscription_entitlement"),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscription.id"],
            name="fk_subscription_entitlement_subscription_id_subscription",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(KEY_CHECK, name="key_is_known"),
        sa.CheckConstraint(VALUE_CHECK, name="value_fits_key"),
    )

    plans = ",\n            ".join(
        f"({_sql(slug)}, {_sql(name)}, {price}, {_sql(interval)}, {str(public).lower()})"
        for slug, name, price, interval, public in PLANS
    )
    op.execute(f"""
        INSERT INTO plan (slug, name, price_cents, interval, is_public) VALUES
            {plans}
    """)
    rows = ",\n            ".join(
        f"('{slug}', '{key}', '{json.dumps(value)}')"
        for slug, values in ENTITLEMENTS.items()
        for key, value in values.items()
    )
    op.execute(f"""
        INSERT INTO plan_entitlement (plan_id, key, value)
        SELECT plan.id, granted.key, granted.value::jsonb
        FROM (VALUES
            {rows}
        ) AS granted (slug, key, value)
        JOIN plan USING (slug)
    """)


def downgrade() -> None:
    op.drop_table("subscription_entitlement")
    op.drop_index("uq_subscription_user_live", "subscription")
    op.drop_index("ix_subscription_user_id", "subscription")
    op.drop_table("subscription")
    op.drop_table("plan_entitlement")
    op.drop_table("plan")
