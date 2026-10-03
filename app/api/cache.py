"""Responses kept in Redis and revalidated by ETag, for endpoints read far more than they change.

An endpoint opts in with :func:`cached`. Its answer is built once, serialised,
and served as those bytes to every request with the same arguments until the
key expires or is invalidated, from whichever API process asks.

How long
--------
By what the answer is about (:class:`Lifetime`):

- A **closed period**, one whose filings are in, is kept for a day. Its data
  changes only when a late filing or an amendment is published, and publishing
  invalidates the cache (below), so the day is a backstop rather than a
  staleness anyone sees.
- The **current period**, or no period asked for, which means the latest
  published one and moves on when the next is published: five minutes.
- **Search**: a minute. **Metadata**: five minutes.

The lifetime goes out as ``Cache-Control``, counted down from when the answer
was built, so a browser that keeps it does not add its own lifetime on top of
Redis's. A closed period's also allows ``stale-while-revalidate``: past that, a
browser or CDN serves what it has at once and revalidates behind it, which the
ETag makes a 304 when nothing changed.

The ETag is a hash of the body, so it is the same for the same bytes whether
they were cached or built, in any process. A request whose ``If-None-Match``
names it gets a 304 with no body.

The key
-------
``ww:v1:{endpoint}:{hash}``. The endpoint is the handler, by its router's
module and its name: ``portfolio.read_portfolio``. Not the route's path, which
FastAPI keeps relative to the router it was declared on, without ``/v1``. The
hash is of the arguments the handler is called with, path and query, after
FastAPI has parsed and defaulted them, so ``?period=2024Q1``
and ``?period=2024-03-31`` are one entry, as are a parameter left out and the
same parameter sent as its default. A query parameter the endpoint does not
declare is not an argument, so it cannot split the cache. The hash also covers
the release, version and commit, so a deploy that changes a response's shape
does not go on serving the previous release's for the rest of a day. ``v1`` is
for invalidating everything by hand, with no deploy: bump it.

Invalidation
------------
:func:`invalidate` deletes by prefix. The CLI calls it whenever it publishes:
after a ``recompute`` commits, after ``refresh-views``, after an
``ingest-filing`` or ``backfill`` that loaded something, and after
``seed-investors`` writes. Everything, not just the periods touched: the
materialised views are refreshed whole, and nearly every response reads one.

A cache, not a dependency
-------------------------
When Redis cannot be reached the response is built for each request, as it
would be without the cache, and the failure is logged: an outage there makes
these endpoints slower, not unavailable.
"""

import functools
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Final

from fastapi import Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RedisDep, SettingsDep
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.periods import filing_deadline
from app.core.version import VERSION

logger = get_logger(__name__)

#: What every key starts with. Bump the version to drop every cached response.
KEY_PREFIX: Final = "ww:v1:"


class Lifetime(StrEnum):
    """What a response is about, which decides how long it is kept."""

    CLOSED_PERIOD = "closed_period"
    CURRENT_PERIOD = "current_period"
    SEARCH = "search"
    METADATA = "metadata"


#: Seconds each is kept, in Redis and by a client.
TTL: Final = {
    Lifetime.CLOSED_PERIOD: 24 * 60 * 60,
    Lifetime.CURRENT_PERIOD: 5 * 60,
    Lifetime.SEARCH: 60,
    Lifetime.METADATA: 5 * 60,
}

#: How long past its ``max-age`` a client may serve a closed period's answer
#: while it revalidates.
STALE_WHILE_REVALIDATE: Final = 24 * 60 * 60

#: How long after its filing deadline a period is closed. filing_deadline is
#: not rolled past weekends and holidays, so it can be three days early, and
#: today is reckoned in UTC, a day ahead of EDGAR's evening.
_CLOSING: Final = timedelta(days=4)

#: Arguments that are the handler's resources, not what it was asked.
_NOT_ARGUMENTS: Final = (AsyncSession, Redis, Settings)

type Endpoint = Callable[..., Awaitable[Any]]


def cached(
    lifetime: Lifetime | None = None, *, period: str | None = None
) -> Callable[[Endpoint], Endpoint]:
    """Serve the decorated endpoint's answer from Redis, with an ETag.

    Under ``@router.get``, over a handler that returns a pydantic model. It is
    served as that model's JSON, so the handler's ``response_model`` must be
    the model it returns.

    :param lifetime: How long every answer is kept.
    :param period: Instead of ``lifetime``, the handler's parameter that names
        the period its answer is for. A closed one is kept for a day; one not
        given, or still filling in, for five minutes.
    """
    if (lifetime is None) == (period is None):
        raise TypeError("cached takes a lifetime, or the period parameter that decides it")

    def decorate(endpoint: Endpoint) -> Endpoint:
        signature = inspect.signature(endpoint)
        if period is not None and period not in signature.parameters:
            raise TypeError(f"{endpoint.__name__} has no parameter {period!r}")
        name = f"{endpoint.__module__.rpartition('.')[2]}.{endpoint.__name__}"

        @functools.wraps(endpoint)
        async def wrapper(
            *args: Any,
            _cache_request: Request,
            _cache_redis: Redis,
            _cache_settings: Settings,
            **kwargs: Any,
        ) -> Response:
            kept = lifetime if lifetime is not None else period_lifetime(kwargs[period or ""])
            key = cache_key(name, kwargs, _cache_settings)

            body, remaining, reachable = await _read(_cache_redis, key)
            hit = body is not None
            if body is None:
                answer = await endpoint(*args, **kwargs)
                if not isinstance(answer, BaseModel):
                    raise TypeError(f"{endpoint.__name__} returned {type(answer).__name__}")
                body = answer.model_dump_json()
                remaining = TTL[kept]
                # Not after a failed read: a Redis that just timed out would
                # most likely make this request wait out a second timeout for
                # nothing.
                if reachable:
                    await _write(_cache_redis, key, body, remaining)

            return _respond(_cache_request, body, kept, remaining, hit=hit)

        # FastAPI reads the handler's parameters from its signature, so the
        # wrapper declares the endpoint's own and asks for three more.
        wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=[
                *signature.parameters.values(),
                _keyword("_cache_request", Request),
                _keyword("_cache_redis", RedisDep),
                _keyword("_cache_settings", SettingsDep),
            ]
        )
        return wrapper

    return decorate


