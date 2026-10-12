"""sec 13f security

``sec_13f_security``: the SEC's Official List of Section 13(f) Securities, one
row per CUSIP per quarter, written by ``import-13f-list``. Every quarter's list
is kept. See :class:`~app.db.models.sec_13f_security.Sec13FSecurity`.

``quarter`` is generated from ``period_of_report`` by the expression
``filing.quarter`` uses, added by raw DDL for the reason given in 0002.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.filing import QUARTER_EXPRESSION
from app.db.models.sec_13f_security import STATUS_CHECK

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sec_13f_security",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("cusip", sa.CHAR(9), nullable=False),
        sa.Column("issuer_name", sa.Text(), nullable=False),
        sa.Column("issuer_description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("has_listed_options", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sec_13f_security"),
        sa.UniqueConstraint(
            "period_of_report", "cusip", name="uq_sec_13f_security_period_of_report"
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(STATUS_CHECK, name="status_is_known"),
        sa.CheckConstraint("issuer_name <> ''", name="issuer_name_is_not_empty"),
    )
    op.execute(
        sa.text(
            "ALTER TABLE sec_13f_security "
            f"ADD COLUMN quarter text GENERATED ALWAYS AS ({QUARTER_EXPRESSION}) STORED"
        )
    )
    op.create_index(
        "ix_sec_13f_security_cusip_period_of_report",
        "sec_13f_security",
        ["cusip", sa.text("period_of_report DESC")],
    )


def downgrade() -> None:
    """Drops every imported list. The archived files are untouched, so
    ``import-13f-list`` for each quarter rebuilds them without EDGAR."""
    op.drop_index("ix_sec_13f_security_cusip_period_of_report", table_name="sec_13f_security")
    op.drop_table("sec_13f_security")
