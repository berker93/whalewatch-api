"""security quote

``security_quote``: a security's closing price on a trading day, from the quote
provider, written by ``fetch-quotes``. See
:class:`~app.db.models.security_quote.SecurityQuote`.

Created empty, and stays empty under the default ``QUOTE_PROVIDER=null``, which
is production's. Nothing reads it yet.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-14 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.security_quote import QUOTE_SOURCE_CHECK

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_quote",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("security_id", "date", name="pk_security_quote"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_security_quote_security_id_security",
            ondelete="CASCADE",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("close > 0", name="close_is_positive"),
        sa.CheckConstraint("ticker <> ''", name="ticker_is_not_empty"),
        sa.CheckConstraint(QUOTE_SOURCE_CHECK, name="source_is_known"),
    )


def downgrade() -> None:
    """Loses the fetched closes. ``fetch-quotes`` gets the latest again, but
    not the days before it."""
    op.drop_table("security_quote")
