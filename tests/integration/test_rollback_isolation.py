"""The fixture contract: whatever a test writes, the next test cannot see.

This is the property the whole integration suite rests on. If it stops holding,
failures stop being reproducible — a test passes alone and fails in the suite,
or passes on your machine and fails in CI where collection order differs — and
the usual fix is a pile of manual cleanup in every test. So it is asserted
directly, here, rather than assumed.

The two tests in the middle are a deliberate, order-dependent pair: the first
writes, the second asserts the write is gone. Read them together.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import SessionDep

# A scratch table, created once per session and committed, so that rows written
# into it during a test are the only thing the rollback has to undo. It is
# raw DDL and not a model on purpose: putting it in Base.metadata would make it
# visible to Alembic autogenerate, which would then propose a migration
# creating a table that exists only in tests.
PROBE_TABLE = "rollback_probe"


def _run_ddl(engine: AsyncEngine, statement: str) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(text(statement))

    asyncio.run(run())


@pytest.fixture(scope="session")
def probe_table(migrated_engine: AsyncEngine) -> Iterator[None]:
    """Create the scratch table outside any test's transaction.

    On its own connection, and committed — if this ran inside the per-test
    transaction the table would disappear along with the rows, and the second
    test below would fail with "relation does not exist" for the wrong reason.
    """
    _run_ddl(migrated_engine, f"CREATE TABLE {PROBE_TABLE} (id serial PRIMARY KEY, note text)")
    yield
    _run_ddl(migrated_engine, f"DROP TABLE {PROBE_TABLE}")


async def _count(session: AsyncSession) -> int:
    result = await session.execute(text(f"SELECT count(*) FROM {PROBE_TABLE}"))
    return int(result.scalar_one())


# --- the pair ---------------------------------------------------------------


async def test_a_row_written_here_is_visible_here(
    db_session: AsyncSession, probe_table: None
) -> None:
    """Half one: write, and commit, exactly as production code would.

    The commit is the point. A fixture that only isolates tests which never
    commit isolates nothing, because every handler in this codebase commits its
    own unit of work. This one succeeds — the session sees its row afterwards —
    and is still undone by the time the next test runs.
    """
    await db_session.execute(text(f"INSERT INTO {PROBE_TABLE} (note) VALUES ('written')"))
    await db_session.commit()

    assert await _count(db_session) == 1


async def test_the_next_test_sees_an_empty_table(
    db_session: AsyncSession, probe_table: None
) -> None:
    """Half two, and the assertion that matters: the row above is gone.

    Nothing truncated it. The connection the previous test held was rolled back
    when its fixture tore down, taking the committed savepoint with it.
    """
    assert await _count(db_session) == 0


# --- the same property, through the API -------------------------------------


async def test_a_write_made_through_the_client_lands_in_the_test_transaction(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, probe_table: None
) -> None:
    """A request handler and the test share one session, and one transaction.

    Which is what makes an end-to-end assertion possible: POST through the API,
    then read the result back through the session, without a second connection
    that would be outside the transaction and see nothing. The handler commits,
    as a real one does.
    """

    @app.post("/probe")
    async def write_probe(session: SessionDep) -> dict[str, str]:
        await session.execute(text(f"INSERT INTO {PROBE_TABLE} (note) VALUES ('via-api')"))
        await session.commit()
        return {"status": "written"}

    response = await client.post("/probe")

    assert response.status_code == 200
    result = await db_session.execute(text(f"SELECT note FROM {PROBE_TABLE}"))
    assert result.scalars().all() == ["via-api"]


async def test_the_api_write_is_rolled_back_too(
    db_session: AsyncSession, probe_table: None
) -> None:
    """The other half of the test above, for the same reason as the pair."""
    assert await _count(db_session) == 0
