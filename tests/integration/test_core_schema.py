"""The core schema, asserted against a real Postgres.

Almost nothing here can be tested anywhere else. A generated column, ``NULLS
NOT DISTINCT``, a composite foreign key and a native enum are all things that
either do not exist or silently mean something different outside Postgres, and
they are precisely the parts of this schema whose failure mode is a wrong number
rather than an exception.

The bias in what follows is towards asserting *behaviour under a second write*.
Every constraint here exists because ingestion is not write-once: filings get
re-fetched, tasks get redelivered, amendments restate periods months later. A
test that inserts one row and reads it back would pass against a schema with no
constraints at all.
"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, Security
from tests.integration.conftest import ALEMBIC_INI

# `command.upgrade` / `command.downgrade` with the revision already bound.
AlembicAction = Callable[[Config], None]

APPLE = "037833100"
MICROSOFT = "594918104"
BERKSHIRE = "0001067983"


async def _a_filer(session: AsyncSession, *, slug: str = "berkshire-hathaway") -> Filer:
    filer = Filer(name="Berkshire Hathaway Inc", slug=slug)
    session.add(filer)
    await session.flush()
    return filer


async def _a_filing(
    session: AsyncSession,
    filer: Filer,
    *,
    accession_no: str = "0001067983-24-000011",
    period: date | None = date(2024, 3, 31),
    form_type: str = "13F-HR",
) -> Filing:
    filing = Filing(
        accession_no=accession_no,
        cik=BERKSHIRE,
        filer_id=filer.id,
        form_type=form_type,
        period_of_report=period,
        filed_at=datetime(2024, 5, 15, 16, 30, tzinfo=UTC),
        value_multiplier=1,
    )
    session.add(filing)
    await session.flush()
    return filing


async def _a_security(session: AsyncSession, cusip: str = APPLE) -> Security:
    security = Security(cusip=cusip, name="APPLE INC")
    session.add(security)
    await session.flush()
    return security


# --- the idempotency key ----------------------------------------------------


async def test_one_accession_number_can_only_be_filed_once(db_session: AsyncSession) -> None:
    """The single constraint the whole ingestion pipeline rests on.

    Every task in ``app/jobs`` is keyed on an accession number and Celery
    delivers at least once, so this collision happens in normal operation — on a
    retried task, a resumed backfill, an operator re-running a quarter. Without
    the constraint it does not raise, it *duplicates*: two filings, two sets of
    holdings, and a portfolio reporting exactly twice the positions it holds.
    """
    filer = await _a_filer(db_session)
    await _a_filing(db_session, filer)

    with pytest.raises(IntegrityError, match="uq_filing_accession_no"):
        async with db_session.begin_nested():
            await _a_filing(db_session, filer)


async def test_re_ingesting_a_holding_updates_it_instead_of_adding_a_second_copy(
    db_session: AsyncSession,
) -> None:
    """The natural key has to work as an ``ON CONFLICT`` target for common stock.

    This is the ``NULLS NOT DISTINCT`` test, and it is the one worth reading. A
    common-stock line has ``put_call IS NULL``, which is most rows in the table.
    Under Postgres's default NULL handling two such rows never conflict with
    each other, so the unique constraint would be satisfied by unlimited copies
    of exactly the rows it exists to protect, ``ON CONFLICT`` would never fire,
    and re-ingesting a filing would add a second full set of positions.

    Nothing about that failure is visible except the totals doubling.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)

    async def ingest(shares: str) -> None:
        await db_session.execute(
            text("""
                INSERT INTO holding (
                    filing_id, security_id, filer_id, period_of_report, cusip,
                    value_usd, shares, sshprnamt_type, put_call
                )
                VALUES (
                    :filing_id, :security_id, :filer_id, :period, :cusip,
                    1000.00, :shares, 'SH', NULL
                )
                ON CONFLICT (filing_id, cusip, put_call, sshprnamt_type)
                DO UPDATE SET shares = EXCLUDED.shares
            """),
            {
                "filing_id": filing.id,
                "security_id": security.id,
                "filer_id": filer.id,
                "period": filing.period_of_report,
                "cusip": APPLE,
                "shares": Decimal(shares),
            },
        )

    await ingest("789000000")
    await ingest("790000000")

    rows = (await db_session.execute(text("SELECT shares FROM holding"))).scalars().all()

    assert rows == [Decimal("790000000.0000")], "re-ingest duplicated instead of upserting"


