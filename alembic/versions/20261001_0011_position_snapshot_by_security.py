"""position snapshot by security

Reshapes ``position_snapshot`` into the portfolio the product displays: one row
per security per ``(filer, period)``, common stock only, keyed on exactly that.
See :class:`~app.db.models.position_snapshot.PositionSnapshot`.

* The primary key is ``(filer_id, period_of_report, security_id)``, and the
  surrogate ``id`` goes. Nothing refers to a snapshot row, and an ``id`` that
  every rebuild renumbers is no use as a cursor either.
* Option lines and ``PRN`` principal amounts are no longer rows. Without them a
  security is one row per period, which is what makes the key a key. ``cusip``,
  ``put_call`` and ``sshprnamt_type`` go with them: the first is
  ``security.cusip``, and the other two would be the same on every row.
* ``weight``, a fraction that was null on an option line, becomes
  ``weight_pct``, a percentage of the period's value.
* ``source_filing_id`` is new: the filing each position was read from.

Dropped and created again, empty, rather than altered. None of the new columns
can be filled from the old rows. ``source_filing_id`` needs the period resolved
again, and so do the weights, because a period's total changes once its
``PRN`` rows leave it. Doing either here would run ``recompute`` inside a schema
change, which 0009 declined to do for the reason it gives. The table is a
cache: run ``recompute`` after upgrading. Until then it is empty, which reads as
nothing published.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("position_snapshot")
    op.create_table(
        "position_snapshot",
        sa.Column("filer_id", sa.BigInteger(), nullable=False),
        sa.Column("period_of_report", sa.Date(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("shares", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("value_usd", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("weight_pct", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("source_filing_id", sa.BigInteger(), nullable=False),
        sa.Column("suspect", sa.Boolean(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "filer_id", "period_of_report", "security_id", name="pk_position_snapshot"
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
        # CASCADE, as on filer_id: a cache has no business keeping a filing alive.
        sa.ForeignKeyConstraint(
            ["source_filing_id"],
            ["filing.id"],
            name="fk_position_snapshot_source_filing_id_filing",
            ondelete="CASCADE",
        ),
        # Bare name: the ck template prefixes it (see 0002).
        sa.CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100", name="weight_pct_is_a_percentage"
        ),
    )


def downgrade() -> None:
    """0009's table, empty again. Nothing is lost: every row is derived, and
    ``recompute`` at that revision rebuilds them from ``holding``."""
    op.drop_table("position_snapshot")
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
        sa.CheckConstraint("weight >= 0 AND weight <= 1", name="weight_is_a_fraction"),
        sa.CheckConstraint("put_call IS NULL OR weight IS NULL", name="an_option_has_no_weight"),
    )
