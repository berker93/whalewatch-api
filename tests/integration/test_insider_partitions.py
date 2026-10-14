"""``insider_transaction``'s partitioning (0032), against a real Postgres.

What it is (range on ``transaction_date``, a partition a year and a default),
that the keys survived it, that :func:`~app.db.partitions.ensure_partitions`
keeps it ahead of the calendar, that the feed's date-ranged query prunes, and
that the migration moves populated rows both ways without losing one.
"""

import asyncio
import re
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.api.pagination import PageParams
from app.api.routers.insiders import Filters, transactions_query
from app.db.partitions import (
    DEFAULT_PARTITION,
    FIRST_YEAR,
    ensure_partitions,
    is_partition,
    next_year,
    partition_name,
    yearly_partitions,
)
from tests.integration.test_core_schema import _execute, _run
from tests.integration.test_insider_schema import (
    _a_form4,
    _a_line,
    _an_insider,
    _an_owner,
    _load,
    partition_indexes,
)

# The last year 0032 creates. Later ones are ensure_partitions' to make.
MIGRATED_THROUGH = 2027


async def _where(session: AsyncSession, line_number: int) -> str:
    """The partition a line is stored in."""
    return str(
        await session.scalar(
            text("SELECT tableoid::regclass::text FROM insider_transaction WHERE line_number = :n"),
            {"n": line_number},
        )
    )


async def _explain(session: AsyncSession, start: date, end: date) -> str:
    """The plan of the feed, every code, between two days, with the dates
    inlined: what the planner prunes at plan time."""
    statement, _ = transactions_query(PageParams(), Filters(codes=None, start=start, end=end))
    sql = statement.compile(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"literal_binds": True},
    )
    return "\n".join((await session.execute(text(f"EXPLAIN {sql}"))).scalars().all())


def _partitions_scanned(plan: str) -> set[str]:
    return set(re.findall(r" on (insider_transaction_\w+)", plan))


# --- what it is ---------------------------------------------------------------


async def test_the_table_is_partitioned_by_range_on_the_trade_date(
    db_session: AsyncSession,
) -> None:
    key = await db_session.scalar(
        text("SELECT pg_get_partkeydef(CAST('insider_transaction' AS regclass))")
    )

    assert key == "RANGE (transaction_date)"


async def test_there_is_a_partition_a_year_from_2020_and_a_default(
    db_session: AsyncSession,
) -> None:
    bounds = dict(
        (
            await db_session.execute(
                text("""
                    SELECT child.relname, pg_get_expr(child.relpartbound, child.oid)
                    FROM pg_inherits
                    JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
                    WHERE pg_inherits.inhparent = CAST('insider_transaction' AS regclass)
                """)
            )
        )
        .tuples()
        .all()
    )

    assert await yearly_partitions(db_session) == list(range(FIRST_YEAR, MIGRATED_THROUGH + 1))
    assert bounds["insider_transaction_2024"] == (
        "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"
    )
    assert bounds[DEFAULT_PARTITION] == "DEFAULT"


async def test_the_keys_still_include_the_partition_key(db_session: AsyncSession) -> None:
    keys = dict(
        (
            await db_session.execute(
                text("""
                    SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
                    WHERE conrelid = CAST('insider_transaction' AS regclass)
                      AND contype IN ('p', 'u')
                """)
            )
        )
        .tuples()
        .all()
    )

    assert keys == {
        "pk_insider_transaction": "PRIMARY KEY (id, transaction_date)",
        "uq_insider_transaction_filing_id_line_number_transaction_date": (
            "UNIQUE (filing_id, line_number, transaction_date)"
        ),
    }


async def test_every_index_is_on_every_partition(db_session: AsyncSession) -> None:
    """The three of 0031 and the two keys: one parent index each, with an
    index under it on each of the nine partitions."""
    partitions = MIGRATED_THROUGH - FIRST_YEAR + 2

    for index in (
        "pk_insider_transaction",
        "uq_insider_transaction_filing_id_line_number_transaction_date",
        "ix_insider_transaction_transaction_date_open_market",
        "ix_insider_transaction_security_id_transaction_date",
        "ix_insider_transaction_issuer_cik_unlinked",
    ):
        assert len(await partition_indexes(db_session, index)) == partitions, index


async def test_a_line_lands_in_its_year_and_any_other_date_in_the_default(
    db_session: AsyncSession,
) -> None:
    """A trade from 2015 reported late, and a year mistyped past every
    partition, are both loaded rather than failing their filing."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider, is_officer=True)

    await _load(
        db_session,
        [
            _a_line(filing, insider, 1, transaction_date=date(2026, 6, 20)),
            _a_line(filing, insider, 2, transaction_date=date(2015, 3, 2)),
            _a_line(filing, insider, 3, transaction_date=date(2062, 6, 20)),
        ],
    )

    assert await _where(db_session, 1) == "insider_transaction_2026"
    assert await _where(db_session, 2) == DEFAULT_PARTITION
    assert await _where(db_session, 3) == DEFAULT_PARTITION


async def test_a_reloaded_filing_still_conflicts_on_the_natural_key(
    db_session: AsyncSession,
) -> None:
    """The dedup constraint is enforced per partition, which is enough because
    the date is in it: the same line has the same date, so the same partition."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider, is_officer=True)
    lines = [
        _a_line(filing, insider, 1, transaction_date=date(2024, 6, 20)),
        _a_line(filing, insider, 2, transaction_date=date(2026, 6, 20)),
        _a_line(filing, insider, 3, transaction_date=date(2015, 6, 20)),
    ]

    for _ in range(3):
        await _load(db_session, lines)

    assert await db_session.scalar(text("SELECT count(*) FROM insider_transaction")) == 3


