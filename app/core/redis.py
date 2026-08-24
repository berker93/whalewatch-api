"""Redis connection pool construction.

Same rule as :mod:`app.db.session`: built from settings, owned by the caller.
``Redis.from_url`` creates a connection pool, not a connection, so this is cheap
and does no I/O until the first command.
"""

from redis.asyncio import Redis

from app.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    """Build the async Redis client for caching and rate-limit state."""
    return Redis.from_url(
        settings.redis_url,
        # Bytes out of Redis are a paper cut in every call site downstream; this
        # codebase only ever stores text and JSON.
        decode_responses=True,
        # Without these two, a Redis that accepts the TCP connection but never
        # answers hangs the caller indefinitely. The readiness probe has its own
        # outer deadline, but everything else that touches Redis does not.
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
