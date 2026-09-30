"""amendment no

``filing.amendment_no``: the cover page's ``<amendmentNo>``, which the parser
has always read and the loader had nowhere to put.

Informational, not operational. The ``effective_filing`` views order a
period's amendments by ``filed_at`` and keep doing so — see
:attr:`~app.db.models.filing.Filing.amendment_no` for why the filer's own
sequence number is not trusted with that. What the column buys is the number
everyone else uses to name an amendment, and a way to see a gap in a chain.

Nullable with no default, so adding it is catalogue-only whatever the table's
size. Rows loaded before this revision stay null until they are re-parsed:
``backfill --force`` reads their archived documents again and writes it, with
no EDGAR request. Not backfilled here because the value is in those documents
and nowhere in the database — a migration that fetched from the archive would
be an ingest wearing a schema change's clothes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filing", sa.Column("amendment_no", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    """Nothing is lost that a reparse cannot write back: the number is on every
    amendment's archived cover page."""
    op.drop_column("filing", "amendment_no")