async def test_one_cusip_can_be_held_three_ways_in_a_single_filing(
    db_session: AsyncSession,
) -> None:
    """The other half of the natural key, and the reason it is four columns.

    A filer routinely reports the same CUSIP as common stock, as calls and as
    puts in one filing, and can split it again by ``SH`` and ``PRN``. A key of
    ``(filing_id, cusip)`` collapses all of them into one row on conflict and
    discards the rest — a portfolio quietly missing its entire options book.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)

    for put_call, kind in [(None, "SH"), ("Call", "SH"), ("Put", "SH"), (None, "PRN")]:
        db_session.add(
            Holding(
                filing_id=filing.id,
                security_id=security.id,
                filer_id=filer.id,
                period_of_report=filing.period_of_report,
                cusip=APPLE,
                value_usd=Decimal("1000.00"),
                shares=Decimal("1.0000"),
                sshprnamt_type=kind,
                put_call=put_call,
            )
        )
    await db_session.flush()

    count = (await db_session.execute(text("SELECT count(*) FROM holding"))).scalar_one()

    assert count == 4


# --- the generated column ---------------------------------------------------


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (date(2024, 3, 31), "2024Q1"),
        (date(2024, 6, 30), "2024Q2"),
        (date(2024, 9, 30), "2024Q3"),
        (date(2024, 12, 31), "2024Q4"),
        (date(1999, 1, 1), "1999Q1"),
    ],
)
async def test_quarter_is_computed_by_postgres_from_the_period(
    db_session: AsyncSession, period: date, expected: str
) -> None:
    """Including the format, which is the part a rewrite could quietly change.

    The expression is not the ``to_char`` the data model originally sketched —
    ``to_char`` is STABLE, and Postgres rejects a non-IMMUTABLE generation
    expression outright — so these cases also pin the replacement to producing
    identical output.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer, period=period)
    await db_session.refresh(filing)

    assert filing.quarter == expected


async def test_quarter_is_null_for_a_filing_with_no_period(db_session: AsyncSession) -> None:
    """Form 4 shares this table and describes a day, not a quarter."""
    filer = await _a_filer(db_session)
    filing = await _a_filing(
        db_session, filer, form_type="4", period=None, accession_no="0001067983-24-000098"
    )
    await db_session.refresh(filing)

    assert filing.quarter is None


