"""screener index: a period's positions, largest first

``GET /v1/screener`` reads one period of ``position_change``, every action but
an exit unless one is asked for, largest position first. No index led with
the period for every action (0017's leaves out holds), so the default screen
was a parallel sequential scan of the whole table and a top-N sort: 25 ms of
575,000 rows on the dev database's backfill, and more with every quarter
loaded. On ``(period_of_report, value_usd DESC, filer_id DESC, security_id
DESC)``, the screen's default keyset, it is an index scan that stops after the
page: 0.7 ms. A screen with filters or another sort reads the period's entries
from it and filters them, as it would have the table. 26 MB on the backfill.

Built without ``CONCURRENTLY``, for 0017's reason.

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-01 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_position_change_period_of_report_value_usd",
        "position_change",
        [
            "period_of_report",
            sa.text("value_usd DESC"),
            sa.text("filer_id DESC"),
            sa.text("security_id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_position_change_period_of_report_value_usd", table_name="position_change")
