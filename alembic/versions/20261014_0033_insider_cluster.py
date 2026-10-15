"""insider cluster

``insider_cluster``: the episodes of cluster buying found in
``insider_transaction``, several of an issuer's officers and directors buying on
the open market within a window, for each window and threshold the API offers.
See :class:`~app.db.models.insider_cluster.InsiderCluster`.

Created empty. Run ``detect-clusters`` after upgrading, or let the next
``ingest-form4`` that loads anything do it.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-14 23:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "insider_cluster",
        sa.Column("window_days", sa.SmallInteger(), nullable=False),
        sa.Column("min_insiders", sa.SmallInteger(), nullable=False),
        sa.Column("issuer_cik", sa.CHAR(length=10), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("issuer_name", sa.Text(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=True),
        sa.Column("insider_count", sa.Integer(), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("total_shares", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("total_value", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("transaction_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "window_days",
            "min_insiders",
            "issuer_cik",
            "window_start",
            name="pk_insider_cluster",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_insider_cluster_security_id_security",
            ondelete="SET NULL",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("window_days > 0", name="window_days_is_positive"),
        sa.CheckConstraint("min_insiders >= 2", name="min_insiders_is_a_cluster"),
        sa.CheckConstraint("window_end >= window_start", name="window_is_ordered"),
        sa.CheckConstraint(
            "insider_count >= min_insiders AND transaction_count >= insider_count",
            name="counts_are_consistent",
        ),
        sa.CheckConstraint(
            "cardinality(transaction_ids) = transaction_count",
            name="transaction_ids_are_counted",
        ),
    )
    op.create_index(
        "ix_insider_cluster_window_end",
        "insider_cluster",
        ["window_days", "min_insiders", sa.text("window_end DESC"), sa.text("issuer_cik DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_insider_cluster_window_end", table_name="insider_cluster")
    op.drop_table("insider_cluster")
