"""The yearly partitions of ``insider_transaction``, kept ahead of the calendar.

``insider_transaction`` is ``PARTITION BY RANGE (transaction_date)`` (0032):
one partition per calendar year from :data:`FIRST_YEAR`, named
``insider_transaction_2026``, and ``insider_transaction_default`` for every date
no year's partition covers.

The default partition is a net, not a home. It catches two kinds of line: a
trade before 2020 reported years late, and a date mistyped into a year that has
no partition yet. Without it either one fails its filing's insert, and the
loader fails the whole filing on one bad line. The price of the net: a range
partition cannot be created over rows the default already holds, so
:func:`ensure_partitions` moves them into the new year first.

**What keeps the years ahead.** :func:`ensure_partitions`, which ``ingest-form4``
calls before every run, and the ``create-partitions`` verb for doing it by hand.
Both create every missing year through next year. So the year a trade lands in
already has its partition, and the default only ever holds the two kinds above.
No ``pg_partman``: the extension is not in the ``postgres`` image, and what it
would automate here is one ``CREATE TABLE ... PARTITION OF`` a year.

**Why yearly.** The feed's date-ranged queries are days to months wide, so a
year prunes all but one or two partitions. Monthly would be more partitions to
plan than the table has years of data, for no pruning a reader would see.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

#: The partitioned table.
TABLE: Final = "insider_transaction"

#: The first year with a partition of its own. Earlier dates go to the default.
FIRST_YEAR: Final = 2020

#: The partition for every date the yearly ones do not cover.
DEFAULT_PARTITION: Final = f"{TABLE}_default"

#: The advisory lock :func:`ensure_partitions` holds until it commits, so two
#: runs do not both create one year. Any 64-bit number no other lock in this
#: database uses; these are the bytes of ``wwpartit``.
PARTITION_LOCK: Final = int.from_bytes(b"wwpartit", "big")

_YEARLY = re.compile(rf"^{TABLE}_(\d{{4}})$")

# Every column but value_usd, which is generated and cannot be inserted.
_COPIED = (
    "id, transaction_date, filing_id, insider_id, line_number, issuer_cik, issuer_name, "
    "issuer_ticker, security_id, security_title, is_derivative, transaction_code, "
    "acquired_disposed, shares, price_per_share, shares_owned_after, is_10b5_1, footnotes"
)


def partition_name(year: int) -> str:
    """``insider_transaction_2026``."""
    return f"{TABLE}_{year}"


def is_partition(name: str) -> bool:
    """Whether a table is one of ``insider_transaction``'s partitions. Alembic's
    autogenerate sees each as a table no model declares, and would otherwise
    propose dropping it."""
    return name == DEFAULT_PARTITION or _YEARLY.match(name) is not None


@dataclass(frozen=True, slots=True)
class CreatedPartition:
    """One year :func:`ensure_partitions` created."""

    year: int
    rows_moved: int
    """Lines the default partition held for this year, moved into it."""

    @property
    def name(self) -> str:
        return partition_name(self.year)


async def yearly_partitions(session: AsyncSession) -> list[int]:
    """The years that have a partition, in order."""
    names = await session.scalars(
        text("""
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            WHERE pg_inherits.inhparent = CAST(:table AS regclass)
        """),
        {"table": TABLE},
    )
    return sorted(int(match[1]) for name in names if (match := _YEARLY.match(name)))


async def ensure_partitions(session: AsyncSession, *, through: int) -> list[CreatedPartition]:
    """Create every missing year's partition from :data:`FIRST_YEAR` through
    ``through``, and return what was created. Nothing, almost always.

    In the caller's transaction, under :data:`PARTITION_LOCK`. Creating a
    partition locks ``insider_transaction`` against reads until the commit, so
    commit soon after. When every year exists, only the advisory lock and a
    catalog read are taken.
    """
    await session.execute(select(func.pg_advisory_xact_lock(PARTITION_LOCK)))
    existing = set(await yearly_partitions(session))
    return [
        await _create(session, year)
        for year in range(FIRST_YEAR, through + 1)
        if year not in existing
    ]


def next_year(today: date | None = None) -> int:
    """The last year :func:`ensure_partitions` is asked for by default."""
    return (today or date.today()).year + 1


async def _create(session: AsyncSession, year: int) -> CreatedPartition:
    """One year's partition, with any of its lines the default partition holds
    moved into it.

    Postgres refuses to create a range partition while the default holds a
    row in its range ("updated partition constraint for default partition
    would be violated"), so those lines are set aside in a temporary table,
    deleted from the default, and inserted again once the year exists, which
    routes them to it. Same ids, same everything.
    """
    bounds = {"start": date(year, 1, 1), "end": date(year + 1, 1, 1)}
    in_year = "transaction_date >= :start AND transaction_date < :end"
    # The year's partition does not exist yet, so every line of the year is in
    # the default. SHARE ROW EXCLUSIVE stops a load adding one between the
    # copy and the delete; reads go on until the CREATE below.
    await session.execute(text(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE"))
    stranded = await session.scalar(
        text(f"SELECT count(*) FROM {DEFAULT_PARTITION} WHERE {in_year}"), bounds
    )
    if stranded:
        await session.execute(
            text(f"""
                CREATE TEMPORARY TABLE stranded_insider_transaction ON COMMIT DROP AS
                SELECT {_COPIED} FROM {DEFAULT_PARTITION} WHERE {in_year}
            """),
            bounds,
        )
        await session.execute(text(f"DELETE FROM {DEFAULT_PARTITION} WHERE {in_year}"), bounds)
    # DDL takes no bind parameters. Both bounds are dates built from an int.
    await session.execute(
        text(f"""
            CREATE TABLE {partition_name(year)} PARTITION OF {TABLE}
            FOR VALUES FROM ('{bounds["start"].isoformat()}') TO ('{bounds["end"].isoformat()}')
        """)
    )
    if stranded:
        await session.execute(
            text(f"""
                INSERT INTO {TABLE} ({_COPIED})
                SELECT {_COPIED} FROM stranded_insider_transaction
            """)
        )
        await session.execute(text("DROP TABLE stranded_insider_transaction"))
    return CreatedPartition(year=year, rows_moved=stranded or 0)