async def test_quarter_cannot_be_written_to(db_session: AsyncSession) -> None:
    """The property that makes it a generated column rather than a loader's job.

    There is no code path anywhere — no bug, no migration, no operator with
    psql — that can put ``2024Q1`` on a June period. That guarantee is the
    entire reason this is not an ordinary column populated at parse time.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)

    with pytest.raises(DBAPIError, match="generated column"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE filing SET quarter = '1999Q9' WHERE id = :id"), {"id": filing.id}
            )


# --- filer identity ---------------------------------------------------------


async def test_a_filer_may_file_under_many_ciks(db_session: AsyncSession) -> None:
    """One institution, several registered entities. Routine, and permanent.

    If ``cik`` were a unique column on ``filer``, ingestion would have to either
    split one institution's history across two filer rows — the API showing two
    Berkshires with a decade each — or discard the filings made under every CIK
    but one.
    """
    filer = await _a_filer(db_session)
    db_session.add_all(
        [
            FilerCik(filer_id=filer.id, cik=BERKSHIRE),
            FilerCik(filer_id=filer.id, cik="0000109694"),
        ]
    )
    await db_session.flush()

    ciks = (
        await db_session.execute(
            text("SELECT count(*) FROM filer_cik WHERE filer_id = :id"), {"id": filer.id}
        )
    ).scalar_one()

    assert ciks == 2


async def test_a_cik_belongs_to_exactly_one_filer(db_session: AsyncSession) -> None:
    """The other direction, and the one that has to be enforced.

    Resolving a filing's CIK to a filer is only a function if this holds.
    """
    first = await _a_filer(db_session)
    second = await _a_filer(db_session, slug="an-impostor")
    db_session.add(FilerCik(filer_id=first.id, cik=BERKSHIRE))
    await db_session.flush()

    with pytest.raises(IntegrityError, match="uq_filer_cik_cik"):
        async with db_session.begin_nested():
            db_session.add(FilerCik(filer_id=second.id, cik=BERKSHIRE))
            await db_session.flush()


# --- money and quantities ---------------------------------------------------


async def test_every_money_and_share_column_is_numeric(db_session: AsyncSession) -> None:
    """Not float, and this is asserted against the live schema rather than trusted.

    These columns are summed, ranked, and compared for equality across quarters.
    Binary floating point makes a ``sum()`` depend on the order the planner
    happens to return rows in, so the same portfolio total changes when the plan
    switches to a parallel scan — and no rounding convention rescues a total
    once it has been accumulated in float.
    """
    rows = (
        await db_session.execute(
            text("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name IN (
                      'value_usd', 'shares',
                      'voting_sole', 'voting_shared', 'voting_none'
                  )
            """)
        )
    ).all()

    assert rows, "no money or share columns found — did they get renamed?"
    for table_name, column_name, data_type in rows:
        assert data_type == "numeric", f"{table_name}.{column_name} is {data_type}"


async def test_a_position_survives_the_round_trip_at_full_precision(
    db_session: AsyncSession,
) -> None:
    """A real Berkshire-sized position, to the cent, plus fractional shares.

    Fractional share counts appear after corporate actions, and ``PRN`` rows
    report a principal amount rather than a count — which is why ``shares`` is
    numeric rather than bigint.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)
    value = Decimal("135360012345.67")
    shares = Decimal("789368450.2500")

    db_session.add(
        Holding(
            filing_id=filing.id,
            security_id=security.id,
            filer_id=filer.id,
            period_of_report=filing.period_of_report,
            cusip=APPLE,
            value_usd=value,
            shares=shares,
            sshprnamt_type="SH",
        )
    )
    await db_session.flush()
    db_session.expire_all()

    stored = (await db_session.execute(text("SELECT value_usd, shares FROM holding"))).one()

    assert stored.value_usd == value
    assert stored.shares == shares


# --- invariants -------------------------------------------------------------


async def test_a_holdings_period_cannot_drift_from_its_filing(db_session: AsyncSession) -> None:
    """``holding.period_of_report`` is a copy, and the composite FK keeps it one.

    Denormalising filer and period onto the largest table in the schema is what
    keeps a join off every read path. It is also a second source of truth, which
    is a lie waiting to happen — so it is pinned to the filing by a foreign key
    rather than by a convention in the loader.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer, period=date(2024, 3, 31))
    security = await _a_security(db_session)

    with pytest.raises(IntegrityError, match="fk_holding_filing_id_period_of_report_filing"):
        async with db_session.begin_nested():
            db_session.add(
                Holding(
                    filing_id=filing.id,
                    security_id=security.id,
                    filer_id=filer.id,
                    period_of_report=date(2023, 12, 31),
                    cusip=APPLE,
                    value_usd=Decimal("1.00"),
                    shares=Decimal("1.0000"),
                    sshprnamt_type="SH",
                )
            )
            await db_session.flush()