async def test_deleting_a_filing_cascades_to_its_lines_in_every_partition(
    db_session: AsyncSession,
) -> None:
    """The loader's ``--force`` path and an amendment's: lines go with their
    owner, through the composite foreign key, whichever partition they are in."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider, is_officer=True)
    await _load(
        db_session,
        [
            _a_line(filing, insider, 1, transaction_date=date(2021, 1, 4)),
            _a_line(filing, insider, 2, transaction_date=date(2026, 6, 20)),
            _a_line(filing, insider, 3, transaction_date=date(2015, 6, 20)),
        ],
    )

    await db_session.execute(
        text("DELETE FROM reporting_owner WHERE filing_id = :id"), {"id": filing.id}
    )

    assert await db_session.scalar(text("SELECT count(*) FROM insider_transaction")) == 0


# --- keeping ahead of the calendar --------------------------------------------


async def test_ensure_partitions_creates_the_missing_years_and_nothing_else(
    db_session: AsyncSession,
) -> None:
    created = await ensure_partitions(db_session, through=2029)
    again = await ensure_partitions(db_session, through=2029)

    assert [(p.year, p.rows_moved) for p in created] == [(2028, 0), (2029, 0)]
    assert again == []
    assert await yearly_partitions(db_session) == list(range(FIRST_YEAR, 2030))


async def test_a_new_year_takes_its_lines_from_the_default(db_session: AsyncSession) -> None:
    """The one thing a default partition makes harder: Postgres refuses a new
    range over rows the default holds. They move, with their ids, and lines of
    other dates stay where they were."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider, is_officer=True)
    await _load(
        db_session,
        [
            _a_line(filing, insider, 1, transaction_date=date(2028, 2, 1)),
            _a_line(filing, insider, 2, transaction_date=date(2028, 11, 30)),
            _a_line(filing, insider, 3, transaction_date=date(2015, 6, 20)),
        ],
    )
    ids = set(await db_session.scalars(text("SELECT id FROM insider_transaction")))

    created = await ensure_partitions(db_session, through=2028)

    assert [(p.name, p.rows_moved) for p in created] == [("insider_transaction_2028", 2)]
    assert await _where(db_session, 1) == "insider_transaction_2028"
    assert await _where(db_session, 2) == "insider_transaction_2028"
    assert await _where(db_session, 3) == DEFAULT_PARTITION
    assert set(await db_session.scalars(text("SELECT id FROM insider_transaction"))) == ids
    assert (
        await db_session.scalar(
            text("SELECT value_usd FROM insider_transaction WHERE line_number = 1")
        )
        == 70600
    )


async def test_a_new_partition_gets_every_index(db_session: AsyncSession) -> None:
    await ensure_partitions(db_session, through=2028)

    for index in (
        "pk_insider_transaction",
        "ix_insider_transaction_transaction_date_open_market",
    ):
        names = await partition_indexes(db_session, index)
        assert any(name.startswith("insider_transaction_2028_") for name in names), index


def test_next_year_and_the_names_autogenerate_ignores() -> None:
    assert next_year(date(2026, 12, 31)) == 2027
    assert partition_name(2026) == "insider_transaction_2026"
    assert is_partition("insider_transaction_2026")
    assert is_partition(DEFAULT_PARTITION)
    assert not is_partition("insider_transaction")
    assert not is_partition("insider_transaction_new")


# --- pruning ------------------------------------------------------------------


async def test_a_date_ranged_feed_reads_only_its_year(db_session: AsyncSession) -> None:
    """The ticket's point, as a test: September 2026 scans 2026's partition
    and no other; a range across a year end, the two it spans."""
    one_month = await _explain(db_session, date(2026, 9, 1), date(2026, 9, 30))
    year_end = await _explain(db_session, date(2025, 12, 15), date(2026, 1, 15))

    assert _partitions_scanned(one_month) == {"insider_transaction_2026"}
    assert _partitions_scanned(year_end) == {"insider_transaction_2025", "insider_transaction_2026"}


async def test_an_open_ended_range_cannot_prune_the_default(db_session: AsyncSession) -> None:
    """``from`` alone could match a mistyped future date, which only the
    default can hold, so it is read along with the years from ``from`` on."""
    statement, _ = transactions_query(
        PageParams(), Filters(codes=None, start=date(2026, 9, 1), end=None)
    )
    sql = statement.compile(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        compile_kwargs={"literal_binds": True},
    )
    plan = "\n".join((await db_session.execute(text(f"EXPLAIN {sql}"))).scalars().all())

    assert _partitions_scanned(plan) == {
        "insider_transaction_2026",
        "insider_transaction_2027",
        DEFAULT_PARTITION,
    }


