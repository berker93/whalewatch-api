"""saved filter alerts: an alert rule about one of the user's saved filters

A new kind, ``saved_filter_match``: a 13F arrived with positions that pass a
saved screener filter. Its rule names the filter in
``alert_rule.saved_filter_id``, set for that kind and no other (a ``CHECK``),
and the foreign key is on the filter and the rule's owner together, so a rule
can only be about its owner's own filter; deleting the filter deletes the
rule, and what it had waiting in the outbox with it. ``saved_filter`` gains
the unique key on ``(id, user_id)`` that the foreign key needs.

One rule per kind and target per user, as before, with the filter now part of
the target. The three ``kind`` checks (rule, notification, outbox) take the
new kind.

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-02 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "filer_filed",
    "filer_position_change",
    "stock_ownership_change",
    "insider_cluster",
    "season_digest",
)
_NEW = "saved_filter_match"
_KIND_TABLES = ("alert_rule", "notification", "alert_outbox")


def _kind_check(kinds: Sequence[str]) -> str:
    return "kind IN ({})".format(", ".join(f"'{kind}'" for kind in kinds))


def _set_kinds(kinds: Sequence[str]) -> None:
    for table in _KIND_TABLES:
        # Named as the models name them: the naming convention adds ck_<table>_.
        op.drop_constraint("kind_is_known", table, type_="check")
        op.create_check_constraint("kind_is_known", table, _kind_check(kinds))


def upgrade() -> None:
    op.create_unique_constraint("uq_saved_filter_id_user_id", "saved_filter", ["id", "user_id"])
    op.add_column("alert_rule", sa.Column("saved_filter_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_alert_rule_saved_filter_id_saved_filter",
        "alert_rule",
        "saved_filter",
        ["saved_filter_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    _set_kinds((*_KINDS, _NEW))
    op.create_check_constraint(
        "saved_filter_kinds_name_a_saved_filter",
        "alert_rule",
        f"(saved_filter_id IS NOT NULL) = (kind IN ('{_NEW}'))",
    )
    op.drop_index("uq_alert_rule_user_id_kind_target", "alert_rule")
    op.create_index(
        "uq_alert_rule_user_id_kind_target",
        "alert_rule",
        ["user_id", "kind", "filer_id", "security_id", "saved_filter_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "ix_alert_rule_saved_filter_id",
        "alert_rule",
        ["saved_filter_id", "user_id"],
        postgresql_where=sa.text("saved_filter_id IS NOT NULL"),
    )


def downgrade() -> None:
    # The new kind's rows go first: no check of the old kinds would hold them.
    op.execute(f"DELETE FROM alert_outbox WHERE kind = '{_NEW}'")
    op.execute(f"DELETE FROM notification WHERE kind = '{_NEW}'")
    op.execute(f"DELETE FROM alert_rule WHERE kind = '{_NEW}'")
    op.drop_index("ix_alert_rule_saved_filter_id", "alert_rule")
    op.drop_index("uq_alert_rule_user_id_kind_target", "alert_rule")
    op.create_index(
        "uq_alert_rule_user_id_kind_target",
        "alert_rule",
        ["user_id", "kind", "filer_id", "security_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint("saved_filter_kinds_name_a_saved_filter", "alert_rule", type_="check")
    _set_kinds(_KINDS)
    op.drop_constraint(
        "fk_alert_rule_saved_filter_id_saved_filter", "alert_rule", type_="foreignkey"
    )
    op.drop_column("alert_rule", "saved_filter_id")
    op.drop_constraint("uq_saved_filter_id_user_id", "saved_filter", type_="unique")
