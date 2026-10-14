"""security split

``security_split``: the stock splits detected from the holders' filings, one
row per security per first quarter end after the split, with the exact ratio
and the evidence for it. See :class:`~app.db.models.security_split.SecuritySplit`.

``position_change`` gains ``is_split`` and ``split_ratio``: whether the change
was taken against the previous shares adjusted for a split, and by what ratio.
``is_split`` is false and ``split_ratio`` null on every existing row, which is
what they were computed as.

Created empty, for 0012's reason. Run ``recompute --all`` after upgrading, which
detects the splits and reclassifies the changes across them. ``detect-splits
--all --dry-run`` lists what it would detect, without writing anything, for a
look before five years of ``position_change`` are rewritten.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-14 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.position_change import SPLIT_CHECK, SPLIT_ROW_CHECK
from app.db.models.security_split import RATIO_EXPRESSION, SPLIT_CONFIDENCE_CHECK

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_split",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("numerator", sa.Integer(), nullable=False),
        sa.Column("denominator", sa.Integer(), nullable=False),
        sa.Column(
            "ratio",
            sa.Numeric(precision=20, scale=10),
            sa.Computed(RATIO_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("holders", sa.Integer(), nullable=False),
        sa.Column("holders_agreeing", sa.Integer(), nullable=False),
        sa.Column("holders_exact", sa.Integer(), nullable=False),
        sa.Column("shares_ratio", sa.Numeric(precision=28, scale=6), nullable=False),
        sa.Column("price_ratio", sa.Numeric(precision=28, scale=10), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("security_id", "period_of_report", name="pk_security_split"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_security_split_security_id_security",
            ondelete="CASCADE",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(
            "numerator > 0 AND denominator > 0 AND numerator <> denominator",
            name="ratio_is_a_split",
        ),
        sa.CheckConstraint(SPLIT_CONFIDENCE_CHECK, name="confidence_is_known"),
        sa.CheckConstraint(
            "holders_exact BETWEEN 0 AND holders_agreeing AND holders_agreeing <= holders",
            name="holders_are_consistent",
        ),
    )

    # A constant default: Postgres 11 and on add the column without rewriting
    # the table's rows.
    op.add_column(
        "position_change",
        sa.Column("is_split", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "position_change",
        sa.Column("split_ratio", sa.Numeric(precision=20, scale=10), nullable=True),
    )
    op.create_check_constraint("split_has_a_ratio", "position_change", SPLIT_CHECK)
    op.create_check_constraint("split_against_a_position", "position_change", SPLIT_ROW_CHECK)


def downgrade() -> None:
    """Nothing is lost that ``recompute`` cannot rebuild. The changes go back to
    comparing share counts as filed after the next ``recompute --all``, which is
    what the code before this revision does."""
    op.drop_constraint("split_against_a_position", "position_change", type_="check")
    op.drop_constraint("split_has_a_ratio", "position_change", type_="check")
    op.drop_column("position_change", "split_ratio")
    op.drop_column("position_change", "is_split")
    op.drop_table("security_split")