async def test_bound_dates_prune_when_the_plan_is_generic(db_session: AsyncSession) -> None:
    """The API binds the dates, and a prepared statement's generic plan does
    not know them. Postgres prunes again when execution starts, and says how
    many partitions it dropped."""
    await db_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await db_session.execute(
        text("""
            PREPARE dated (date, date) AS
            SELECT id FROM insider_transaction
            WHERE transaction_date >= $1 AND transaction_date <= $2
            ORDER BY transaction_date DESC, id DESC LIMIT 50
        """)
    )

    plan = "\n".join(
        (
            await db_session.execute(
                text("EXPLAIN (ANALYZE, COSTS OFF) EXECUTE dated('2026-09-01', '2026-09-30')")
            )
        )
        .scalars()
        .all()
    )
    await db_session.execute(text("DEALLOCATE dated"))

    assert "Subplans Removed: 8" in plan
    assert _partitions_scanned(plan) == {"insider_transaction_2026"}


# --- the migration, with rows in it -------------------------------------------


@pytest.fixture
def scratch_database(pg_url: str) -> Iterator[str]:
    """A second database on the container, for test_core_schema.py's reason:
    the session's own cannot be taken down to 0031 under the other tests."""
    name = "insider_partition_round_trip"
    admin_url = pg_url.rsplit("/", 1)[0] + "/postgres"

    async def run(statement: str) -> None:
        engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    try:
        yield pg_url.rsplit("/", 1)[0] + f"/{name}"
    finally:
        asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


_SEED = """
    WITH f AS (
        INSERT INTO filing (accession_no, cik, form_type, filed_at, value_multiplier)
        VALUES ('0001197647-26-000042', '0001045810', '4', '2026-06-23 21:05+00', 1)
        RETURNING id
    ), i AS (
        INSERT INTO insider (cik, name) VALUES ('0001197647', 'HUANG JEN HSUN') RETURNING id
    ), o AS (
        INSERT INTO reporting_owner (filing_id, insider_id, is_officer)
        SELECT f.id, i.id, true FROM f, i RETURNING filing_id, insider_id
    )
    INSERT INTO insider_transaction (
        transaction_date, filing_id, insider_id, line_number, issuer_cik, issuer_name,
        is_derivative, transaction_code, acquired_disposed, shares, price_per_share
    )
    SELECT day, o.filing_id, o.insider_id, n, '0001045810', 'NVIDIA CORP',
           false, 'S', 'D', 500, 141.20
    FROM o, unnest(ARRAY[DATE '2015-03-02', '2021-01-04', '2026-06-20', '2026-06-21'])
         WITH ORDINALITY AS lines (day, n)
"""

_ROWS = """
    SELECT string_agg(
        concat_ws('|', tableoid::regclass, id, transaction_date, line_number, value_usd),
        ',' ORDER BY id
    )
    FROM insider_transaction
"""


def test_the_migration_moves_every_row_both_ways(scratch_database: str) -> None:
    """Rows written before 0032 are in the right partitions after it, with the
    same ids and values, and in the plain table again after the downgrade.
    The sequence goes on from where it was in both directions."""
    _run(scratch_database, lambda config: command.upgrade(config, "0031"))
    _execute(scratch_database, _SEED)
    unpartitioned = str(_execute(scratch_database, _ROWS))

    _run(scratch_database, lambda config: command.upgrade(config, "0032"))
    partitioned = str(_execute(scratch_database, _ROWS))
    next_id = _execute(scratch_database, "SELECT nextval('insider_transaction_id_seq')")

    _run(scratch_database, lambda config: command.downgrade(config, "0031"))
    restored = str(_execute(scratch_database, _ROWS))
    is_partitioned = _execute(
        scratch_database,
        "SELECT relkind = 'p' FROM pg_class WHERE relname = 'insider_transaction'",
    )
    leftovers = _execute(
        scratch_database,
        "SELECT count(*) FROM pg_class WHERE relname LIKE 'insider_transaction\\_%'"
        " AND relkind IN ('r', 'p')",
    )

    assert unpartitioned == (
        "insider_transaction|1|2015-03-02|1|70600.00,"
        "insider_transaction|2|2021-01-04|2|70600.00,"
        "insider_transaction|3|2026-06-20|3|70600.00,"
        "insider_transaction|4|2026-06-21|4|70600.00"
    )
    assert partitioned == (
        "insider_transaction_default|1|2015-03-02|1|70600.00,"
        "insider_transaction_2021|2|2021-01-04|2|70600.00,"
        "insider_transaction_2026|3|2026-06-20|3|70600.00,"
        "insider_transaction_2026|4|2026-06-21|4|70600.00"
    )
    assert next_id == 5
    assert restored == unpartitioned
    assert is_partitioned is False
    assert leftovers == 0
