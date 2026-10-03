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
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.errors import RATE_LIMITED, http_error, validation_error
from app.api.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    TimeoutMiddleware,
    UnhandledErrorMiddleware,
    request_id_on_server_error,
)
from app.api.rate_limit import RateLimitMiddleware, SlidingWindowLimiter
from app.api.routers import filings, health, investors, market, meta, portfolio, search, stocks
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.core.version import VERSION
from app.db.session import create_engine, create_session_factory


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
    # Built once here rather than per request: the factory is where
    # expire_on_commit and autoflush are decided, and one instance means no
    # handler can construct a session configured differently.
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.redis = create_redis(settings)
    app.state.rate_limiter = SlidingWindowLimiter(app.state.redis, settings.rate_limit_per_minute)
    try:
        yield
    finally:
        # In a finally, so an exception during serving still returns the
        # connections rather than leaving Postgres to reap them on its own.
        await app.state.engine.dispose()
        await app.state.redis.aclose()


def create_app(settings: Settings) -> FastAPI:
    """Build an app bound to ``settings``."""
    # Before anything else in the factory, so that whatever the lines below log
    # is already rendered by the configuration this app asked for. Logging is
    # process-global rather than per-app, so this is the one thing create_app
    # does that outlives the app it returns — see app.core.logging.
    configure_logging(settings)

    # Interactive docs render every route, model and example this service has —
    # a free map of the API for anyone who finds the URL. Useful everywhere we
    # control the audience, off in production. openapi_url goes too: leaving the
    # schema served while hiding /docs only hides the HTML page.
    docs_enabled = settings.environment != "production"

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=VERSION,
        # Never settings.debug. Starlette's debug mode answers an unhandled
        # exception with its traceback, to whoever asked; DEBUG is for log
        # verbosity, and a traceback belongs in the log, under the request id.
        debug=False,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Read back by the dependencies in app.api.deps, and by the lifespan above —
    # which is why this has to happen before the app is started, not inside it.
    app.state.settings = settings

    # Each add_middleware wraps everything added before it, so the last added
    # is the outermost: the list below reads from the route outwards. The order
    # is argued in app.api.middleware's docstring.
    app.add_middleware(TimeoutMiddleware, seconds=settings.request_timeout_seconds)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or [],
        # Everything here is a read. A preflight for anything else is refused
        # by the browser, before the request is sent.
        allow_methods=["GET", "HEAD", "OPTIONS"],
        # Not safelisted, so without these a browser revalidating by ETag, or
        # passing on a trace id, would be refused at the preflight.
        allow_headers=["If-None-Match", REQUEST_ID_HEADER],
        # What a script on the frontend may read off a response. Cache-Control
        # is safelisted already; these are not.
        expose_headers=[
            "ETag",
            "Retry-After",
            REQUEST_ID_HEADER,
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
        ],
        # No cookies, no auth: nothing a credentialed request would add.
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware, environment=settings.environment)
    # Outermost, so the request_id is bound before any other middleware runs,
    # every refusal below carries it, and the duration covers all of them.
    app.add_middleware(RequestContextMiddleware)
    # Not a middleware: the fallback for an exception raised outside
    # UnhandledErrorMiddleware, which only an exception handler can reach.
    app.add_exception_handler(Exception, request_id_on_server_error)
    # Every 4xx in one shape, the one the routes document. Starlette's
    # HTTPException rather than FastAPI's subclass, so the 404 for an unknown
    # path and the 405 that routing raises are rendered here too.
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

    # The probes are not rate limited; everything else documents its 429.
    app.include_router(health.router)
    app.include_router(filings.router, responses=RATE_LIMITED)
    for router in (investors, portfolio, stocks, market, search, meta):
        app.include_router(router.router, prefix="/v1", responses=RATE_LIMITED)

    return app


# At import, not inside a request handler. Building Settings validates the whole
# environment, so a missing SEC_CONTACT_EMAIL or an out-of-range rate limit stops
# uvicorn here with a ValidationError naming the field — rather than surfacing
# hours later inside the first Celery task that happened to need it.
app = create_app(get_settings())
