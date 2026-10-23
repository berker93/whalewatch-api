"""Dropping the API's cached responses after a job changes what it serves."""

from __future__ import annotations

from redis.exceptions import RedisError

from app.api.cache import invalidate
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import create_redis

logger = get_logger(__name__)


async def invalidate_cache(settings: Settings) -> int | None:
    """Drop every response the API has cached. How many, or None if Redis could not be reached.

    After a commit, never before: a request between the two would cache the
    old answer again. A Redis that cannot be reached does not fail the run,
    whose data is committed and served; the error is logged, and until it
    expires what was cached is served as it was — up to a day for a closed
    period.
    """
    redis = create_redis(settings)
    try:
        deleted = await invalidate(redis)
    except RedisError as exc:
        # The message, not the traceback: a refused connection's says no more,
        # and its lines would not carry the run's id.
        logger.error("cache.invalidate_failed", error=str(exc))
        return None
    finally:
        await redis.aclose()
    logger.info("cache.invalidated", keys=deleted)
    return deleted
