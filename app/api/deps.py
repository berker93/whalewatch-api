"""Shared FastAPI dependencies: db session, pagination, auth.

The connection pools live on ``app.state``, put there by the lifespan in
:mod:`app.main`. Handlers reach them through these dependencies rather than
importing a module-level engine, which is what lets a test point one endpoint at
a fake without touching the rest of the app::

    app.dependency_overrides[get_engine] = lambda: fake_engine
"""

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

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


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
RedisDep = Annotated[Redis, Depends(get_redis)]
