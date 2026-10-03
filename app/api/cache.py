"""A response kept in Redis for a few minutes, for endpoints read far more often than they change.

The response is built once, serialised, and served as those bytes to every
request until the key expires, from whichever API process asks. The same
lifetime goes out as ``Cache-Control``, counted down from when the response was
built, so a browser that keeps it does not add its own minutes on top of
Redis's: a client is never shown an answer older than ``seconds``.

A cache, not a dependency. When Redis cannot be reached the response is built
for each request, as it would be without the cache, and the failure is logged:
an outage there makes these endpoints slower, not unavailable.

The key carries the release, the version and the commit, so a deploy that
changes a response's shape does not go on serving the previous release's for
the rest of the TTL. The version alone would not: it changes only when
``pyproject.toml``'s does.
"""

from collections.abc import Awaitable, Callable

from fastapi import Response
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.version import VERSION

logger = get_logger(__name__)


async def cached_json(
    redis: Redis,
    settings: Settings,
    key: str,
    build: Callable[[], Awaitable[BaseModel]],
    *,
    seconds: int,
) -> Response:
    """The JSON ``build`` returns, from the cache under ``key`` or built and cached for ``seconds``.

    :param key: Unique to the endpoint and to every parameter its answer
        depends on.
    """
    key = f"cache:{VERSION}:{settings.git_sha}:{key}"
    # str, since the client decodes responses (app.core.redis); bytes to the type checker.
    body: str | bytes | None = None
    remaining = seconds
    reachable = True
    try:
        body = await redis.get(key)
        if body is not None:
            # -2 when the key expired between the two commands: serve what was
            # read, and tell the client not to keep it.
            remaining = max(await redis.ttl(key), 0)
    except RedisError as exc:
        reachable = False
        logger.warning("cache.read_failed", key=key, exc_info=exc)

    if body is None:
        body = (await build()).model_dump_json()
        # Not after a failed read: a Redis that just timed out would most
        # likely make this request wait out a second timeout for nothing.
        if reachable:
            try:
                await redis.set(key, body, ex=seconds)
            except RedisError as exc:
                logger.warning("cache.write_failed", key=key, exc_info=exc)

    return Response(
        content=body,
        media_type="application/json",
        headers={"Cache-Control": f"public, max-age={remaining}"},
    )
