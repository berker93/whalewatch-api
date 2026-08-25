"""Shared FastAPI dependencies: db session, pagination, auth.

The connection pools live on ``app.state``, put there by the lifespan in
:mod:`app.main`. Handlers reach them through these dependencies rather than
importing a module-level engine, which is what lets a test point one endpoint at
a fake without touching the rest of the app::

    app.dependency_overrides[get_engine] = lambda: fake_engine
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    """The settings this app was built with.

    Deliberately not ``get_settings()``: an app built by ``create_app(other)``
    must answer for ``other``, or the factory's whole point — a test app that
    differs from the process-wide config — quietly stops working.
    """
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> AsyncEngine:
    """The application's SQLAlchemy engine."""
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_redis(request: Request) -> Redis:
    """The application's Redis client."""
    redis: Redis = request.app.state.redis
    return redis


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The application's session factory, built by the lifespan."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the request ends — always.

    This is the only way a handler should obtain a session. The lifetime is the
    request's, not the application's: a session held across requests keeps a
    connection checked out of a pool sized for concurrency, and carries one
    request's identity map and open transaction into the next.

    The ``async with`` is what makes "always" true. Closing returns the
    connection to the pool and rolls back anything uncommitted, and it runs on
    the error path too — FastAPI throws a handler's exception back in at the
    ``yield``, so a request that raises after a partial write releases its locks
    here instead of holding them until the pool recycles the connection.

    Committing is not this dependency's job. An implicit commit-on-success turns
    every 200 into a write barrier and commits work a handler may have
    abandoned; handlers commit their own unit of work, and a handler that never
    commits has, correctly, written nothing.
    """
    factory = get_session_factory(request)
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