def _keyword(name: str, annotation: object) -> inspect.Parameter:
    return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)


def period_lifetime(period: date | None, *, today: date | None = None) -> Lifetime:
    """How long an answer about ``period`` is kept: a day once its filings are in.

    ``None`` is the latest published period, which is replaced by the next.
    """
    if period is None:
        return Lifetime.CURRENT_PERIOD
    today = today or datetime.now(UTC).date()
    closed = today > filing_deadline(period) + _CLOSING
    return Lifetime.CLOSED_PERIOD if closed else Lifetime.CURRENT_PERIOD


def cache_key(endpoint: str, arguments: dict[str, Any], settings: Settings) -> str:
    """``ww:v1:{endpoint}:{hash}``, for an endpoint's answer to ``arguments``."""
    canonical = json.dumps(
        {
            "release": [VERSION, settings.git_sha],
            "arguments": {
                name: _canonical(value)
                for name, value in arguments.items()
                if not isinstance(value, _NOT_ARGUMENTS)
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return f"{KEY_PREFIX}{endpoint}:{digest}"


def _canonical(value: object) -> object:
    """``value`` as JSON that is the same for every spelling of the same argument.

    :raises TypeError: A kind of argument this has not been taught. Better
        than a key that two different arguments could share.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, date):  # and datetime
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    raise TypeError(f"cannot key a cached response on a {type(value).__name__}")


async def _read(redis: Redis, key: str) -> tuple[str | None, int, bool]:
    """The cached body, the seconds it has left, and whether Redis answered."""
    try:
        # str, since the client decodes responses (app.core.redis); bytes to
        # the type checker.
        body: str | bytes | None = await redis.get(key)
        if body is None:
            return None, 0, True
        # -2 when the key expired between the two commands: serve what was
        # read, and tell the client not to keep it.
        remaining = max(await redis.ttl(key), 0)
    except RedisError as exc:
        logger.warning("cache.read_failed", key=key, exc_info=exc)
        return None, 0, False
    return body.decode() if isinstance(body, bytes) else body, remaining, True


async def _write(redis: Redis, key: str, body: str, seconds: int) -> None:
    try:
        await redis.set(key, body, ex=seconds)
    except RedisError as exc:
        logger.warning("cache.write_failed", key=key, exc_info=exc)


def _respond(
    request: Request, body: str, lifetime: Lifetime, remaining: int, *, hit: bool
) -> Response:
    content = body.encode()
    etag = f'"{hashlib.blake2b(content, digest_size=16).hexdigest()}"'
    cache_control = f"public, max-age={remaining}"
    if lifetime is Lifetime.CLOSED_PERIOD:
        cache_control += f", stale-while-revalidate={STALE_WHILE_REVALIDATE}"
    headers = {"Cache-Control": cache_control, "ETag": etag, "X-Cache": "HIT" if hit else "MISS"}

    if _none_match(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=content, media_type="application/json", headers=headers)


def _none_match(header: str | None, etag: str) -> bool:
    """Whether ``If-None-Match`` names ``etag``: by weak comparison, as RFC 9110 has it.

    So ``W/"abc"`` matches ``"abc"``, which is what a proxy that compressed the
    body hands back.
    """
    if header is None:
        return False
    if header.strip() == "*":
        return True
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


# --- invalidation -----------------------------------------------------------

#: Redis's glob characters, escaped so a prefix matches only itself.
_GLOB: Final = re.compile(r"([*?\[\]\\])")

#: Keys per SCAN round trip and per UNLINK.
_BATCH: Final = 1000


async def invalidate(redis: Redis, endpoint: str = "") -> int:
    """Delete every cached response of the endpoints whose names start with ``endpoint``.

    ``portfolio.`` is every handler in that router; ``portfolio.read_history``
    is that one's.
    Every one, of every release, with the default. Returns how many.

    :raises RedisError: Redis could not be reached. What a caller does about
        that is its own business: the API is unaffected, but whatever is
        cached stays until it expires.
    """
    pattern = _GLOB.sub(r"\\\1", f"{KEY_PREFIX}{endpoint}") + "*"
    deleted = 0
    batch: list[str] = []
    # SCAN rather than KEYS, which blocks every other client until it has
    # walked the whole keyspace. UNLINK frees the memory off the main thread.
    async for key in redis.scan_iter(match=pattern, count=_BATCH):
        batch.append(key)
        if len(batch) == _BATCH:
            deleted += await redis.unlink(*batch)
            batch.clear()
    if batch:
        deleted += await redis.unlink(*batch)
    return deleted
