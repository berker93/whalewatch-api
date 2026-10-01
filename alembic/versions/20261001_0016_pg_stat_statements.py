"""pg_stat_statements

The extension that records, per normalised statement, how often it ran, how
long it took in total and at worst, and how many buffers it hit and read. It is
what finds the query worth an ``EXPLAIN``: the one with the largest total time,
not the one that felt slow once. See docs/query-performance.md.

Creating the extension only adds the ``pg_stat_statements`` view and its
functions. The statistics are collected by the library, which the server loads
at start (``shared_preload_libraries``, set in docker-compose.yml). A server
without it lets this migration run, and every read of the view fails with an
error that says so. Managed Postgres (RDS, Cloud SQL, Neon) preloads it already.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01 23:58:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS: an operator may have created it by hand to look at a
    # problem before this migration reached the database.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    """Drops the view and the statistics gathered so far. The server goes on
    collecting while the library is loaded, and nothing in the schema reads them."""
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
