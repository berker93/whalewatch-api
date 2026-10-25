"""saved filters: a user's named screener filters

``saved_filter`` holds one filter one user saved for the screener: a name,
and ``GET /v1/flows``'s parameters as a JSON object. How many an account may
keep is its plan's ``screener_saved_filters``, checked by the route when one is
saved; the table holds no limit of its own. Erased with the account, by its
foreign key, as the watchlist is.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_filter",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_filter"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_saved_filter_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "name = btrim(name) AND length(name) BETWEEN 1 AND 80", name="name_is_named"
        ),
        sa.CheckConstraint("jsonb_typeof(query) = 'object'", name="query_is_an_object"),
    )
    op.create_index("ix_saved_filter_user_id", "saved_filter", ["user_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_saved_filter_user_id", "saved_filter")
    op.drop_table("saved_filter")
