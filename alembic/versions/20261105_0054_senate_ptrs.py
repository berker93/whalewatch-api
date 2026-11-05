"""senate ptrs: eFD's reports, and the html they are read from

``congress_filing`` gains what eFD's titles say of a Senate report: the day it
is "for" (``report_date``, the original's on an amendment) and which amendment
it is (``amendment``, null on an original). Both null on a House filing.

``extraction`` gains ``html`` on both tables, for a row read from the table of
a report filed online at eFD. See :mod:`app.ingestion.congress.senate_ptr`.

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-05 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | Sequence[str] | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported: a migration says what the schema was when
# it ran, whatever the models say later.
EXTRACTIONS_BEFORE = ("text", "ocr", "none")
EXTRACTIONS_AFTER = ("text", "ocr", "html", "none")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _extraction(values: Sequence[str]) -> None:
    for table, nullable in (("congress_filing", True), ("congress_transaction", False)):
        check = _in("extraction", values)
        op.drop_constraint("extraction_is_known", table, type_="check")
        op.create_check_constraint(
            "extraction_is_known", table, f"extraction IS NULL OR {check}" if nullable else check
        )


def upgrade() -> None:
    op.add_column("congress_filing", sa.Column("report_date", sa.Date(), nullable=True))
    op.add_column("congress_filing", sa.Column("amendment", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "amendment_is_positive", "congress_filing", "amendment IS NULL OR amendment >= 1"
    )
    _extraction(EXTRACTIONS_AFTER)


def downgrade() -> None:
    """Loses every Senate row read from HTML, and what eFD's titles said. The
    pages stay archived, so ``ingest-senate-ptrs --reparse`` reads them again
    without a fetch."""
    op.execute(
        "DELETE FROM congress_transaction WHERE filing_id IN "
        "(SELECT id FROM congress_filing WHERE extraction = 'html')"
    )
    op.execute(
        "UPDATE congress_filing SET extraction = NULL, parser_version = NULL, parsed_at = NULL "
        "WHERE extraction = 'html'"
    )
    _extraction(EXTRACTIONS_BEFORE)
    op.drop_constraint("amendment_is_positive", "congress_filing", type_="check")
    op.drop_column("congress_filing", "amendment")
    op.drop_column("congress_filing", "report_date")
