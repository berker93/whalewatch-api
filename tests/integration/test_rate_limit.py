"""The sliding-window script, run by a real Redis.

The script is the whole of the limiter's logic, and Lua run by a fake would
test the fake. One container for the module; each test flushes it.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from redis.asyncio import Redis
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.api.rate_limit import KEY_PREFIX, SlidingWindowLimiter

# The major version compose runs.
REDIS_IMAGE = "redis:7-alpine"


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    container = (
        DockerContainer(REDIS_IMAGE)
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    )
    with container:
        host, port = container.get_container_host_ip(), container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


async def test_admits_the_limit_then_refuses(redis: Redis) -> None:
    limiter = SlidingWindowLimiter(redis, limit=60)

    admitted = [await limiter.hit("203.0.113.7") for _ in range(60)]
    refused = await limiter.hit("203.0.113.7")

    assert all(d.admitted for d in admitted)
    assert [d.remaining for d in admitted] == list(range(59, -1, -1))
    assert not refused.admitted
    # The oldest of the sixty leaves the window in a minute, less the time the
    # sixty took.
    assert 55 <= refused.retry_after <= 60


async def test_a_refused_request_is_not_counted(redis: Redis) -> None:
    """Or a client retrying through its 429s would hold itself locked out."""
    limiter = SlidingWindowLimiter(redis, limit=2)
    for _ in range(5):
        await limiter.hit("203.0.113.7")

    assert await redis.zcard(KEY_PREFIX + "203.0.113.7") == 2


async def test_the_window_slides(redis: Redis) -> None:
    limiter = SlidingWindowLimiter(redis, limit=2, window_seconds=1)
    await limiter.hit("203.0.113.7")
    await limiter.hit("203.0.113.7")

    assert not (await limiter.hit("203.0.113.7")).admitted
    await asyncio.sleep(1.1)
    assert (await limiter.hit("203.0.113.7")).admitted


async def test_concurrent_requests_cannot_overshoot(redis: Redis) -> None:
    """The script is atomic: fifty at once against a limit of ten admit ten."""
    limiter = SlidingWindowLimiter(redis, limit=10)

    decisions = await asyncio.gather(*(limiter.hit("203.0.113.7") for _ in range(50)))

    assert sum(d.admitted for d in decisions) == 10


async def test_addresses_have_their_own_budgets(redis: Redis) -> None:
    limiter = SlidingWindowLimiter(redis, limit=1)

    assert (await limiter.hit("203.0.113.7")).admitted
    assert (await limiter.hit("203.0.113.8")).admitted
    assert not (await limiter.hit("203.0.113.7")).admitted


async def test_one_ipv6_64_shares_a_budget(redis: Redis) -> None:
    limiter = SlidingWindowLimiter(redis, limit=1)

    assert (await limiter.hit("2001:db8:1:2::1")).admitted
    assert not (await limiter.hit("2001:db8:1:2::ffff")).admitted


async def test_an_idle_address_leaves_nothing_behind(redis: Redis) -> None:
    limiter = SlidingWindowLimiter(redis, limit=60)

    await limiter.hit("203.0.113.7")

    assert 0 < await redis.pttl(KEY_PREFIX + "203.0.113.7") <= 60_000
