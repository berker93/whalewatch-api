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

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.accounts import entitlements, usage
from app.accounts.sessions import SessionStore, SessionStoreError
from app.accounts.usage import UsageMeter
from app.admin.auth import admin_store
from app.api.entitlements import GatedResponseMiddleware
from app.api.errors import (
    RATE_LIMITED,
    entitlements_unavailable,
    http_error,
    sessions_unavailable,
    validation_error,
)
from app.api.metering import METERED, MeteredDep, QuotaHeadersMiddleware
from app.api.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    OriginCheckMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    TimeoutMiddleware,
    UnhandledErrorMiddleware,
    request_id_on_server_error,
)
from app.api.rate_limit import (
    ATTEMPTS_KEY_PREFIX,
    AUTH_KEY_PREFIX,
    RateLimitMiddleware,
    SlidingWindowLimiter,
)
from app.api.routers import (
    account,
    admin,
    api_keys,
    auth,
    billing,
    filings,
    health,
    insiders,
    investors,
    market,
    me,
    meta,
    notifications,
    portfolio,
    saved_filters,
    search,
    stocks,
    timeline,
    watchlist,
)
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
    app.state.auth_rate_limiter = SlidingWindowLimiter(
        app.state.redis, settings.auth_rate_limit_per_minute, prefix=AUTH_KEY_PREFIX
    )
    app.state.attempt_limiter = SlidingWindowLimiter(
        app.state.redis,
        settings.auth_attempt_limit,
        settings.auth_attempt_window_seconds,
        prefix=ATTEMPTS_KEY_PREFIX,
    )
    app.state.sessions = SessionStore(app.state.redis)
    # The admin console's, under other keys: a user's cookie names none of them.
    app.state.admin_sessions = admin_store(app.state.redis, settings)
    # What every plan allows, loaded in the background and kept fresh, so no
    # request reads it from Postgres and a Postgres down at boot does not stop
    # the app starting.
    catalog_refresh = asyncio.create_task(
        entitlements.keep_fresh(app.state.session_factory, settings.entitlements_refresh_seconds)
    )
    # The API keys' monthly calls, counted in Redis and written to Postgres
    # every few minutes, and once more below as the process exits.
    app.state.meter = UsageMeter(app.state.redis)
    usage_flush = asyncio.create_task(
        usage.keep_flushing(
            app.state.redis, app.state.session_factory, settings.usage_flush_seconds
        )
    )
    try:
        yield
    finally:
        for task in (catalog_refresh, usage_flush):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            await usage.flush_quietly(app.state.redis, app.state.session_factory)
        finally:
            # In a finally, so an exception during serving, or in the last
            # flush, still returns the connections rather than leaving
            # Postgres to reap them on its own.
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
    # Innermost: they read what the route marked, and need nothing of the others.
    app.add_middleware(GatedResponseMiddleware)
    # Inside the rate limit, which leaves the quota's X-RateLimit-* standing.
    app.add_middleware(QuotaHeadersMiddleware)
    app.add_middleware(TimeoutMiddleware, seconds=settings.request_timeout_seconds)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(UnhandledErrorMiddleware)
    # Inside CORS, so its 403 carries the headers a browser needs to read it.
    app.add_middleware(OriginCheckMiddleware, allowed=settings.cors_origins or [])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or [],
        # The reads, and the account, auth and watchlist writes. Only the
        # allow-listed origins may send them, and OriginCheckMiddleware refuses
        # a write from anywhere else even if a browser sent it.
        allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
        # Not safelisted, so without these a browser revalidating by ETag,
        # passing on a trace id, or sending a JSON body would be refused at the
        # preflight.
        allow_headers=["If-None-Match", REQUEST_ID_HEADER, "Content-Type"],
        # What a script on the frontend may read off a response. Cache-Control
        # is safelisted already; these are not.
        expose_headers=[
            "ETag",
            "Retry-After",
            REQUEST_ID_HEADER,
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
        # The session cookie (app.api.auth). Safe only because the origins are
        # an allow-list: Settings refuses "*", which with credentials would let
        # any site act as whoever is signed in.
        allow_credentials=True,
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
    app.add_exception_handler(SessionStoreError, sessions_unavailable)
    app.add_exception_handler(entitlements.EntitlementsUnavailableError, entitlements_unavailable)

    # The probes are not rate limited; everything else documents its 429. The
    # data is what an API key reads, and each request made with one spends a
    # call of its month (app.api.metering); the account is the session's.
    app.include_router(health.router)
    app.include_router(
        filings.router, responses={**RATE_LIMITED, **METERED}, dependencies=[MeteredDep]
    )
    for router in (investors, portfolio, stocks, insiders, market, search, meta, timeline):
        app.include_router(
            router.router,
            prefix="/v1",
            responses={**RATE_LIMITED, **METERED},
            dependencies=[MeteredDep],
        )
    for router in (auth, me, notifications, account, watchlist, saved_filters, api_keys, billing):
        app.include_router(router.router, prefix="/v1", responses=RATE_LIMITED)
    # The admin console: outside /v1, which is the product's, and on its own sessions.
    app.include_router(admin.router, responses=RATE_LIMITED)
    # Paddle's, not a client's: outside /v1, the OpenAPI document and the rate limit.
    app.include_router(billing.webhook_router)

    return app


# At import, not inside a request handler. Building Settings validates the whole
# environment, so a missing SEC_CONTACT_EMAIL or an out-of-range rate limit stops
# uvicorn here with a ValidationError naming the field — rather than surfacing
# hours later inside the first Celery task that happened to need it.
app = create_app(get_settings())
