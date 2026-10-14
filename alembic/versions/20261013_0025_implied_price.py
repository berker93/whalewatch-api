"""implied price

``implied_price``: each security's quarter-end price per period, the median of
``value_usd / shares`` over the filers that held it, with how many observed it
and how far they agree. Rebuilt from ``position_snapshot`` by ``whalewatch
recompute``, in the same transaction. See
:class:`~app.db.models.implied_price.ImpliedPrice`.

Created empty, for 0012's reason. Run ``recompute --all`` after upgrading.
Until then the table is empty, which reads as no price known for anything.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-13 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.implied_price import CONFIDENCE_CHECK, PRICED_CHECK

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "implied_price",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("unpriced_count", sa.Integer(), nullable=False),
        sa.Column("agreeing_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("suspect", sa.Boolean(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("security_id", "period_of_report", name="pk_implied_price"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_implied_price_security_id_security",
            ondelete="CASCADE",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("price > 0", name="price_is_positive"),
        sa.CheckConstraint(PRICED_CHECK, name="priced_when_confident"),
        sa.CheckConstraint(CONFIDENCE_CHECK, name="confidence_is_known"),
        sa.CheckConstraint(
            "observation_count > 0"
            " AND unpriced_count BETWEEN 0 AND observation_count"
            " AND agreeing_count BETWEEN 0 AND observation_count - unpriced_count",
            name="counts_are_consistent",
        ),
    )


def downgrade() -> None:
    """Nothing is lost: every row is derived, and ``recompute`` rebuilds them
    from ``position_snapshot`` after the next upgrade."""
    op.drop_table("implied_price")
