"""ingest provenance

Three columns on ``filing``, and between them they answer "where did this row
come from, and when did we last touch it" — the questions asked the moment a
backfill produces a number nobody believes.

``raw_key`` and ``source_url`` are the interim form of the ``raw_document_id``
in docs/data-model.md. A ``raw_document`` table earns its place when a filing
has several archived documents worth describing separately, each with its own
size, hash and fetch time. Until then two nullable text columns hold what the
loader is actually handed, and the eventual table is a backfill from them rather
than a re-crawl of EDGAR at 10 requests a second.

``ingested_at`` is what makes the upsert's ``DO UPDATE`` legible after the fact.
Without it a re-ingest is invisible: the row changes in place and nothing on it
says when, so "which filings did the run I started an hour ago rewrite" has no
answer. ``parsed_at`` does not answer it — a re-parse and a re-load are
different events, and the interesting one here is the write.

``NOT NULL`` with a ``now()`` default on ``ingested_at``. Postgres 11+ stores a
constant default in the catalogue rather than rewriting the table, but
``now()`` is not constant, so this one *does* rewrite. Deliberate, and cheap
today: the table is empty on every environment this will run against, and the
alternative — nullable, backfilled, then set NOT NULL — buys nothing for a
column whose whole point is that every row has a value.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filing", sa.Column("raw_key", sa.Text(), nullable=True))
    op.add_column("filing", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column(
        "filing",
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Drops all three, and with them every pointer to an archived document.

    Reversible in the schema sense and not in the operational one: the keys
    cannot be recomputed from anything left behind, because the archive layout
    is the fetcher's business and not a function of the accession number. A
    downgrade past this revision is a re-fetch of every filing that has been
    ingested since it was applied.
    """
    op.drop_column("filing", "ingested_at")
    op.drop_column("filing", "source_url")
    op.drop_column("filing", "raw_key")
