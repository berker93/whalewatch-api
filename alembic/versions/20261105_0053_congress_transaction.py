"""congress transaction: the trades in each PTR, read from its PDF

``congress_transaction``: one row per line of a PTR's transaction grid, or one
placeholder for a PTR no line could be read from. See
:class:`~app.db.models.congress_transaction.CongressTransaction`.

``congress_filing`` gains how its PDF was read: where it is served and
archived, its pages, ``text``/``ocr``/``none``, the parser version, when, and
why not. All null until ``ingest-house-ptrs`` reads it.

``admin_action`` gains ``ptr_row_accepted`` and ``ptr_row_rejected``, for the
review queue.

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-05 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0053"
down_revision: str | Sequence[str] | None = "0052"
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
)
ACTIONS_AFTER = (*ACTIONS_BEFORE, "ptr_row_accepted", "ptr_row_rejected")
STATUSES = ("parsed", "needs_review", "placeholder", "rejected")
EXTRACTIONS = ("text", "ocr", "none")
TYPES = ("purchase", "sale", "partial_sale", "exchange")
OWNERS = ("SP", "JT", "DC")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    for name, column in (
        ("document_url", sa.Text()),
        ("document_key", sa.Text()),
        ("pages", sa.SmallInteger()),
        ("extraction", sa.Text()),
        ("parser_version", sa.SmallInteger()),
        ("parsed_at", sa.DateTime(timezone=True)),
        ("parse_error", sa.Text()),
    ):
        op.add_column("congress_filing", sa.Column(name, column, nullable=True))
    op.create_check_constraint(
        "extraction_is_known",
        "congress_filing",
        f"extraction IS NULL OR {_in('extraction', EXTRACTIONS)}",
    )

    op.create_table(
        "congress_transaction",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("filing_id", sa.BigInteger(), nullable=False),
        sa.Column("line", sa.SmallInteger(), nullable=False),
        sa.Column("page", sa.SmallInteger(), nullable=True),
        sa.Column("parse_status", sa.Text(), nullable=False),
        sa.Column("extraction", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("asset_name", sa.Text(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("asset_type", sa.Text(), nullable=True),
        sa.Column("transaction_type", sa.Text(), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("notification_date", sa.Date(), nullable=True),
        sa.Column("amount_low", sa.BigInteger(), nullable=True),
        sa.Column("amount_high", sa.BigInteger(), nullable=True),
        sa.Column("amount_exact", sa.Numeric(14, 2), nullable=True),
        sa.Column("cap_gains_over_200", sa.Boolean(), nullable=True),
        sa.Column("filing_status", sa.Text(), nullable=True),
        sa.Column("amended_id", sa.Text(), nullable=True),
        sa.Column("subholding_of", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column(
            "problems",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("raw_text", sa.Text(), server_default="", nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_congress_transaction"),
        sa.ForeignKeyConstraint(
            ["filing_id"],
            ["congress_filing.id"],
            name="fk_congress_transaction_filing_id_congress_filing",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["admin_user.id"],
            name="fk_congress_transaction_reviewed_by_admin_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("filing_id", "line", name="uq_congress_transaction_filing_id"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(_in("parse_status", STATUSES), name="parse_status_is_known"),
        sa.CheckConstraint(_in("extraction", EXTRACTIONS), name="extraction_is_known"),
        sa.CheckConstraint(_in("transaction_type", TYPES), name="transaction_type_is_known"),
        sa.CheckConstraint(_in("owner", OWNERS), name="owner_is_known"),
        sa.CheckConstraint(
            "parse_status <> 'parsed' OR (asset_name IS NOT NULL AND transaction_type IS NOT "
            "NULL AND transaction_date IS NOT NULL AND (amount_low IS NOT NULL OR amount_exact "
            "IS NOT NULL))",
            name="parsed_is_complete",
        ),
        sa.CheckConstraint(
            "parse_status <> 'placeholder' OR (asset_name IS NULL AND transaction_type IS NULL "
            "AND transaction_date IS NULL AND amount_low IS NULL AND amount_exact IS NULL)",
            name="placeholder_is_empty",
        ),
        sa.CheckConstraint(
            "amount_high IS NULL OR amount_high > amount_low", name="amount_is_a_band"
        ),
        sa.CheckConstraint(
            "amount_exact IS NULL OR (amount_low IS NULL AND amount_exact > 0)",
            name="amount_is_a_band_or_a_figure",
        ),
        sa.CheckConstraint("line >= 1", name="line_is_positive"),
        sa.CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_by IS NULL)", name="review_names_its_admin"
        ),
    )
    op.create_index(
        "ix_congress_transaction_needs_review",
        "congress_transaction",
        ["filing_id"],
        postgresql_where=sa.text("parse_status = 'needs_review'"),
    )

    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_AFTER))


def downgrade() -> None:
    """Loses every trade read from a PTR, and every review of one. The PDFs
    stay archived, so ``ingest-house-ptrs`` reads them again without a fetch;
    the reviews are gone. Refused while the admin log names a review: the log
    is append-only, and the narrower CHECK would not hold."""
    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_BEFORE))
    op.drop_index("ix_congress_transaction_needs_review", table_name="congress_transaction")
    op.drop_table("congress_transaction")
    op.drop_constraint("extraction_is_known", "congress_filing", type_="check")
    for name in (
        "parse_error",
        "parsed_at",
        "parser_version",
        "extraction",
        "pages",
        "document_key",
        "document_url",
    ):
        op.drop_column("congress_filing", name)
