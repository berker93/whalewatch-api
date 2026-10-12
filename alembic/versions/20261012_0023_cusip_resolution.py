"""cusip resolution

What ``resolve-cusips`` and ``resolve-cusip`` need on ``security``, and the
queue they write fuzzy matches to.

* ``security.filed_name``: the filing's ``nameOfIssuer``, copied from ``name``
  for every existing row. ``name`` becomes the best name we have, written by
  resolution; this keeps what it replaces. Trigram-indexed like ``name``, for
  the fuzzy match, which compares both.
* ``security.resolution_note``: why a person resolved a CUSIP by hand.
* ``resolution_source`` may now be ``13f_list`` (a name and no ticker), and a
  ticker must say where it came from.
* ``manual_review``: see :class:`~app.db.models.manual_review.ManualReview`.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-12 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.models.manual_review import REVIEW_STATUS_CHECK
from app.db.models.security import RESOLUTION_SOURCE_CHECK

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 0002's constraint, for the downgrade.
_OLD_RESOLUTION_SOURCE_CHECK = (
    "resolution_source IS NULL OR resolution_source IN ('openfigi', '13f_column', 'manual')"
)


def upgrade() -> None:
    op.add_column("security", sa.Column("filed_name", sa.Text(), nullable=True))
    op.add_column("security", sa.Column("resolution_note", sa.Text(), nullable=True))
    op.execute("UPDATE security SET filed_name = name")
    op.create_index(
        "ix_security_filed_name_trgm",
        "security",
        ["filed_name"],
        postgresql_using="gin",
        postgresql_ops={"filed_name": "gin_trgm_ops"},
    )

    # Bare names: the ck template prefixes them (see 0002).
    op.drop_constraint("resolution_source_is_known", "security", type_="check")
    op.create_check_constraint("resolution_source_is_known", "security", RESOLUTION_SOURCE_CHECK)
    op.create_check_constraint(
        "a_ticker_has_a_source", "security", "ticker IS NULL OR resolution_source IS NOT NULL"
    )

    op.create_table(
        "manual_review",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("suggested_security_id", sa.BigInteger(), nullable=False),
        sa.Column("suggested_ticker", sa.Text(), nullable=False),
        sa.Column("matched_name", sa.Text(), nullable=False),
        sa.Column("suggested_name", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_manual_review"),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_manual_review_security_id_security",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suggested_security_id"],
            ["security.id"],
            name="fk_manual_review_suggested_security_id_security",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(REVIEW_STATUS_CHECK, name="status_is_known"),
        sa.CheckConstraint(
            "(status = 'pending') = (closed_at IS NULL)", name="closed_when_not_pending"
        ),
        sa.CheckConstraint(
            "security_id <> suggested_security_id", name="not_a_suggestion_of_itself"
        ),
    )
    op.create_index(
        "uq_manual_review_pending",
        "manual_review",
        ["security_id", "suggested_security_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Drops the queue, the notes and the filed names, after putting ``name``
    back to the filed name: the schema this returns to has nowhere else to
    keep it. Tickers stay, OpenFIGI's and manual ones alike, since 0002 allows
    both; ``13f_list`` rows, which it does not allow, go back to unresolved."""
    op.drop_index("uq_manual_review_pending", table_name="manual_review")
    op.drop_table("manual_review")
    op.execute("UPDATE security SET name = filed_name WHERE name IS DISTINCT FROM filed_name")
    op.execute(
        "UPDATE security SET resolution_source = NULL, resolved_at = NULL"
        " WHERE resolution_source = '13f_list'"
    )
    op.drop_constraint("a_ticker_has_a_source", "security", type_="check")
    op.drop_constraint("resolution_source_is_known", "security", type_="check")
    op.create_check_constraint(
        "resolution_source_is_known", "security", _OLD_RESOLUTION_SOURCE_CHECK
    )
    op.drop_column("security", "resolution_note")
    op.drop_index("ix_security_filed_name_trgm", table_name="security")
    op.drop_column("security", "filed_name")
