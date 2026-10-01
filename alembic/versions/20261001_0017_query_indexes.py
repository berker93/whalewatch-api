"""query indexes

The indexes the API's queries turned out to need, and one they turned out not
to, measured with ``EXPLAIN (ANALYZE, BUFFERS)`` on the full backfill. The plans
before and after, and the experiments that decided each one, are in
docs/query-performance.md.

* ``position_snapshot (period_of_report, security_id)``. "The latest period we
  hold", which every request without ``?period=`` asks first, was a parallel
  sequential scan of 1.4 million rows: 20 ms to 40 ms. It is now a backward
  index-only scan that reads one entry. Period first, so ``max()`` can use it;
  "who holds this stock in this period" uses both columns either way.
* ``security USING gin (name gin_trgm_ops)``. ``/stocks?q=`` searches names
  with ``ILIKE '%...%'``, which no B-tree can serve: a sequential scan of every
  security, 7 ms. Trigrams make it a bitmap index scan, 0.04 ms. GIN rather than
  GiST, which was 17 times slower here. On ``security.name`` because the
  ``issuer`` table the data model puts this index on does not exist yet.
* ``position_change (period_of_report, security_id) WHERE action <> 'hold'``.
  "Who added, trimmed, opened or exited this stock" never asks for a hold, so
  the index leaves them out. That saves little, since holds are 6.5% of
  changes, and nothing else could use the index.
* Drops ``holding (filer_id, period_of_report)``. No query may use it: every
  per-filer read goes through ``effective_filing`` by ``filing_id`` (see
  app/db/queries/effective.py), and the per-filer read path moved to
  ``position_snapshot`` in 0009. It was scanned zero times by a full backfill,
  ``recompute --all``, ``check-data``, both audits and every API query, and it
  took 14 MB and a write on every one of 1.9 million inserts.

Built without ``CONCURRENTLY``: Alembic runs a migration in a transaction, and
``CREATE INDEX CONCURRENTLY`` cannot run in one. Each build locks its table
against writes for a few seconds, which nothing serving reads notices.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01 23:59:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # scripts/init-db.sql creates it in the compose database. Every other
    # database, the test container's and production's, gets it here, before
    # the first index that needs it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_position_snapshot_period_of_report_security_id",
        "position_snapshot",
        ["period_of_report", "security_id"],
    )
    op.create_index(
        "ix_security_name_trgm",
        "security",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_position_change_period_of_report_security_id_not_hold",
        "position_change",
        ["period_of_report", "security_id"],
        postgresql_where=sa.text("action <> 'hold'"),
    )
    op.drop_index("ix_holding_filer_id_period_of_report", table_name="holding")


def downgrade() -> None:
    """Puts the indexes back as they were. pg_trgm stays: init-db.sql may have
    created it before this migration ran, and dropping it would take that with it."""
    op.create_index(
        "ix_holding_filer_id_period_of_report", "holding", ["filer_id", "period_of_report"]
    )
    op.drop_index(
        "ix_position_change_period_of_report_security_id_not_hold", table_name="position_change"
    )
    op.drop_index("ix_security_name_trgm", table_name="security")
    op.drop_index(
        "ix_position_snapshot_period_of_report_security_id", table_name="position_snapshot"
    )
