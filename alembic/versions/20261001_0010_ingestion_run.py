"""ingestion run

``ingestion_run``: one row per execution of a job, written as it starts and
updated as it ends, so whether a backfill finished is a ``SELECT``. ``id`` is
the ``run_id`` on every log line the run wrote. See
:class:`~app.db.models.ingestion_run.IngestionRun`.

One index, on ``(job_name, started_at DESC)``. Unlike ``pending_filing``, which
is bounded by the tracked universe, this table only grows, and "the latest runs
of one job" is the question it exists to answer.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.models.ingestion_run import (
    FINISHED_CHECK,
    NOT_SUCCESS_SAYS_WHY_CHECK,
    RUN_STATUS_CHECK,
)

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_run",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_run"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(RUN_STATUS_CHECK, name="status_is_known"),
        sa.CheckConstraint(FINISHED_CHECK, name="finished_when_not_running"),
        sa.CheckConstraint(NOT_SUCCESS_SAYS_WHY_CHECK, name="an_unsuccessful_run_says_why"),
        sa.CheckConstraint("items_seen >= 0", name="items_seen_is_not_negative"),
        sa.CheckConstraint("items_written >= 0", name="items_written_is_not_negative"),
    )
    op.create_index(
        "ix_ingestion_run_job_name_started_at",
        "ingestion_run",
        ["job_name", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    """Drops the run history, which nothing can rebuild: the logs still have
    every line, but no longer the one row per run that summarised them."""
    op.drop_index("ix_ingestion_run_job_name_started_at", table_name="ingestion_run")
    op.drop_table("ingestion_run")
