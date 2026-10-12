"""figi response log

``figi_response_log``: every answer OpenFIGI's mapping API gives, one row per
CUSIP asked about, the job's response kept verbatim as ``jsonb``. The client
reads the latest row per CUSIP before asking again, so re-running a resolution
costs no quota. See :class:`~app.db.models.figi_response_log.FigiResponseLog`.

One index, on ``(cusip, requested_at DESC)``: "the latest answer for these
CUSIPs" is the only question asked of it in the request path.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.models.figi_response_log import OUTCOME_CHECK

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "figi_response_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cusip", sa.CHAR(9), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_figi_response_log"),
        # Bare name: the ck template prefixes it (see 0002).
        sa.CheckConstraint(OUTCOME_CHECK, name="outcome_is_known"),
    )
    op.create_index(
        "ix_figi_response_log_cusip_requested_at",
        "figi_response_log",
        ["cusip", sa.text("requested_at DESC")],
    )


def downgrade() -> None:
    """Drops every answer OpenFIGI has given, which only quota can buy back:
    the next resolution run asks about every CUSIP again."""
    op.drop_index("ix_figi_response_log_cusip_requested_at", table_name="figi_response_log")
    op.drop_table("figi_response_log")
