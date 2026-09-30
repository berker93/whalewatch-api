"""pending filing

The ingestion work queue: every 13F discovery has found that is not loaded,
with what ingesting it has come to. ``discover-filings`` writes it,
``ingest-filing`` reads its CIK and records each attempt. See
:mod:`app.db.models.pending_filing`.

Keyed on ``accession_no`` with no surrogate: nothing references this table and
every write to it conflicts on that column. No foreign key to ``filing``
either — a queued filing has no ``filing`` row yet, which is the point of it.

No secondary indexes. The table holds a few thousand rows for the whole
tracked universe, and every question asked of it — "what is failed", "what is
this CIK's backlog" — is a scan of that.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.pending_filing import PENDING_STATUS_CHECK

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_filing",
        sa.Column("accession_no", sa.CHAR(length=20), nullable=False),
        sa.Column("cik", sa.CHAR(length=10), nullable=False),
        sa.Column("form_type", sa.Text(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("accession_no", name="pk_pending_filing"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(PENDING_STATUS_CHECK, name="status_is_known"),
        sa.CheckConstraint("attempts >= 0", name="attempts_is_not_negative"),
    )


def downgrade() -> None:
    """Drops the queue. Nothing in it is lost for good: ``discover-filings``
    rebuilds every unloaded row from EDGAR, and only the attempt history of
    filings that failed is gone."""
    op.drop_table("pending_filing")
