"""congress filing: Periodic Transaction Reports, from each chamber's index

``congress_filing``: one row per PTR a member of Congress filed, keyed on
``(chamber, doc_id)``, from the House Clerk's yearly index to begin with. See
:class:`~app.db.models.congress_filing.CongressFiling`.

Created empty. ``ingest-house-index`` fills it, and moves the
``crawl_watermark`` row ``ingest-house-index``, which needs no schema of its
own.

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-05 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "congress_filing",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("chamber", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("prefix", sa.Text(), nullable=True),
        sa.Column("suffix", sa.Text(), nullable=True),
        sa.Column("office", sa.Text(), nullable=True),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_at",
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
        sa.PrimaryKeyConstraint("id", name="pk_congress_filing"),
        sa.UniqueConstraint("chamber", "doc_id", name="uq_congress_filing_chamber"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("chamber IN ('house', 'senate')", name="chamber_is_known"),
        sa.CheckConstraint("report_type IN ('ptr')", name="report_type_is_known"),
        sa.CheckConstraint("doc_id <> ''", name="doc_id_is_not_empty"),
    )
    op.create_index(
        "ix_congress_filing_chamber_filing_date", "congress_filing", ["chamber", "filing_date"]
    )


def downgrade() -> None:
    """Loses every PTR read from an index, and with the watermark row the place
    the daily run starts from: the next run needs ``--from-year`` first. The
    archived ZIPs are kept, but are re-fetched rather than re-read."""
    op.drop_index("ix_congress_filing_chamber_filing_date", table_name="congress_filing")
    op.drop_table("congress_filing")
    op.execute("DELETE FROM crawl_watermark WHERE job = 'ingest-house-index'")
