"""filer curation

The columns ``data/investors.yaml`` supplies and ``seed-investors`` writes:
``display_name``, ``manager_name``, ``category``, ``country``, ``notes``.

All nullable, and not as a staging step toward ``NOT NULL``. The curated list
is how a filer acquires these, not what makes a row a filer: a filer inserted
by a test or a future discovery job with only a name and a slug is valid, and
"tracked but not curated" is a state the schema should be able to hold. What
*is* required of a curated entry is enforced where the entry is written — by
the schema in :mod:`app.ingestion.investors`, which fails the seed before it
opens a transaction.

``category`` is text with a check, named bare per the note in
``0002_core_schema``. ``country`` is ``CHAR(2)`` because ISO 3166-1 alpha-2 is
exactly two letters and a three-letter value in there is a bug.

No table rewrite: nullable columns with no default are catalogue-only in
Postgres 11+.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.filer import CATEGORY_CHECK

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("filer", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column("filer", sa.Column("manager_name", sa.Text(), nullable=True))
    op.add_column("filer", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("filer", sa.Column("country", sa.CHAR(length=2), nullable=True))
    op.add_column("filer", sa.Column("notes", sa.Text(), nullable=True))
    op.create_check_constraint("category_is_known", "filer", CATEGORY_CHECK)


def downgrade() -> None:
    """Drops the curation, which ``seed-investors`` can write back in full.

    Unlike ``0004``'s provenance columns, nothing here is lost for good: every
    value came from a file in the repository, and re-running the seed after
    upgrading again restores it.
    """
    # Bare, like the create: drop_constraint runs the name through the same ck
    # template (see 0003), so the prefixed spelling would miss.
    op.drop_constraint("category_is_known", "filer", type_="check")
    op.drop_column("filer", "notes")
    op.drop_column("filer", "country")
    op.drop_column("filer", "category")
    op.drop_column("filer", "manager_name")
    op.drop_column("filer", "display_name")
