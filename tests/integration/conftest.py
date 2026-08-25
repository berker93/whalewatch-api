"""Fixtures for tests that run against a real Postgres.

Everything under ``tests/integration`` talks to an actual PostgreSQL 16 server
in a container, started once per session and thrown away at the end. There is no
in-memory substitute here on purpose. This codebase's queries are Postgres:
aggregate ``FILTER``, window functions, ``gin_trgm_ops`` indexes, generated
columns, partitions, materialised views. SQLite parses none of it, so a suite
that ran on SQLite would be testing a different program than the one that ships
— and would go green on the query that fails in production.

Three decisions worth knowing about before you write a test here:

**The schema is built by migrations, not ``create_all``.** ``Base.metadata``
describes the schema we *think* we have; the migration chain is the one
production will actually have. Building the test database with ``upgrade head``
means a migration that fails, or that drifts from the models, fails here — in
the suite you already run — rather than during a deploy window. It also costs
nothing: the chain runs once per session, in about the time the container takes
to accept its first connection.

**Every test runs inside a transaction that is rolled back.** Not TRUNCATE
between tests, not a fresh database per test: the ``db_session`` fixture opens
one connection, begins a transaction on it, and binds the session to that
connection. Rollback at the end of the test undoes everything, in milliseconds,
whatever the test wrote. Tests therefore see an empty database no matter what
ran before them, and no matter what order they ran in.

**The engine uses NullPool.** The container and the migrated engine are
session-scoped, but every async test gets its own event loop, and an asyncpg
connection belongs to the loop that opened it. A pooled connection handed to the
next test's loop is the "attached to a different loop" failure that async
SQLAlchemy gets blamed for. NullPool means each ``connect()`` opens a fresh
connection on the current loop and closes it at the end of the test, so nothing
loop-bound ever outlives the loop it was made on. Against a container on
localhost a connect is a fraction of a millisecond, and the pooling behaviour of
the *application's* engine is asserted in tests/test_session.py, where it
belongs.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_session
from app.core.config import Settings
from app.db.session import create_session_factory
from tests.conftest import make_settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Pinned to the major version production runs, and to -alpine because the image
# is a third of the size of the Debian one and starts in about a second — which
# is most of the difference between a suite you run on every save and one you
# run before pushing. Bump this in the same commit that bumps the server.
POSTGRES_IMAGE = "postgres:16-alpine"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in this package ``integration``.

    So that ``pytest -m "not integration"`` is a complete, honest answer to "run
    the tests that do not need Docker" — without every module in here having to
    remember a ``pytestmark``, which is exactly the kind of thing that gets
    forgotten on the file where it matters.
    """
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker("integration")


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """One PostgreSQL 16 server for the whole run.

    Session-scoped because starting it is the only slow thing in this suite;
    per-test containers would turn a 5-second run into a 5-minute one and buy
    nothing, since the transaction rollback in ``db_session`` already gives each
    test a clean database.

    The credentials are passed explicitly rather than left to default.
    ``PostgresContainer`` falls back to ``os.environ["POSTGRES_USER"]`` and
    friends, which in this repo are set — they point at the compose stack — so
    defaulting would silently make the container's credentials depend on the
    developer's shell, and differ between a laptop and CI.
    """
    container = PostgresContainer(
        POSTGRES_IMAGE,
        username="whalewatch",
        password="testing",
        dbname="whalewatch_test",
    )
    with container as postgres:
        yield postgres


@pytest.fixture(scope="session")
def pg_url(pg_container: PostgresContainer) -> str:
    """The container's DSN, on the driver this application uses.

    ``get_connection_url`` defaults to psycopg2, which is not installed and is
    not what the app speaks. Asking for asyncpg here means the migrations, the
    fixtures and the application code all reach Postgres through the same
    driver, so a driver-specific behaviour (asyncpg's prepared statements, its
    type codecs) is exercised rather than mocked out.
    """
    return pg_container.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def migrated_engine(pg_url: str) -> Iterator[AsyncEngine]:
    """An engine on a container whose schema is at ``head``.

    Deliberately a *sync* fixture that reaches into ``asyncio.run`` rather than
    an async one. A session-scoped async fixture has to be pinned to a
    session-scoped event loop, which then infects every test that touches it;
    getting that wrong produces cross-loop errors in unrelated files. Here the
    migration gets a private loop that is closed before any test starts, and the
    engine it yields is loop-agnostic because it is NullPool (see the module
    docstring).
    """
    _upgrade_to_head(pg_url)

    engine = create_async_engine(pg_url, poolclass=NullPool, echo=False)
    yield engine
    # Nothing to close — NullPool holds no connections — but disposing is what
    # makes that a fact about the pool rather than an assumption about it.
    asyncio.run(engine.dispose())


