"""position snapshot

``position_snapshot``: the published portfolio, one row per position per
``(filer, period)``, resolved through ``effective_filing`` and rebuilt wholesale
by ``whalewatch recompute``. The first table in the derived layer. See
:class:`~app.db.models.position_snapshot.PositionSnapshot`.

Created empty. Filling it here would run ``recompute`` inside a schema change,
and would publish whatever happened to be loaded at deploy time without anyone
having run ``check-data`` over it. Until the first ``recompute`` the table is
empty, and that is what "nothing published yet" should look like.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_snapshot",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("filer_id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("cusip", sa.CHAR(length=9), nullable=False),
        sa.Column("put_call", sa.Text(), nullable=True),
        sa.Column("sshprnamt_type", sa.Text(), nullable=False),
        sa.Column("shares", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("value_usd", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("weight", sa.Numeric(precision=7, scale=6), nullable=True),
        sa.Column("suspect", sa.Boolean(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_position_snapshot"),
        # One row per position per period, however many filings the period
        # resolved to. NULLS NOT DISTINCT for holding's reason: put_call is null
        # on nearly every row, and without it the key constrains nothing.
        sa.UniqueConstraint(
            "filer_id",
            "period_of_report",
            "cusip",
            "put_call",
            "sshprnamt_type",
            name="uq_position_snapshot_filer_period_position",
            postgresql_nulls_not_distinct=True,
        ),
        sa.ForeignKeyConstraint(
            ["filer_id"],
            ["filer.id"],
            name="fk_position_snapshot_filer_id_filer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_position_snapshot_security_id_security",
            ondelete="RESTRICT",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="weight_is_a_fraction"),
        sa.CheckConstraint("put_call IS NULL OR weight IS NULL", name="an_option_has_no_weight"),
    )


def downgrade() -> None:
    """Nothing is lost: every row is derived, and ``recompute`` rebuilds them
    from ``holding`` after the next upgrade."""
    op.drop_table("position_snapshot")
