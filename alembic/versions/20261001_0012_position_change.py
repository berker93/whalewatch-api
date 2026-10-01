"""position change

``position_change``: what each filer did to each position since its previous
published period (``new``, ``add``, ``trim`` or ``hold``), with the previous
figures and the deltas. Rebuilt from ``position_snapshot`` by ``whalewatch
recompute``, in the same transaction. See
:class:`~app.db.models.position_change.PositionChange`.

Created empty, for 0009's reason: filling it here would run ``recompute`` inside
a schema change. Run ``recompute`` after upgrading. Until then the table is
empty, which reads as no changes published.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.position_change import ACTION_CHECK, NEW_CHECK

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_change",
        sa.Column("filer_id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("shares", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("value_usd", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("weight_pct", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("prev_period_of_report", sa.Date(), nullable=True),
        sa.Column("prev_shares", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("prev_value_usd", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("prev_weight_pct", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("shares_delta", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("shares_delta_pct", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("value_delta", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("weight_delta", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("suspect", sa.Boolean(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "filer_id", "period_of_report", "security_id", name="pk_position_change"
        ),
        sa.ForeignKeyConstraint(
            ["filer_id"],
            ["filer.id"],
            name="fk_position_change_filer_id_filer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_position_change_security_id_security",
            ondelete="RESTRICT",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint(ACTION_CHECK, name="action_is_known"),
        sa.CheckConstraint(NEW_CHECK, name="new_when_not_held_before"),
    )


def downgrade() -> None:
    """Nothing is lost: every row is derived, and ``recompute`` rebuilds them
    from ``position_snapshot`` after the next upgrade."""
    op.drop_table("position_change")
