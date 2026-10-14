"""form4 loader

What the insider loader needs that 0028 did not have.

``filing.date_of_original_submission``: a 4/A's ``dateOfOriginalSubmission``,
the only thing a 4/A says about which Form 4 it amends. Stored so that an
original loaded *after* its amendment can still find the amendment, and be
superseded by it, rather than putting the corrected trades back beside the
corrections.

``ix_reporting_owner_insider_id``: the insider loader keeps ``insider.name``
as the latest-filed filing spelled it, which looks up an insider's filings by
insider. The ``RESTRICT`` foreign key from ``insider`` checks the same column.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-14 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "filing",
        sa.Column("date_of_original_submission", sa.Date(), nullable=True),
    )
    op.create_index("ix_reporting_owner_insider_id", "reporting_owner", ["insider_id"])


def downgrade() -> None:
    """Loses which Form 4 each unlinked 4/A amends. Re-loading the archived
    4/As restores it."""
    op.drop_index("ix_reporting_owner_insider_id", table_name="reporting_owner")
    op.drop_column("filing", "date_of_original_submission")
