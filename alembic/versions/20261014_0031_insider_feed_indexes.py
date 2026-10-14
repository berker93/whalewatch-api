"""insider feed indexes

What the insider feed (INS-5) reads ``insider_transaction`` by.

``ix_insider_transaction_transaction_date_open_market`` gains ``id``: the feed
pages by ``(transaction_date, id)``, newest first, and with the date alone
every page sorted the day it ended in again. Still partial on P and S.

``ix_insider_transaction_security_id_transaction_date``: one stock's trades,
newest first, for ``/v1/stocks/{ticker}/insiders`` and ``?ticker=``.

``ix_insider_transaction_issuer_cik_unlinked``: the lines the loader could not
link to a security, by issuer. A stock's insider trades include its issuer's
unlinked lines (a dual-class issuer whose filers name both classes), and this
is how they are found without reading every line the issuer filed. Partial,
so it holds only those few.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-14 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from the model, so that this revision builds
# the index it built when it was written.
_OPEN_MARKET = "transaction_code IN ('P', 'S')"


def upgrade() -> None:
    op.drop_index(
        "ix_insider_transaction_transaction_date_open_market",
        table_name="insider_transaction",
    )
    op.create_index(
        "ix_insider_transaction_transaction_date_open_market",
        "insider_transaction",
        [sa.literal_column("transaction_date DESC"), sa.literal_column("id DESC")],
        postgresql_where=sa.text(_OPEN_MARKET),
    )
    op.create_index(
        "ix_insider_transaction_security_id_transaction_date",
        "insider_transaction",
        [
            "security_id",
            sa.literal_column("transaction_date DESC"),
            sa.literal_column("id DESC"),
        ],
    )
    op.create_index(
        "ix_insider_transaction_issuer_cik_unlinked",
        "insider_transaction",
        ["issuer_cik"],
        postgresql_where=sa.text("security_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_insider_transaction_issuer_cik_unlinked", table_name="insider_transaction")
    op.drop_index(
        "ix_insider_transaction_security_id_transaction_date", table_name="insider_transaction"
    )
    op.drop_index(
        "ix_insider_transaction_transaction_date_open_market",
        table_name="insider_transaction",
    )
    op.create_index(
        "ix_insider_transaction_transaction_date_open_market",
        "insider_transaction",
        [sa.literal_column("transaction_date DESC")],
        postgresql_where=sa.text(_OPEN_MARKET),
    )
