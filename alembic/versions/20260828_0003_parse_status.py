"""parse status and parse notes

Two columns on ``filing``, and between them they are the record of every
validation guard in :mod:`app.ingestion.normalisation`.

``parse_status`` is ``text`` with a ``CHECK``, not a native enum. The rule this
codebase follows is in :mod:`app.db.models.enums`: an enum for a vocabulary
closed by someone else's rules (``amendment_kind`` is EDGAR's two boxes), text
plus a check for one that is ours. This one is ours and will grow, and
``ALTER TYPE ... ADD VALUE`` is a migration with no reverse — there is no
``DROP VALUE``. Both directions of a check constraint are one statement.

``parse_notes`` is ``jsonb``, which is what makes "which filings did the
implied-price guard fire on" a query rather than a grep. No GIN index on it yet:
the containment queries it is shaped for are an operator's, run over a backfill
at human intervals, and an index that exists for a query nobody has written yet
costs a write on every filing.

The backfill is ``server_default``. ``parse_status`` is ``NOT NULL``, and adding
a NOT NULL column with a constant default to a populated table has been a
catalogue-only operation since Postgres 11 — no table rewrite, no long ACCESS
EXCLUSIVE lock, whatever the table's size when this runs.

One thing deliberately *not* done here: backfilling ``parse_status = 'ok'`` for
rows that already have a ``parsed_at``. Those rows were parsed before any guard
existed, so nothing has ever checked them; calling them ``ok`` would be a claim
this migration is not in a position to make. They stay ``pending`` until
something re-parses them, which is the honest answer and also the one that makes
them easy to find.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.models.filing import (
    PARSE_STATUS_CHECK,
    SUSPECT_HAS_NOTES_CHECK,
    ParseStatus,
)

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "filing",
        sa.Column(
            "parse_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{ParseStatus.PENDING.value}'"),
        ),
    )
    op.add_column("filing", sa.Column("parse_notes", postgresql.JSONB(), nullable=True))

    # Named bare, without the `ck_filing_` prefix, for the reason spelled out at
    # the top of 0002: the `ck` template in NAMING_CONVENTION interpolates
    # %(constraint_name)s and so wraps whatever it is given. A pre-prefixed name
    # lands in Postgres as ck_filing_ck_filing_parse_status_is_known.
    op.create_check_constraint("parse_status_is_known", "filing", PARSE_STATUS_CHECK)
    op.create_check_constraint("a_suspect_filing_says_why", "filing", SUSPECT_HAS_NOTES_CHECK)


def downgrade() -> None:
    """Drops both columns, and with them every guard finding ever recorded.

    The constraints go first. ``DROP COLUMN`` would take them anyway, but naming
    them here is what proves the names in this file are the names in the
    database — a downgrade that silently no-ops on a misspelled constraint is
    the failure this project's downgrade test exists to catch.

    Bare names again, and for a sharper reason than in :func:`upgrade`:
    ``drop_constraint`` runs the name through the same ``ck`` template, so the
    full ``ck_filing_...`` spelling — the one Postgres actually uses, and the
    one you would reach for here — is expanded to
    ``ck_filing_ck_filing_...`` and the drop fails on a constraint that does
    not exist.
    """
    op.drop_constraint("a_suspect_filing_says_why", "filing", type_="check")
    op.drop_constraint("parse_status_is_known", "filing", type_="check")

    op.drop_column("filing", "parse_notes")
    op.drop_column("filing", "parse_status")
