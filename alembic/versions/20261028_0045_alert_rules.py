"""alert rules: a plan's number of them, and a threshold only where there are dollars

Adds ``alert_rules`` to the entitlement keys: how many rules an account keeps
on at once. Free keeps one, the season digest, the only kind it is sent
(``alerts_instant`` is off); every paid plan keeps as many as it likes. A plan
made by hand since 0037 gets Free's, as 0039 gave it Free's MCP calls.
Changing them is an UPDATE.

``alert_rule.min_value`` is now null for a kind whose events are not a number
of dollars: a filing, and a season's digest, have nothing to compare it with.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-28 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
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
    "mcp_calls_month",
)
NEW_LIMIT = "alert_rules"

#: What each plan allows of the new key, by slug, as JSON: null is no limit.
ALERT_RULES = {"free": "1", "pro": "null", "api_hobbyist": "null", "api_pro": "null"}

ENTITLEMENT_TABLES = ("plan_entitlement", "subscription_entitlement")

THRESHOLD_KINDS = ("filer_position_change", "stock_ownership_change", "insider_cluster")


def _quoted(keys: Sequence[str]) -> str:
    return ", ".join(f"'{key}'" for key in keys)


def _key_check(keys: Sequence[str]) -> str:
    return f"key IN ({_quoted(keys)})"


def upgrade() -> None:
    for table in ENTITLEMENT_TABLES:
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint("key_is_known", table, _key_check((*LIMITS, NEW_LIMIT, *FLAGS)))
    rows = ", ".join(f"('{slug}', '{value}'::jsonb)" for slug, value in ALERT_RULES.items())
    # Every plan has every key, or the catalog refuses the lot.
    op.execute(f"""
        INSERT INTO plan_entitlement (plan_id, key, value)
        SELECT plan.id, '{NEW_LIMIT}', coalesce(granted.value, '{ALERT_RULES["free"]}'::jsonb)
        FROM plan
        LEFT JOIN (VALUES {rows}) AS granted (slug, value) USING (slug)
    """)

    # No rule has had a reason to set one on these kinds yet: nothing sets
    # any rule. Cleared rather than refused all the same.
    op.execute(f"""
        UPDATE alert_rule SET min_value = NULL
        WHERE min_value IS NOT NULL AND kind NOT IN ({_quoted(THRESHOLD_KINDS)})
    """)
    op.create_check_constraint(
        "min_value_has_dollars_to_compare",
        "alert_rule",
        f"min_value IS NULL OR kind IN ({_quoted(THRESHOLD_KINDS)})",
    )


def downgrade() -> None:
    op.drop_constraint("min_value_has_dollars_to_compare", "alert_rule", type_="check")
    for table in ENTITLEMENT_TABLES:
        op.execute(f"DELETE FROM {table} WHERE key = '{NEW_LIMIT}'")
        op.drop_constraint("key_is_known", table, type_="check")
        op.create_check_constraint("key_is_known", table, _key_check((*LIMITS, *FLAGS)))
