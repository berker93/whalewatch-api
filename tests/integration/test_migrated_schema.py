"""What the container is, and how its schema got there.

Two claims that the rest of the integration suite quietly depends on, and that
are cheap to assert once: the database is the PostgreSQL this application
targets — not a stand-in that parses a subset of its SQL — and its schema was
produced by running the migration chain, which is the same thing production
will run.
"""

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis
from tests.integration.conftest import ALEMBIC_INI, POSTGRES_IMAGE


async def test_the_server_is_the_postgres_major_version_we_target(
    db_session: AsyncSession,
) -> None:
    """A test suite on a different major version tests a different planner, a
    different set of reserved words, and in our case a different answer to
    whether ``MERGE`` or a given ``jsonb`` operator exists at all."""
    major = POSTGRES_IMAGE.removeprefix("postgres:").split("-")[0]
    version = (await db_session.execute(text("SELECT version()"))).scalar_one()

    assert str(version).startswith(f"PostgreSQL {major}.")


async def test_the_schema_is_at_the_migration_head(db_session: AsyncSession) -> None:
    """``alembic_version`` exists and names the head revision.

    Which is the difference between "the tables happen to be there" and "the
    chain ran". A schema built by ``create_all`` would pass every query test in
    this suite and still leave a broken migration to be discovered in a deploy.
    """
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    applied = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).scalars()

    assert applied.all() == [head]


async def test_postgres_only_sql_actually_runs(db_session: AsyncSession) -> None:
    """The ticket, as a test: aggregate ``FILTER`` and a window function.

    Neither parses in SQLite, and both are ordinary in this codebase's queries —
    a suite that could not run this statement would be quietly excluding most of
    what we write from ever being tested.
    """
    statement = text("""
        SELECT
            count(*) FILTER (WHERE value > 1) AS above_one,
            sum(value) OVER (ORDER BY value) AS running_total
        FROM (VALUES (1), (2), (3)) AS sample(value)
        GROUP BY value
        ORDER BY value
    """)

    rows = (await db_session.execute(statement)).all()

    assert [tuple(row) for row in rows] == [(0, 1), (1, 3), (1, 6)]


async def test_the_app_reports_ready_against_the_container(
    app: FastAPI, client: AsyncClient
) -> None:
    """/ready takes a connection out of the engine and round-trips ``SELECT 1``.

    Everywhere else in this project that check runs against a stub, so this is
    the first test in which "postgres: ok" means Postgres actually answered.

    Redis is stubbed rather than started: this suite runs no Redis container, and
    pointing the probe at localhost:6379 would make the result depend on whether
    the developer happens to have the compose stack up.
    """

    class ReachableRedis:
        async def ping(self) -> bool:
            return True

    app.dependency_overrides[get_redis] = ReachableRedis

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}