async def test_correcting_a_filings_period_carries_its_holdings_with_it(
    db_session: AsyncSession,
) -> None:
    """``ON UPDATE CASCADE``, and the generated quarter following along.

    An amendment can correct the period on a filing that already has holdings
    loaded. Without the cascade that update fails on the foreign key, and the
    only ways forward are deleting the holdings or leaving them on the old
    period.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer, period=date(2024, 3, 31))
    security = await _a_security(db_session)
    db_session.add(
        Holding(
            filing_id=filing.id,
            security_id=security.id,
            filer_id=filer.id,
            period_of_report=date(2024, 3, 31),
            cusip=APPLE,
            value_usd=Decimal("1.00"),
            shares=Decimal("1.0000"),
            sshprnamt_type="SH",
        )
    )
    await db_session.flush()

    await db_session.execute(
        text("UPDATE filing SET period_of_report = :period WHERE id = :id"),
        {"period": date(2024, 6, 30), "id": filing.id},
    )

    holding_period = (
        await db_session.execute(text("SELECT period_of_report FROM holding"))
    ).scalar_one()
    quarter = (
        await db_session.execute(
            text("SELECT quarter FROM filing WHERE id = :id"), {"id": filing.id}
        )
    ).scalar_one()

    assert holding_period == date(2024, 6, 30)
    assert quarter == "2024Q2"


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("value_usd", Decimal("-1.00"), "ck_holding_value_usd_is_not_negative"),
        ("shares", Decimal("-1.0000"), "ck_holding_shares_is_not_negative"),
    ],
)
async def test_a_negative_quantity_is_rejected(
    db_session: AsyncSession, column: str, value: Decimal, constraint: str
) -> None:
    """13F is long-only. A negative is a sign error in the parser, not a short.

    Worth catching at the boundary: a negative that gets in nets silently
    against someone else's long in every aggregate that follows.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)
    amounts = {"value_usd": Decimal("1.00"), "shares": Decimal("1.0000"), column: value}

    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            db_session.add(
                Holding(
                    filing_id=filing.id,
                    security_id=security.id,
                    filer_id=filer.id,
                    period_of_report=filing.period_of_report,
                    cusip=APPLE,
                    sshprnamt_type="SH",
                    **amounts,
                )
            )
            await db_session.flush()


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("sshprnamt_type", "SHARES", "ck_holding_sshprnamt_type_is_known"),
        ("put_call", "Straddle", "ck_holding_put_call_is_known"),
    ],
)
async def test_the_holding_vocabularies_are_closed(
    db_session: AsyncSession, field: str, value: str, constraint: str
) -> None:
    """``SH``/``PRN`` and ``Put``/``Call``, both of which change what a row means.

    ``PRN`` is a principal amount, not a share count; an option's value is the
    notional value of the underlying, not the premium. A row whose category is
    unrecognised gets summed into whichever aggregate does not filter it out.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)
    fields = {"sshprnamt_type": "SH", field: value}

    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            db_session.add(
                Holding(
                    filing_id=filing.id,
                    security_id=security.id,
                    filer_id=filer.id,
                    period_of_report=filing.period_of_report,
                    cusip=APPLE,
                    value_usd=Decimal("1.00"),
                    shares=Decimal("1.0000"),
                    **fields,
                )
            )
            await db_session.flush()


async def test_the_value_multiplier_is_one_or_one_thousand(db_session: AsyncSession) -> None:
    """The two sides of the 2023-01-03 whole-dollars cutover, and nothing else.

    Any other value means the parser guessed. Getting this wrong is a 1000x
    error in which every filer in the quarter is wrong by the same factor, so
    rankings, percentages and quarter-over-quarter shapes all look normal.
    """
    filer = await _a_filer(db_session)

    with pytest.raises(IntegrityError, match="value_multiplier_is_one_or_one_thousand"):
        async with db_session.begin_nested():
            db_session.add(
                Filing(
                    accession_no="0001067983-24-000012",
                    cik=BERKSHIRE,
                    filer_id=filer.id,
                    form_type="13F-HR",
                    period_of_report=date(2024, 3, 31),
                    filed_at=datetime(2024, 5, 15, 16, 30, tzinfo=UTC),
                    value_multiplier=100,
                )
            )
            await db_session.flush()


async def test_a_thirteen_f_must_carry_a_period(db_session: AsyncSession) -> None:
    """A 13F without one cannot be grouped, compared, or given a quarter.

    Form 4 is exempt — it genuinely has no period — which is why the constraint
    is written as an implication on the form type rather than as ``NOT NULL``.
    """
    filer = await _a_filer(db_session)

    with pytest.raises(IntegrityError, match="thirteen_f_has_a_period_of_report"):
        async with db_session.begin_nested():
            await _a_filing(db_session, filer, period=None)


async def test_amendment_kind_is_a_closed_type(db_session: AsyncSession) -> None:
    """The enum round-trips as its value, and rejects anything outside it.

    Worth asserting the stored representation specifically: without
    ``values_callable`` SQLAlchemy writes the member *names* while the Postgres
    type carries the values, and every insert fails against a type that looks
    correct in psql.
    """
    filer = await _a_filer(db_session)
    original = await _a_filing(db_session, filer)
    amendment = Filing(
        accession_no="0001067983-24-000013",
        cik=BERKSHIRE,
        filer_id=filer.id,
        form_type="13F-HR/A",
        period_of_report=date(2024, 3, 31),
        filed_at=datetime(2024, 8, 1, 16, 30, tzinfo=UTC),
        value_multiplier=1,
        amends_id=original.id,
        amendment_kind=AmendmentKind.NEW_HOLDINGS,
    )
    db_session.add(amendment)
    await db_session.flush()

    stored = (
        await db_session.execute(
            text("SELECT amendment_kind::text FROM filing WHERE id = :id"), {"id": amendment.id}
        )
    ).scalar_one()

    assert stored == "new_holdings"

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE filing SET amendment_kind = 'partial' WHERE id = :id"),
                {"id": amendment.id},
            )


async def test_a_held_security_cannot_be_deleted(db_session: AsyncSession) -> None:
    """RESTRICT, so that a bad enrichment job cannot take positions with it."""
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session, MICROSOFT)
    db_session.add(
        Holding(
            filing_id=filing.id,
            security_id=security.id,
            filer_id=filer.id,
            period_of_report=filing.period_of_report,
            cusip=MICROSOFT,
            value_usd=Decimal("1.00"),
            shares=Decimal("1.0000"),
            sshprnamt_type="SH",
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError, match="fk_holding_security_id_security"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM security WHERE id = :id"), {"id": security.id}
            )


async def test_deleting_a_filing_takes_its_holdings(db_session: AsyncSession) -> None:
    """CASCADE in this direction, because re-parsing a bad filing is a delete.

    The opposite of the security case above, and deliberately so: the holdings
    are the filing's content, not independent facts.
    """
    filer = await _a_filer(db_session)
    filing = await _a_filing(db_session, filer)
    security = await _a_security(db_session)
    db_session.add(
        Holding(
            filing_id=filing.id,
            security_id=security.id,
            filer_id=filer.id,
            period_of_report=filing.period_of_report,
            cusip=APPLE,
            value_usd=Decimal("1.00"),
            shares=Decimal("1.0000"),
            sshprnamt_type="SH",
        )
    )
    await db_session.flush()

    await db_session.execute(text("DELETE FROM filing WHERE id = :id"), {"id": filing.id})

    remaining = (await db_session.execute(text("SELECT count(*) FROM holding"))).scalar_one()

    assert remaining == 0


# --- indexes ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "index"),
    [
        ("filing", "ix_filing_filer_id_period_of_report"),
        ("filing", "ix_filing_filed_at"),
        ("filing", "ix_filing_cik"),
        ("holding", "ix_holding_filing_id"),
        ("holding", "ix_holding_security_id"),
        ("holding", "ix_holding_cusip"),
        ("holding", "ix_holding_filer_id_period_of_report"),
        ("filer_cik", "uq_filer_cik_cik"),
        ("filing", "uq_filing_accession_no"),
    ],
)
async def test_the_index_exists(db_session: AsyncSession, table: str, index: str) -> None:
    """Named, so that a rename in a later migration has to come past this list.

    Asserted by name rather than by "some index covers these columns" because
    the names are what a future ``op.drop_index`` refers to.
    """
    found = (
        await db_session.execute(
            text("SELECT count(*) FROM pg_indexes WHERE tablename = :t AND indexname = :i"),
            {"t": table, "i": index},
        )
    ).scalar_one()

    assert found == 1


async def test_the_period_indexes_are_descending(db_session: AsyncSession) -> None:
    """The direction the AC asked for, which is the direction every caller reads in.

    Postgres can walk a B-tree backwards, so this changes no plan today. It
    starts mattering the moment a query orders these two columns in opposite
    directions, and by then the index is already right.
    """
    rows = (
        await db_session.execute(
            text("""
                SELECT indexname, indexdef FROM pg_indexes
                WHERE indexname IN
                    ('ix_filing_filed_at', 'ix_filing_filer_id_period_of_report')
            """)
        )
    ).all()
    definitions: dict[str, str] = {name: definition for name, definition in rows}

    assert "filed_at DESC" in definitions["ix_filing_filed_at"]
    assert "period_of_report DESC" in definitions["ix_filing_filer_id_period_of_report"]


# --- the migration, both directions -----------------------------------------


@pytest.fixture
def scratch_database(pg_url: str) -> Iterator[str]:
    """A second, empty database on the same container, dropped afterwards.

    The session's migrated database cannot be used for the test below: it would
    have to be torn down to ``base`` and rebuilt, which every other test in this
    package is concurrently depending on being at ``head``.
    """
    name = "core_schema_round_trip"
    admin_url = pg_url.rsplit("/", 1)[0] + "/postgres"

    async def run(statement: str) -> None:
        # AUTOCOMMIT because CREATE DATABASE and DROP DATABASE cannot run inside
        # a transaction block, which is the one place Postgres's otherwise
        # transactional DDL does not apply.
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


def test_the_chain_applies_and_reverses_and_applies_again(scratch_database: str) -> None:
    """The AC's "applies and reverses cleanly", against a real server.

    The second upgrade is the part that catches what a single ``downgrade``
    does not. A downgrade that drops the tables but leaves the
    ``amendment_kind`` type behind exits zero and looks clean; the next
    ``upgrade`` is what fails, with "type already exists" — in whatever
    situation caused the rollback, which is never a calm one.

    Deliberately a sync test: it drives Alembic through ``asyncio.run`` on a
    private loop, which cannot be done from inside a running one.
    """
    revisions_before = _run(scratch_database, lambda config: command.upgrade(config, "head"))
    _run(scratch_database, lambda config: command.downgrade(config, "base"))
    left_behind = _surviving_objects(scratch_database)
    revisions_after = _run(scratch_database, lambda config: command.upgrade(config, "head"))

    assert revisions_before == revisions_after
    assert left_behind == [], f"downgrade left objects behind: {left_behind}"


def _run(url: str, action: AlembicAction) -> list[str]:
    """Run one Alembic command against ``url`` and report the applied revisions."""

    async def go() -> list[str]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(_in_alembic, action)
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("""
                        SELECT version_num FROM alembic_version
                        UNION ALL SELECT NULL
                        WHERE NOT EXISTS (SELECT 1 FROM alembic_version)
                    """)
                )
                return [row for row in result.scalars().all() if row is not None]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _in_alembic(connection: Connection, action: AlembicAction) -> None:
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    action(config)


def _surviving_objects(url: str) -> list[str]:
    """Everything in ``public`` after a downgrade to base, ``alembic_version`` aside.

    Tables, enum types and sequences, because those are the three things a
    hand-written downgrade forgets. ``alembic_version`` is excluded because
    Alembic owns it and does not drop it.
    """

    async def go() -> list[str]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("""
                        SELECT 'table: ' || tablename FROM pg_tables
                        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                        UNION ALL
                        SELECT 'type: ' || t.typname FROM pg_type t
                        JOIN pg_namespace n ON n.oid = t.typnamespace
                        WHERE n.nspname = 'public' AND t.typtype = 'e'
                        UNION ALL
                        SELECT 'sequence: ' || sequencename FROM pg_sequences
                        WHERE schemaname = 'public'
                        ORDER BY 1
                    """)
                )
                return list(result.scalars().all())
        finally:
            await engine.dispose()

    return asyncio.run(go())
