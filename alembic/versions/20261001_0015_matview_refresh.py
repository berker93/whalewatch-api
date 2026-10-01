"""matview refresh

``matview_refresh``: one row per materialised view, saying when it was last
refreshed, written in the transaction that refreshes it. A view is as of its
last refresh, and this is what lets a response built from one say when that
was. See :class:`~app.db.models.matview_refresh.MatviewRefresh`.

Created empty. 0014 filled the views, but nothing recorded when, and a time
written here now would claim they are as fresh as this migration. A view
without a row has a last refresh that is not known, and the first
``refresh-views`` records it.

Also ``ingestion_run.metrics``: what a run measured beyond its two counters,
written as it ends. ``refresh-views`` records how long each view took there.
Every existing run gets ``{}``, which is true, since none of them measured
anything else.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 23:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matview_refresh",
        sa.Column("view_name", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("view_name", name="pk_matview_refresh"),
    )
    op.add_column(
        "ingestion_run",
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Drops when each view was last refreshed and every run's metrics. The
    views keep their rows, and the next refresh after an upgrade records its
    time again."""
    op.drop_column("ingestion_run", "metrics")
    op.drop_table("matview_refresh")
