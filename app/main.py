"""FastAPI application entrypoint.

The app is built by a factory rather than assembled at module level. A
module-level app is configured by whatever the environment happened to hold at
import, which means a test wanting a *different* configuration — production-mode
docs, a fake engine — has to mutate global state and put it back. ``create_app``
takes its settings as an argument, so a test builds the app it wants and throws
it away.

``app`` below is the one uvicorn and celery import (``app.main:app``); it is just
``create_app`` applied to the process-wide settings.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers import health
from app.core.config import Settings, get_settings
from app.core.redis import create_redis
from app.core.version import VERSION
from app.db.session import create_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the connection pools for exactly as long as the app is serving.

    Pools are created here, not at import: an engine built at import binds to
    whichever event loop imported the module, and nothing ever closes it — which
    on shutdown means connections Postgres has to time out on its own, and in
    tests means a "attached to a different loop" failure three files away.

    Neither constructor performs I/O; both are lazy pools. That is deliberate.
    The app starts even when Postgres is unreachable, and reports the fact
    through /ready instead of crash-looping before it can serve the probe that
    would explain the problem.
    """
    settings: Settings = app.state.settings
    app.state.engine = create_engine(settings)
    app.state.redis = create_redis(settings)
    try:
        yield
    finally:
        # In a finally, so an exception during serving still returns the
        # connections rather than leaving Postgres to reap them on its own.
        await app.state.engine.dispose()
        await app.state.redis.aclose()


def create_app(settings: Settings) -> FastAPI:
    """Build an app bound to ``settings``."""
    # Interactive docs render every route, model and example this service has —
    # a free map of the API for anyone who finds the URL. Useful everywhere we
    # control the audience, off in production. openapi_url goes too: leaving the
    # schema served while hiding /docs only hides the HTML page.
    docs_enabled = settings.environment != "production"

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=VERSION,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Read back by the dependencies in app.api.deps, and by the lifespan above —
    # which is why this has to happen before the app is started, not inside it.
    app.state.settings = settings

    app.include_router(health.router)

    return app


# At import, not inside a request handler. Building Settings validates the whole
# environment, so a missing SEC_CONTACT_EMAIL or an out-of-range rate limit stops
# uvicorn here with a ValidationError naming the field — rather than surfacing
# hours later inside the first Celery task that happened to need it.
app = create_app(get_settings())
