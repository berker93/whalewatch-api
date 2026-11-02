"""saved filters: the screener's named filters, and when each was last run

``saved_filter.query`` held ``GET /v1/flows``'s parameters. A saved filter is
now ``GET /v1/screener``'s, so the column is ``params``: ``{"filters": {key:
[value, ...]}, "sort": ..., "order": ...}``, the vocabulary's keys and values
as a client sends them, without the period, which is chosen when it is run.
``last_run_at`` is when its screen was last read through
``GET /v1/saved-filters/{id}/screen``; null until it is.

Rows already there are left as they were, not translated: ``/v1/flows``'s
``min_value`` is a stock's dollars, the screener's a position's, so no
mapping keeps their meaning. The API reads every row through the vocabulary
and reports what it cannot read rather than applying it, so one of them reads
as a filter of nothing, with each old key named.

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-02 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("saved_filter", "query", new_column_name="params")
    op.execute(
        "ALTER TABLE saved_filter RENAME CONSTRAINT ck_saved_filter_query_is_an_object "
        "TO ck_saved_filter_params_is_an_object"
    )
    op.add_column(
        "saved_filter", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("saved_filter", "last_run_at")
    op.execute(
        "ALTER TABLE saved_filter RENAME CONSTRAINT ck_saved_filter_params_is_an_object "
        "TO ck_saved_filter_query_is_an_object"
    )
    op.alter_column("saved_filter", "params", new_column_name="query")
