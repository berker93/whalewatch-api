"""stock lookup

What ``/v1/stocks/{ticker}`` needs to find a stock, and to read one across
every period.

* ``security_alias``: former tickers and other spellings, each pointing at a
  security. Checked after ``security.ticker``. Not unique, since tickers are
  recycled; the lookup takes the security held most recently. Nothing writes
  it yet: enrichment will, and an operator can by hand.
* ``security (upper(ticker) text_pattern_ops)``. The lookup ignores case, so
  it compares ``upper(ticker)``, and a 404 suggests tickers that start with
  what was asked for. ``text_pattern_ops`` serves the prefix under any
  collation as well as the equality. docs/query-performance.md said this one
  would be needed once tickers resolve; it is here now so the lookup is never
  a sequential scan of ``security``. Followed by ``ANALYZE security``, which
  an expression index needs before the planner will use it well.
* ``position_snapshot (security_id, period_of_report)``. One stock in every
  period, for its ownership history and for when it was last held, was a
  parallel sequential scan of 470,000 rows: 65 ms on the dev database. The
  0017 index leads with the period, which that query does not have.

Built without ``CONCURRENTLY``, for 0017's reason.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-03 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_alias",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("alias <> ''", name="alias_is_not_empty"),
        sa.CheckConstraint(
            "source IS NULL OR source IN ('openfigi', '13f_column', 'manual')",
            name="source_is_known",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_security_alias_security_id_security",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_security_alias"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_security_alias_upper_alias_security_id "
        "ON security_alias (upper(alias) text_pattern_ops, security_id)"
    )
    op.execute("CREATE INDEX ix_security_upper_ticker ON security (upper(ticker) text_pattern_ops)")
    # An expression index has no statistics until the table is next analysed,
    # and autovacuum may not get to that for a long time on a table this
    # quiet. Until then the planner guesses 0.5% of security matches
    # upper(ticker) = :key, and the lookup hash-joins a scan of every security
    # (1.5 ms) instead of probing two indexes (0.06 ms). 20,000 rows: instant.
    op.execute("ANALYZE security")
    op.create_index(
        "ix_position_snapshot_security_id_period_of_report",
        "position_snapshot",
        ["security_id", "period_of_report"],
    )


def downgrade() -> None:
    """Drops the aliases with their table, and both indexes. Nothing else
    reads them, so the lookup falls back to ``security.ticker`` and a CUSIP."""
    op.drop_index(
        "ix_position_snapshot_security_id_period_of_report", table_name="position_snapshot"
    )
    op.drop_index("ix_security_upper_ticker", table_name="security")
    op.drop_table("security_alias")
