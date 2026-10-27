"""Per-IP rate limiting over a sliding window, counted in Redis.

``rate_limit_per_minute`` requests (60 by default) from one address in any 60
seconds; the next is answered 429 with ``Retry-After``, the seconds until the
oldest of those leaves the window. Kept in Redis rather than in the process, so
that two API processes, or ten, count against one budget.

Why a sliding window
--------------------
A fixed window (``INCR`` a key per minute) lets a client send 60 requests at
0:59 and 60 more at 1:00: 120 inside two seconds, under a limit of 60 a minute.
Here each address has a sorted set of the times of its admitted requests in the
last minute, scored in milliseconds. A request trims what is older than the
window, counts what is left, and is admitted, and added, only if that is under
the limit. A refused request is not added: a client that keeps retrying through
a 429 gets back in as soon as its oldest request ages out, rather than holding
itself locked out with its own retries.

All of that is one Lua script, which Redis runs atomically, so two requests
from one address on two processes cannot both read 59 and both be admitted.
The time is Redis's own (``TIME``), not the caller's, so API hosts with clocks
apart do not disagree about what is in the window.

Which address
-------------
The connection's peer, ``scope["client"]``. Behind a load balancer that is the
balancer, and every client would share one budget; uvicorn's
``--proxy-headers --forwarded-allow-ips=<the balancer>`` makes it the client's,
from ``X-Forwarded-For``, trusting the header only from the balancer. This
module never reads that header itself: from anyone else it is whatever the
client wants it to be, and a limit keyed on it is no limit.

IPv6 addresses are counted by their /64. An ISP hands a subscriber a whole
/64, so counting each address would give one client 2^64 budgets.

A guard, not a dependency
-------------------------
When Redis cannot be reached, requests are let through and the failure logged,
as the response cache does: an outage there takes the limit away for a while
rather than taking the API down with it.
"""

import ipaddress
import uuid
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from redis.exceptions import RedisError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import problem
from app.api.schemas.error import ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Outside ``ww:v1:``, which cache invalidation deletes wholesale.
KEY_PREFIX: Final = "ww:rl:"
#: The stricter limit on password changes and emailed tokens (app.api.auth).
AUTH_KEY_PREFIX: Final = "ww:rl:auth:"
#: Logins, registrations and resets, per IP and per email (app.api.auth.limit_attempts).
ATTEMPTS_KEY_PREFIX: Final = "ww:rl:attempts:"

#: The load balancer's and the orchestrator's probes, which come from a few
#: addresses many times a minute and would spend those addresses' budgets;
#: Paddle's and Postmark's webhooks (app/api/routers/billing.py, email.py),
#: which come from a few addresses in bursts, are authenticated, and are
#: retried if refused; and the one-click unsubscribe, which mailbox providers
#: post from a few of their own servers for all their users at once, after a
#: digest goes out. Its token's signature is checked before any work is done.
EXEMPT_PATHS: Final = frozenset(
    {"/health", "/ready", "/webhooks/paddle", "/webhooks/postmark", "/v1/email/unsubscribe"}
)

# KEYS[1]: the address's sorted set. ARGV: window ms, limit, a unique member.
# Returns {admitted 0|1, remaining, ms until the next request would be admitted}.
_SLIDING_WINDOW: Final = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, limit - count - 1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, 0, tonumber(oldest[2]) + window - now}
"""


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether one request is admitted, and what to tell the client either way."""

    admitted: bool
    remaining: int
    """Requests this address has left in the window, after this one."""
    retry_after: int
    """Whole seconds until a refused request would be admitted. 0 if admitted."""


class SlidingWindowLimiter:
    """``limit`` requests per ``window_seconds`` per address."""

    def __init__(
        self, redis: Redis, limit: int, window_seconds: int = 60, *, prefix: str = KEY_PREFIX
    ) -> None:
        """:param prefix: Where this limiter's counts are kept, so that two
        limiters on one Redis (the global one and the stricter one for
        sign-ins) count the same address separately."""
        self.limit = limit
        self.window_ms = window_seconds * 1000
        self.prefix = prefix
        self._redis = redis
        self._script: AsyncScript | None = None

    async def hit(self, client: str) -> Decision:
        """Count one request from ``client``, if there is room for it.

        :raises RedisError: when Redis cannot be reached. The caller decides
            what that means; see :class:`RateLimitMiddleware`.
        """
        # A fresh member per request, never the request id: a client chooses
        # that, and two requests sharing a member would count once.
        if self._script is None:
            # Here rather than in __init__, which runs in the lifespan, so that
            # building the app asks nothing of the client but that it exists.
            # Registering does no I/O: it hashes the script, and the first call
            # sends it whole if Redis has not seen it.
            self._script = self._redis.register_script(_SLIDING_WINDOW)
        admitted, remaining, wait_ms = await self._script(
            keys=[self.prefix + bucket(client)],
            args=[self.window_ms, self.limit, uuid.uuid4().hex],
        )
        # Ceiling, and never 0: "retry after 0 seconds" invites the retry that
        # is refused again.
        retry_after = 0 if admitted else max(1, -(-int(wait_ms) // 1000))
        return Decision(bool(admitted), int(remaining), retry_after)


def bucket(client: str) -> str:
    """What ``client`` is counted as: its IPv4 address, or its IPv6 /64."""
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        # Not an address, as in a test client's "testclient": itself.
        return client
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


class RateLimitMiddleware:
    """Refuse a request over its address's budget, 429, before any route runs.

    The limiter is the one the lifespan puts on ``app.state.rate_limiter``,
    built on the app's Redis. An app with none, as in a test that does not run
    the lifespan, limits nothing.

    Every counted response says ``X-RateLimit-Limit`` and
    ``X-RateLimit-Remaining``, so a well-behaved client can slow down before it
    is refused; but for a request made with an API key, whose answer says its
    monthly quota under those names (:mod:`app.api.metering`).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limiter: SlidingWindowLimiter | None = (
            getattr(scope["app"].state, "rate_limiter", None) if "app" in scope else None
        )
        client = scope.get("client")
        if (
            scope["type"] != "http"
            or limiter is None
            or client is None
            or scope["path"] in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        try:
            decision = await limiter.hit(client[0])
        except (RedisError, OSError) as exc:
            logger.warning("rate_limit.unavailable", error=repr(exc))
            await self.app(scope, receive, send)
            return

        limit_headers = {
            "x-ratelimit-limit": str(limiter.limit),
            "x-ratelimit-remaining": str(decision.remaining),
        }
        if not decision.admitted:
            logger.info("rate_limit.refused", retry_after_s=decision.retry_after)
            response = problem(
                429,
                ErrorCode.RATE_LIMITED,
                f"Over {limiter.limit} requests a minute from this address. "
                f"Try again in {decision.retry_after} seconds.",
                scope=scope,
                headers={**limit_headers, "retry-after": str(decision.retry_after)},
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # A request made with an API key says its month's quota under
                # these names (app.api.metering), which is what its client
                # budgets by; the minute's are for everyone else.
                if "x-ratelimit-limit" not in headers:
                    for name, value in limit_headers.items():
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
