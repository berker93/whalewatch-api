"""SQLAlchemy engine construction.

The engine is created from a ``Settings`` instance and handed to the caller
rather than built at import time. An engine opened at import binds a connection
pool to whatever event loop happens to import the module first — which in tests
is the collector's, not the one the test runs on — and gives no one a place to
substitute a different database. The app's engine is owned by the lifespan in
:mod:`app.main`; tests build their own.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