def _upgrade_to_head(url: str) -> None:
    """Run ``alembic upgrade head`` against ``url``, in this process.

    In-process rather than a subprocess so a failing migration raises the real
    exception, with the real traceback, instead of a non-zero exit code and a
    wall of captured stderr.

    The engine here is thrown away immediately: it exists to open one
    connection, run DDL on it, and close it. ``run_sync`` hands Alembic the
    sync-facade Connection it expects, and ``engine.begin()`` wraps the whole
    chain in one transaction — Postgres has transactional DDL, so a failure
    halfway through leaves the container on no revision rather than a
    half-applied one.
    """

    async def run() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(_run_upgrade)
        finally:
            await engine.dispose()

    asyncio.run(run())


def _run_upgrade(connection: Connection) -> None:
    config = Config(str(ALEMBIC_INI))
    # Both read by alembic/env.py. The connection tells it not to build its own
    # from Settings — which would point at the compose database, not this
    # container. configure_logger keeps it from calling fileConfig() and
    # disabling this process's loggers on its way past.
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


@pytest.fixture
async def db_session(migrated_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose every write is rolled back when the test ends.

    The shape matters, and it is the whole trick. The transaction is begun on
    the *connection*, and the session is bound to that connection rather than to
    the engine — so the session's work happens inside a transaction the session
    does not own and cannot commit away. Bind it to the engine instead and it
    checks out its own connection, its ``commit()`` is a real commit, and the
    rows survive into the next test.

    ``join_transaction_mode="create_savepoint"`` says what happens when the code
    under test commits: the session's ``commit()`` releases a SAVEPOINT inside
    the outer transaction — durable as far as that code can tell, and gone with
    the rollback below. SQLAlchemy's default, ``"conditional_savepoint"``, picks
    the same behaviour here, but it picks it by inspecting the connection, and
    the other branch it can take (``"rollback_only"``) is one where a handler's
    commit ends the outer transaction. That is not a condition a test suite's
    isolation should be resting on, so it is stated rather than inferred.

    Teardown is ordered: close the session first (returning it to a quiet
    state), then roll back, then close the connection. Rolling back before the
    session is closed leaves the session holding a savepoint that no longer
    exists.
    """
    connection = await migrated_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        # The same two settings the application's factory uses, for the same
        # reasons (see app/db/session.py) — a test session that expires on
        # commit would raise MissingGreenlet where the app does not.
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def settings(pg_container: PostgresContainer) -> Settings:
    """Overrides the unit-suite fixture of the same name, pointed at the container.

    Every fixture in the parent conftest that builds an app builds it from
    ``settings``, so replacing this one is all it takes to make ``app`` — and
    anything reading ``settings.database_url`` — refer to the container rather
    than to the compose stack. Nothing else in the parent needs to know.
    """
    return make_settings(
        postgres_host=pg_container.get_container_host_ip(),
        postgres_port=int(pg_container.get_exposed_port(5432)),
        postgres_user=pg_container.username,
        postgres_password=SecretStr(pg_container.password),
        postgres_db=pg_container.dbname,
    )


@pytest.fixture
async def client(
    app: FastAPI, migrated_engine: AsyncEngine, db_session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """An HTTP client whose requests read and write the test's transaction.

    Also overrides the parent conftest's ``client``, which is deliberately
    database-free. Here ``get_session`` is overridden to hand every request the
    *same* session the test holds, which is what makes "POST it through the API,
    then assert on it through the session" work — the two are looking at one
    transaction, and it is rolled back when the test ends.

    That is a departure from production, where each request gets its own
    session, and it is the right one for a test: a per-request session would
    open a second connection, outside the test's transaction, and see none of
    its rows.

    ``ASGITransport`` does not run the lifespan, so the state the lifespan
    normally populates is set here instead — pointed at the container. Redis is
    not: no container is started for it, and a test that needs it should
    override ``get_redis`` with a fake.
    """
    app.state.engine = migrated_engine
    app.state.session_factory = create_session_factory(migrated_engine)
    app.dependency_overrides[get_session] = lambda: db_session

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        # The app fixture is function-scoped so this is belt-and-braces, but an
        # override left behind on a shared app is the kind of bug that shows up
        # three files away as a test passing for the wrong reason.
        app.dependency_overrides.clear()
