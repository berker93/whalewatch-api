"""congress assets: what each PTR line traded, and a person's override

``congress_transaction`` gains ``asset_class``, ``security_id``,
``resolution_method``, ``resolution_confidence``, ``asset_key`` and
``resolved_at``: the class of the asset a line names, and for a stock (or an
option on one) the ``security`` it is. ``congress_asset_override``: a person's
class and security for every line under one asset key. See
:mod:`app.ingestion.congress.asset_resolution`.

``admin_action`` gains ``asset_overridden`` and ``asset_override_removed``.

Every line starts unresolved: ``resolve-congress-assets`` resolves them.

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-06 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported: a migration says what the schema was when
# it ran, whatever the models say later.
ACTIONS_BEFORE = (
    "login",
    "logout",
    "override_set",
    "override_ended",
    "grandfathered",
    "ungrandfathered",
    "admin_created",
    "admin_disabled",
    "ptr_row_accepted",
    "ptr_row_rejected",
    "member_matched",
    "member_rejected",
)
ACTIONS_AFTER = (*ACTIONS_BEFORE, "asset_overridden", "asset_override_removed")
ASSET_CLASSES = (
    "equity",
    "stock_option",
    "municipal_bond",
    "treasury",
    "agency_bond",
    "corporate_bond",
    "mutual_fund",
    "crypto",
    "real_estate",
    "private",
    "other",
    "unknown",
)
OVERRIDE_CLASSES = tuple(kind for kind in ASSET_CLASSES if kind != "unknown")
WITH_SECURITY = ("equity", "stock_option")
METHODS = ("ticker", "former_ticker", "name", "manual")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _now() -> sa.TextClause:
    return sa.text("now()")


TRANSACTION_CHECKS = {
    "asset_class_is_known": f"asset_class IS NULL OR {_in('asset_class', ASSET_CLASSES)}",
    "resolution_method_is_known": (
        f"resolution_method IS NULL OR {_in('resolution_method', METHODS)}"
    ),
    "security_is_a_stock": f"security_id IS NULL OR {_in('asset_class', WITH_SECURITY)}",
    "a_security_says_how": "security_id IS NULL OR resolution_method IS NOT NULL",
    "confidence_is_the_securitys": "(resolution_confidence IS NULL) = (security_id IS NULL)",
    "confidence_is_a_fraction": (
        "resolution_confidence IS NULL OR resolution_confidence BETWEEN 0 AND 1"
    ),
    "resolved_has_a_class": "(resolved_at IS NULL) = (asset_class IS NULL)",
}


def upgrade() -> None:
    op.add_column("congress_transaction", sa.Column("asset_class", sa.Text(), nullable=True))
    op.add_column(
        "congress_transaction",
        sa.Column(
            "security_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "security.id",
                name="fk_congress_transaction_security_id_security",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
    )
    op.add_column("congress_transaction", sa.Column("resolution_method", sa.Text(), nullable=True))
    op.add_column(
        "congress_transaction",
        sa.Column("resolution_confidence", sa.Numeric(4, 3), nullable=True),
    )
    op.add_column("congress_transaction", sa.Column("asset_key", sa.Text(), nullable=True))
    op.add_column(
        "congress_transaction",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, condition in TRANSACTION_CHECKS.items():
        op.create_check_constraint(name, "congress_transaction", condition)
    op.create_index(
        "ix_congress_transaction_security",
        "congress_transaction",
        ["security_id", "transaction_date"],
        postgresql_where=sa.text("security_id IS NOT NULL"),
    )
    op.create_index("ix_congress_transaction_asset_key", "congress_transaction", ["asset_key"])

    op.create_table(
        "congress_asset_override",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("asset_key", sa.Text(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column(
            "security_id",
            sa.BigInteger(),
            sa.ForeignKey("security.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint("asset_key"),
        sa.CheckConstraint(_in("asset_class", OVERRIDE_CLASSES), name="asset_class_is_known"),
        sa.CheckConstraint(
            f"({_in('asset_class', WITH_SECURITY)}) = (security_id IS NOT NULL)",
            name="a_stock_names_its_security",
        ),
        sa.CheckConstraint("asset_key <> ''", name="asset_key_is_not_empty"),
    )

    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_AFTER))


def downgrade() -> None:
    """Loses every line's resolution, which a run rebuilds, and every override,
    which it cannot. Refused while the admin log names an override: the log is
    append-only, and the narrower CHECK would not hold."""
    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_BEFORE))

    op.drop_table("congress_asset_override")
    op.drop_index("ix_congress_transaction_asset_key", table_name="congress_transaction")
    op.drop_index("ix_congress_transaction_security", table_name="congress_transaction")
    for name in TRANSACTION_CHECKS:
        op.drop_constraint(name, "congress_transaction", type_="check")
    for column in (
        "resolved_at",
        "asset_key",
        "resolution_confidence",
        "resolution_method",
        "security_id",
        "asset_class",
    ):
        op.drop_column("congress_transaction", column)
