"""insider transaction

The Form 4 schema: ``insider`` (one row per reporting owner's CIK),
``reporting_owner`` (who a filing names, and their relationship to the issuer
on it), and ``insider_transaction`` (one row per line of the filing's
transaction tables). See :mod:`app.db.models.insider` and
:class:`~app.db.models.insider_transaction.InsiderTransaction`.

``transaction_date`` is in the primary key and the unique constraint, which it
does not need yet, so that partitioning on it later keeps both as they are.

Created empty. Nothing writes them until the Form 4 crawler and loader exist.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-14 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.models.insider_transaction import OPEN_MARKET_PREDICATE, VALUE_EXPRESSION

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "insider",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.CHAR(length=10), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_insider"),
        sa.UniqueConstraint("cik", name="uq_insider_cik"),
    )
    op.create_table(
        "reporting_owner",
        sa.Column("filing_id", sa.BigInteger(), nullable=False),
        sa.Column("insider_id", sa.BigInteger(), nullable=False),
        sa.Column("is_director", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_officer", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_ten_percent_owner", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_other", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("officer_title", sa.Text(), nullable=True),
        sa.Column("other_text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("filing_id", "insider_id", name="pk_reporting_owner"),
        sa.ForeignKeyConstraint(
            ["filing_id"],
            ["filing.id"],
            name="fk_reporting_owner_filing_id_filing",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["insider_id"],
            ["insider.id"],
            name="fk_reporting_owner_insider_id_insider",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "insider_transaction",
        # BIGSERIAL: autoincrement in a composite key. Not IDENTITY, which a
        # partitioned table cannot have on Postgres 16.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("filing_id", sa.BigInteger(), nullable=False),
        sa.Column("insider_id", sa.BigInteger(), nullable=False),
        sa.Column("line_number", sa.SmallInteger(), nullable=False),
        sa.Column("issuer_cik", sa.CHAR(length=10), nullable=False),
        sa.Column("issuer_name", sa.Text(), nullable=False),
        sa.Column("issuer_ticker", sa.Text(), nullable=True),
        sa.Column("security_id", sa.BigInteger(), nullable=True),
        sa.Column("security_title", sa.Text(), nullable=True),
        sa.Column("is_derivative", sa.Boolean(), nullable=False),
        sa.Column("transaction_code", sa.CHAR(length=1), nullable=False),
        sa.Column("acquired_disposed", sa.CHAR(length=1), nullable=False),
        sa.Column("shares", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("price_per_share", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column(
            "value_usd",
            sa.Numeric(precision=20, scale=2),
            sa.Computed(VALUE_EXPRESSION, persisted=True),
            nullable=True,
        ),
        sa.Column("shares_owned_after", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("is_10b5_1", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("footnotes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", "transaction_date", name="pk_insider_transaction"),
        sa.UniqueConstraint(
            "filing_id",
            "line_number",
            "transaction_date",
            name="uq_insider_transaction_filing_id_line_number_transaction_date",
        ),
        sa.ForeignKeyConstraint(
            ["filing_id", "insider_id"],
            ["reporting_owner.filing_id", "reporting_owner.insider_id"],
            name="fk_insider_transaction_filing_id_insider_id_reporting_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["security.id"],
            name="fk_insider_transaction_security_id_security",
            ondelete="SET NULL",
        ),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("line_number > 0", name="line_number_is_positive"),
        sa.CheckConstraint("acquired_disposed IN ('A', 'D')", name="acquired_disposed_is_known"),
        sa.CheckConstraint("shares >= 0", name="shares_is_not_negative"),
        sa.CheckConstraint("shares_owned_after >= 0", name="shares_owned_after_is_not_negative"),
        sa.CheckConstraint("price_per_share > 0", name="a_price_is_positive_or_null"),
        sa.CheckConstraint("jsonb_typeof(footnotes) = 'object'", name="footnotes_is_an_object"),
    )
    op.create_index(
        "ix_insider_transaction_transaction_date_open_market",
        "insider_transaction",
        [sa.literal_column("transaction_date DESC")],
        postgresql_where=sa.text(OPEN_MARKET_PREDICATE),
    )


def downgrade() -> None:
    """Loses every loaded Form 4 line and owner. Re-parsing the archived
    filings restores them; the ``filing`` rows themselves are kept."""
    op.drop_index(
        "ix_insider_transaction_transaction_date_open_market",
        table_name="insider_transaction",
    )
    op.drop_table("insider_transaction")
    op.drop_table("reporting_owner")
    op.drop_table("insider")
