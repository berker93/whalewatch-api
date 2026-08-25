"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Describe what this migration does, and why, in the docstring above.

    If autogenerate wrote this body, read it before committing. It cannot see
    generated columns, native enum changes, partitions, materialised views or
    anything in raw DDL, and it renders a renamed column as a drop plus an add
    — which is data loss with a green test suite.
    """
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Undo :func:`upgrade`, exactly.

    Not ``pass``. A migration you cannot reverse is a deploy you cannot roll
    back, and the moment you need one is the moment you cannot test writing it.
    Where the reverse genuinely loses data (a dropped column), say so here and
    restore the structure anyway — an empty column beats a failed downgrade
    that strands the database between two revisions.
    """
    ${downgrades if downgrades else "pass"}
