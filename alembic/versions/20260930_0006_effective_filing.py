"""effective filing

Which filings count for a ``(filer, period)``. Everything is loaded as filed;
these two views decide what a read adds up, so that amendments and a filer's
overlapping CIKs are resolved once, in one place, instead of in every query
that sums holdings.

``effective_filing_by_cik`` — within one CIK's filings for a period:

* the latest-filed *whole-period* filing counts: the original ``13F-HR``, or a
  ``13F-HR/A`` whose ``amendment_kind`` is ``restatement``, which replaces it;
* every ``new_holdings`` amendment filed *after* that one counts too, on top
  of it — positions released from confidential treatment, typically;
* a ``new_holdings`` amendment filed before a later restatement does not: the
  restatement is the whole period, so it already includes or drops them;
* only filings whose holdings are loaded (``parse_status`` ``ok`` or
  ``suspect``); ``pending`` and ``failed`` have none, and a ``13F-NT`` has none
  by definition;
* not an amendment whose kind is unknown (no ``<amendmentType>`` on the cover
  page). Adding it risks doubling the period and substituting it risks
  dropping most of it, and neither is a guess worth making silently. It stays
  visible through ``GET /filings/{accession_no}``.

``effective_filing`` — across a filer's CIKs, by ``filer.overlap``:

* ``successor``: only the highest-``priority`` CIK that has anything for the
  period counts (ties broken by CIK, so a period never resolves to two by
  accident);
* ``sum``: every CIK counts.

Ordering is by ``filed_at`` then ``accession_no`` rather than by ``amends_id``,
which nothing sets during ingestion yet — and would only restate what the
dates already say.

Plain views, not materialised: they are cheap over the ``(filer_id,
period_of_report)`` index, Postgres pushes a ``filer_id`` / period predicate
through the window functions' partitions, and a view cannot go stale. The
materialised aggregates in Epic 3 read *from* them.

The SQL is written out here rather than imported: a view definition is
history, and a later change is a later migration with ``CREATE OR REPLACE``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.filer import OVERLAP_CHECK

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EFFECTIVE_FILING_BY_CIK = """
CREATE VIEW effective_filing_by_cik AS
WITH candidate AS (
    SELECT
        id,
        filer_id,
        cik,
        period_of_report,
        filed_at,
        accession_no,
        upper(form_type) = '13F-HR' OR amendment_kind = 'restatement' AS replaces_period
    FROM filing
    WHERE filer_id IS NOT NULL
      AND parse_status IN ('ok', 'suspect')
      AND (
          upper(form_type) = '13F-HR'
          OR (upper(form_type) = '13F-HR/A' AND amendment_kind IS NOT NULL)
      )
),
base AS (
    SELECT DISTINCT ON (filer_id, cik, period_of_report)
        filer_id, cik, period_of_report, filed_at, accession_no
    FROM candidate
    WHERE replaces_period
    ORDER BY filer_id, cik, period_of_report, filed_at DESC, accession_no DESC
)
SELECT c.id AS filing_id, c.filer_id, c.cik, c.period_of_report
FROM candidate AS c
LEFT JOIN base AS b
    ON b.filer_id = c.filer_id
   AND b.cik = c.cik
   AND b.period_of_report = c.period_of_report
WHERE b.accession_no IS NULL
   OR c.accession_no = b.accession_no
   OR (
       NOT c.replaces_period
       AND (c.filed_at, c.accession_no) > (b.filed_at, b.accession_no)
   )
"""

EFFECTIVE_FILING = """
CREATE VIEW effective_filing AS
SELECT filing_id, filer_id, cik, period_of_report
FROM (
    SELECT
        e.filing_id,
        e.filer_id,
        e.cik,
        e.period_of_report,
        f.overlap,
        dense_rank() OVER (
            PARTITION BY e.filer_id, e.period_of_report
            ORDER BY coalesce(fc.priority, -1) DESC, e.cik DESC
        ) AS cik_rank
    FROM effective_filing_by_cik AS e
    JOIN filer AS f ON f.id = e.filer_id
    LEFT JOIN filer_cik AS fc ON fc.cik = e.cik AND fc.filer_id = e.filer_id
) AS ranked
WHERE overlap = 'sum' OR cik_rank = 1
"""


def upgrade() -> None:
    op.add_column(
        "filer",
        sa.Column("overlap", sa.Text(), nullable=False, server_default="successor"),
    )
    op.create_check_constraint("overlap_is_known", "filer", OVERLAP_CHECK)
    op.add_column(
        "filer_cik",
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.execute(EFFECTIVE_FILING_BY_CIK)
    op.execute(EFFECTIVE_FILING)


def downgrade() -> None:
    """Views first — they depend on both columns.

    Nothing is lost that the seed cannot write back: ``overlap`` and
    ``priority`` both come from ``data/investors.yaml``.
    """
    op.execute("DROP VIEW effective_filing")
    op.execute("DROP VIEW effective_filing_by_cik")
    op.drop_column("filer_cik", "priority")
    op.drop_constraint("overlap_is_known", "filer", type_="check")
    op.drop_column("filer", "overlap")
