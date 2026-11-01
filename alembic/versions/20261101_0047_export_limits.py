"""export limits: a plan's rows per export, and its exports a month, counted

Adds two limits to the entitlement keys. ``export_rows`` is the most rows one
export may hold: a table with more is refused, 402, and names the plan that
takes it. ``exports_month`` is how many exports an account takes in a quota
month: none on Free, which has no export, and no limit on every paid plan,
where the hourly abuse limit is the only bound. A plan made by hand since 0037
gets Free's of both. Changing them is an UPDATE.

``usage_counter`` counts ``exports_month`` beside the API and MCP calls, so
the account page shows the month's exports as it shows the month's calls.

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-01 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047"
down_revision: str | Sequence[str] | None = "0046"
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
    "alert_rules",
    "api_calls_month",
    "mcp_calls_month",
)
METERED = ("api_calls_month", "mcp_calls_month")

#: What each plan allows of each new key, by slug, as JSON: null is no limit.
#: The paid plans' rows are placeholders, as the API plans' calls are.
NEW_LIMITS = {
    "export_rows": {
        "free": "0",
        "pro": "100000",
        "api_hobbyist": "100000",
        "api_pro": "1000000",
    },
    "exports_month": {"free": "0", "pro": "null", "api_hobbyist": "null", "api_pro": "null"},
}
NEW_METERED = "exports_month"

ENTITLEMENT_TABLES = ("plan_entitlement", "subscription_entitlement")


def _quoted(keys: Sequence[str]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


def _key_check(keys: Sequence[str]) -> str:
    return f"key IN ({_quoted(keys)})"


def upgrade() -> None:
    for table in ENTITLEMENT_TABLES:
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint(
            "key_is_known", table, _key_check((*LIMITS, *NEW_LIMITS, *FLAGS))
        )
    for key, values in NEW_LIMITS.items():
        rows = ", ".join(f"('{slug}', '{value}'::jsonb)" for slug, value in values.items())
        # Every plan has every key, or the catalog refuses the lot.
        op.execute(f"""
            INSERT INTO plan_entitlement (plan_id, key, value)
            SELECT plan.id, '{key}', coalesce(granted.value, '{values["free"]}'::jsonb)
            FROM plan
            LEFT JOIN (VALUES {rows}) AS granted (slug, value) USING (slug)
        """)

    op.drop_constraint("key_is_metered", "usage_counter", type_="check")
    op.create_check_constraint(
        "key_is_metered", "usage_counter", _key_check((*METERED, NEW_METERED))
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM usage_counter WHERE key = '{NEW_METERED}'")
    op.drop_constraint("key_is_metered", "usage_counter", type_="check")
    op.create_check_constraint("key_is_metered", "usage_counter", _key_check(METERED))
    for table in ENTITLEMENT_TABLES:
        op.execute(f"DELETE FROM {table} WHERE key IN ({_quoted(tuple(NEW_LIMITS))})")
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint("key_is_known", table, _key_check((*LIMITS, *FLAGS)))
