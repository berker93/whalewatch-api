"""ticker history

``security_alias`` becomes the history of each security's tickers, and a
security can be flagged as no longer listed.

* ``security_alias.alias`` is renamed ``ticker``, and gains ``valid_from`` and
  ``valid_to`` (a half-open range, null for unbounded) and ``note``. One
  security has one ticker at a time: an exclusion constraint on
  ``(security_id, daterange(valid_from, valid_to))``, which needs
  ``btree_gist``. Different securities may share a ticker at the same time;
  ``cusip-coverage`` reports that as a collision rather than refusing it.
* Rows that only spell the current ticker differently (``BRK/B`` and ``BRK-B``
  for ``BRK.B``, written by 0023's ``resolve-cusips``) are removed. The lookup
  now reads ``/`` and ``-`` as ``.``, so they find nothing it cannot.
* Every ticker in ``security`` gets an open-ended row, its source copied from
  ``resolution_source``. It runs from the beginning of time unless the security
  has older rows, which are closed today and the current one opened today.
* ``security.delisted_by``: see :attr:`~app.db.models.security.Security.delisted_by`.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-13 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.add_column("security", sa.Column("delisted_by", sa.Date(), nullable=True))

    op.drop_index("uq_security_alias_upper_alias_security_id", table_name="security_alias")
    op.drop_constraint("alias_is_not_empty", "security_alias", type_="check")
    op.alter_column("security_alias", "alias", new_column_name="ticker")
    op.create_check_constraint("ticker_is_not_empty", "security_alias", "ticker <> ''")
    op.add_column("security_alias", sa.Column("valid_from", sa.Date(), nullable=True))
    op.add_column("security_alias", sa.Column("valid_to", sa.Date(), nullable=True))
    op.add_column("security_alias", sa.Column("note", sa.Text(), nullable=True))
    op.create_check_constraint(
        "valid_from_before_valid_to", "security_alias", "valid_from < valid_to"
    )

    # Spellings of the current ticker: the lookup normalises them now.
    op.execute(
        "DELETE FROM security_alias a USING security s"
        " WHERE s.id = a.security_id AND s.ticker IS NOT NULL"
        " AND translate(upper(a.ticker), '/-', '..') = upper(s.ticker)"
    )
    # Anything left is a former ticker of unknown dates: ended today, as far
    # as the history can say.
    op.execute("UPDATE security_alias SET valid_to = current_date")
    op.execute(
        "INSERT INTO security_alias (security_id, ticker, source, valid_from)"
        " SELECT s.id, s.ticker, s.resolution_source,"
        " (SELECT max(a.valid_to) FROM security_alias a WHERE a.security_id = s.id)"
        " FROM security s WHERE s.ticker IS NOT NULL"
    )

    op.execute(
        "ALTER TABLE security_alias ADD CONSTRAINT ex_security_alias_one_ticker_at_a_time"
        " EXCLUDE USING gist (security_id WITH =, daterange(valid_from, valid_to) WITH &&)"
    )
    op.execute(
        "CREATE INDEX ix_security_alias_upper_ticker"
        " ON security_alias (upper(ticker) text_pattern_ops)"
    )
    op.execute("ANALYZE security_alias")


def downgrade() -> None:
    """Back to undated aliases. The open rows go, since 0018 kept the current
    ticker in ``security`` alone; the closed ones stay as aliases. A ticker a
    security had twice, in two ranges, keeps one row."""
    op.drop_index("ix_security_alias_upper_ticker", table_name="security_alias")
    op.execute("ALTER TABLE security_alias DROP CONSTRAINT ex_security_alias_one_ticker_at_a_time")
    op.execute("DELETE FROM security_alias WHERE valid_to IS NULL")
    op.execute(
        "DELETE FROM security_alias a USING security_alias b"
        " WHERE a.security_id = b.security_id AND upper(a.ticker) = upper(b.ticker)"
        " AND a.id < b.id"
    )
    op.drop_constraint("valid_from_before_valid_to", "security_alias", type_="check")
    op.drop_column("security_alias", "note")
    op.drop_column("security_alias", "valid_to")
    op.drop_column("security_alias", "valid_from")
    op.drop_constraint("ticker_is_not_empty", "security_alias", type_="check")
    op.alter_column("security_alias", "ticker", new_column_name="alias")
    op.create_check_constraint("alias_is_not_empty", "security_alias", "alias <> ''")
    op.execute(
        "CREATE UNIQUE INDEX uq_security_alias_upper_alias_security_id "
        "ON security_alias (upper(alias) text_pattern_ops, security_id)"
    )
    op.drop_column("security", "delisted_by")
