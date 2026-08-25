"""baseline

The root of the migration chain, and deliberately empty. There are no models
yet, so there is nothing here to create; what this revision provides is a
``down_revision`` for the first real migration to hang off, and a revision that
``alembic stamp 0001`` can name when adopting a database that was built by hand
before migrations existed.

Do not add DDL here later. Once this has been applied anywhere, editing it means
the applied schema and the file disagree, and nothing will ever tell you.

Revision ID: 0001
Revises:
Create Date: 2026-08-25 06:54:42.592626

"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema. The empty database *is* the baseline."""
    pass


def downgrade() -> None:
    """Nothing to undo; :func:`upgrade` created nothing.

    This is the one migration in the project allowed a bare ``pass``, because it
    is the root: downgrading past it means "no schema at all", which is where an
    empty upgrade already leaves you. Every revision with a ``down_revision``
    must implement a real downgrade — tests/test_migrations.py enforces it.
    """
    pass
