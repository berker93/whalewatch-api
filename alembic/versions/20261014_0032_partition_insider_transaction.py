"""partition insider transaction

``insider_transaction`` becomes ``PARTITION BY RANGE (transaction_date)``, one
partition a year from 2020 through 2027 and a default partition for any other
date. See :mod:`app.db.partitions` for the layout and what keeps it ahead of
the calendar.

A table cannot be altered into being partitioned, so this is a data move:

1. ``LOCK ... IN SHARE MODE``: the copy below must see every row, so nothing
   writes until this commits. Reads carry on.
2. The partitioned table is created beside the live one as
   ``insider_transaction_new``, with its partitions, the same columns, checks
   and foreign keys, and its own sequence default pointing at the existing
   sequence, so ids carry on where they were.
3. Every row is copied across, ``value_usd`` left for the new table to
   generate.
4. The row count of each year is compared between the two, and the migration
   raises if any differs. A ``DO`` block rather than Python, so the check is
   in ``alembic upgrade --sql`` output too.
5. The two tables swap names, the sequence moves to the new one (dropping the
   old table would otherwise drop it), and the old table is dropped.
6. The indexes are built, after the copy rather than before it, and the
   parent analysed: autovacuum analyses each partition but never the parent.

The primary key ``(id, transaction_date)`` and the dedup constraint
``(filing_id, line_number, transaction_date)`` are unchanged. Both had the
partition key in them since 0028, which is what a unique constraint on a
partitioned table requires.

All of it is one transaction, like every migration here, so a failed check
leaves the table as it was.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-14 23:59:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported, so that this revision builds what it built
# when it was written. The first and last year are the ones app.db.partitions
# had then; the last is next year as of writing, and `create-partitions` (and
# every `ingest-form4`) adds the years after it.
_FIRST_YEAR = 2020
_LAST_YEAR = 2027

_TABLE = "insider_transaction"

# Every column but value_usd, which is generated and cannot be inserted.
_COPIED = (
    "id, transaction_date, filing_id, insider_id, line_number, issuer_cik, issuer_name, "
    "issuer_ticker, security_id, security_title, is_derivative, transaction_code, "
    "acquired_disposed, shares, price_per_share, shares_owned_after, is_10b5_1, footnotes"
)

_OPEN_MARKET = "transaction_code IN ('P', 'S')"


def _columns_and_constraints(pk: str, uq: str) -> str:
    """The body of 0028's ``CREATE TABLE``, as Postgres has it. The primary key
    and the unique constraint are named by the caller: each is an index, and an
    index name is taken schema-wide, so the copy cannot have the live table's
    until the live table is gone. Checks and foreign keys are named per table
    and keep their names throughout."""
    return f"""
        id bigint NOT NULL DEFAULT nextval('{_TABLE}_id_seq'::regclass),
        transaction_date date NOT NULL,
        filing_id bigint NOT NULL,
        insider_id bigint NOT NULL,
        line_number smallint NOT NULL,
        issuer_cik character(10) NOT NULL,
        issuer_name text NOT NULL,
        issuer_ticker text,
        security_id bigint,
        security_title text,
        is_derivative boolean NOT NULL,
        transaction_code character(1) NOT NULL,
        acquired_disposed character(1) NOT NULL,
        shares numeric(20, 4),
        price_per_share numeric(20, 8),
        value_usd numeric(20, 2)
            GENERATED ALWAYS AS (round(shares * price_per_share, 2)) STORED,
        shares_owned_after numeric(20, 4),
        is_10b5_1 boolean NOT NULL DEFAULT false,
        footnotes jsonb,
        CONSTRAINT {pk} PRIMARY KEY (id, transaction_date),
        CONSTRAINT {uq} UNIQUE (filing_id, line_number, transaction_date),
        CONSTRAINT fk_insider_transaction_filing_id_insider_id_reporting_owner
            FOREIGN KEY (filing_id, insider_id)
            REFERENCES reporting_owner (filing_id, insider_id) ON DELETE CASCADE,
        CONSTRAINT fk_insider_transaction_security_id_security
            FOREIGN KEY (security_id) REFERENCES security (id) ON DELETE SET NULL,
        CONSTRAINT ck_insider_transaction_line_number_is_positive
            CHECK (line_number > 0),
        CONSTRAINT ck_insider_transaction_acquired_disposed_is_known
            CHECK (acquired_disposed IN ('A', 'D')),
        CONSTRAINT ck_insider_transaction_shares_is_not_negative
            CHECK (shares >= 0),
        CONSTRAINT ck_insider_transaction_shares_owned_after_is_not_negative
            CHECK (shares_owned_after >= 0),
        CONSTRAINT ck_insider_transaction_a_price_is_positive_or_null
            CHECK (price_per_share > 0),
        CONSTRAINT ck_insider_transaction_footnotes_is_an_object
            CHECK (jsonb_typeof(footnotes) = 'object')
    """


_PK = f"pk_{_TABLE}"
_UQ = f"uq_{_TABLE}_filing_id_line_number_transaction_date"


def _copy_and_verify(source: str, target: str) -> None:
    """Every row of ``source`` into ``target``, then the same count in each
    year in both, or an exception that rolls the migration back."""
    op.execute(f"INSERT INTO {target} ({_COPIED}) SELECT {_COPIED} FROM {source}")
    op.execute(f"""
        DO $$
        DECLARE
            mismatch record;
        BEGIN
            SELECT year, before, after INTO mismatch
            FROM (
                SELECT extract(year FROM transaction_date) AS year, count(*) AS before
                FROM {source} GROUP BY 1
            ) AS old
            FULL JOIN (
                SELECT extract(year FROM transaction_date) AS year, count(*) AS after
                FROM {target} GROUP BY 1
            ) AS new USING (year)
            WHERE before IS DISTINCT FROM after
            LIMIT 1;
            IF FOUND THEN
                RAISE EXCEPTION
                    '{source} -> {target}: % rows in %, % after the copy',
                    coalesce(mismatch.before, 0), mismatch.year, coalesce(mismatch.after, 0);
            END IF;
        END $$
    """)


def _swap(old: str) -> None:
    """``insider_transaction`` becomes ``old`` and ``insider_transaction_new``
    takes its name, its sequence, and its constraints' names. ``old`` is
    dropped: the check before this was the last chance to keep it."""
    op.execute(f"ALTER TABLE {_TABLE} RENAME TO {old}")
    op.execute(f"ALTER TABLE {_TABLE}_new RENAME TO {_TABLE}")
    op.execute(f"ALTER SEQUENCE {_TABLE}_id_seq OWNED BY {_TABLE}.id")
    op.execute(f"DROP TABLE {old}")
    op.execute(f"ALTER TABLE {_TABLE} RENAME CONSTRAINT {_PK}_new TO {_PK}")
    op.execute(f"ALTER TABLE {_TABLE} RENAME CONSTRAINT {_UQ}_new TO {_UQ}")


def _create_indexes() -> None:
    """0031's three. On a partitioned table each is a parent index with one
    index per partition under it, created and named by Postgres, and a
    partition created later gets its own copies."""
    op.execute(f"""
        CREATE INDEX ix_{_TABLE}_transaction_date_open_market
        ON {_TABLE} (transaction_date DESC, id DESC) WHERE {_OPEN_MARKET}
    """)
    op.execute(f"""
        CREATE INDEX ix_{_TABLE}_security_id_transaction_date
        ON {_TABLE} (security_id, transaction_date DESC, id DESC)
    """)
    op.execute(f"""
        CREATE INDEX ix_{_TABLE}_issuer_cik_unlinked
        ON {_TABLE} (issuer_cik) WHERE security_id IS NULL
    """)


def upgrade() -> None:
    op.execute(f"LOCK TABLE {_TABLE} IN SHARE MODE")
    op.execute(f"""
        CREATE TABLE {_TABLE}_new ({_columns_and_constraints(f"{_PK}_new", f"{_UQ}_new")})
        PARTITION BY RANGE (transaction_date)
    """)
    for year in range(_FIRST_YEAR, _LAST_YEAR + 1):
        op.execute(f"""
            CREATE TABLE {_TABLE}_{year} PARTITION OF {_TABLE}_new
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
        """)
    op.execute(f"CREATE TABLE {_TABLE}_default PARTITION OF {_TABLE}_new DEFAULT")

    _copy_and_verify(_TABLE, f"{_TABLE}_new")
    _swap(f"{_TABLE}_unpartitioned")
    _create_indexes()
    op.execute(f"ANALYZE {_TABLE}")


def downgrade() -> None:
    """Back to one plain table, by the same move in reverse. Loses nothing:
    every row, id and constraint comes back, along with the sequence. Any
    partition ``create-partitions`` added since goes with the rest."""
    op.execute(f"LOCK TABLE {_TABLE} IN SHARE MODE")
    op.execute(f"""
        CREATE TABLE {_TABLE}_new ({_columns_and_constraints(f"{_PK}_new", f"{_UQ}_new")})
    """)

    _copy_and_verify(_TABLE, f"{_TABLE}_new")
    _swap(f"{_TABLE}_partitioned")
    _create_indexes()
    op.execute(f"ANALYZE {_TABLE}")
