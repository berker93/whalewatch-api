"""SQLAlchemy engine and session-factory construction.

The engine is created from a ``Settings`` instance and handed to the caller
rather than built at import time. An engine opened at import binds a connection
pool to whatever event loop happens to import the module first — which in tests
is the collector's, not the one the test runs on — and gives no one a place to
substitute a different database. The app's engine is owned by the lifespan in
:mod:`app.main`; tests build their own.

Sessions follow the same rule one level down. An ``AsyncSession`` wraps a single
checked-out connection and is emphatically not concurrency-safe: two coroutines
issuing statements on one session interleave on the same connection and get
``InterfaceError: another operation is in progress``, or worse, one request's
uncommitted writes flushed inside another's transaction. So there is exactly one
way to get a session per request (:func:`app.api.deps.get_session`) and one way
to get a session outside a request (:func:`session_scope`), and neither hands
out anything a caller could accidentally share.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the application database."""
    return create_async_engine(
        settings.database_url,
        # Postgres and anything between us and it (pgbouncer, an NLB idle
        # timeout) will drop a pooled connection that has been idle long enough.
        # pre_ping spends one round trip to find out before a request does, and
        # recycling below the common 30-minute idle cutoffs means it rarely has
        # to.
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
        # A checkout that cannot be served is a request that should fail, not one
        # that should queue behind an exhausted pool until the client gives up.
        pool_timeout=10,
        # Never echo: SQLAlchemy's echo logs bound parameters, which for this
        # database means filing bodies and, at connect time, the DSN.
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory bound to ``engine``.

    Cheap — a factory holds configuration, not a connection — but built once per
    engine and stored on ``app.state`` so the settings below are impossible to
    get wrong at a call site.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        # After commit, the default expires every loaded attribute, so touching
        # ``user.email`` on the way out emits a fresh SELECT — which in an async
        # session is lazy I/O in a property access, i.e. a MissingGreenlet at the
        # worst possible moment (usually inside response serialization). We
        # commit and *then* serialize, so the objects have to survive the commit.
        expire_on_commit=False,
        # Autoflush turns an innocent SELECT into a write of whatever half-built
        # objects are in the identity map. Flushing is cheap to ask for and
        # painful to get by surprise; call sites flush or commit deliberately.
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session for work outside the request cycle: CLI commands, Celery tasks.

    Deliberately *not* the request factory. Background work has no lifespan to
    own a pool for it, and the usual Celery shape — ``asyncio.run(coro())`` per
    task — creates and destroys an event loop each time. An engine cached across
    those tasks would hand out connections bound to a loop that has already
    closed, which is the real source of the ``InterfaceError`` / "attached to a
    different loop" reports that get blamed on async SQLAlchemy itself. So this
    owns an engine for the duration of the scope and disposes it on the way out.

    That costs a fresh connect per invocation. For a per-minute task that is
    noise; for a hot loop, build one engine with :func:`create_engine`, hold it
    for the life of the loop, and use :func:`create_session_factory` directly.

    Commits on clean exit and rolls back on exception, because a CLI command or
    task is a single unit of work with no HTTP layer to decide otherwise::

        async with session_scope(get_settings()) as session:
            session.add(Filing(...))
    """
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                # Explicit, though ``__aexit__`` would also roll back: a task
                # cancelled mid-write should release its locks now, not whenever
                # the pool gets around to recycling the connection.
                await session.rollback()
                raise
    finally:
        # In a finally so a failed task still returns its connections instead of
        # leaving Postgres to reap them.
        await engine.dispose()
