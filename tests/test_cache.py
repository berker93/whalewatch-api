"""A response cached in Redis: built once, served until it expires, and served with Redis down."""

from typing import Any

from pydantic import BaseModel

from app.api.cache import cached_json
from app.core.version import VERSION
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis


class Answer(BaseModel):
    built: int


class Builder:
    """Counts its calls, so a test can tell a hit from a miss."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> Answer:
        self.calls += 1
        return Answer(built=self.calls)


async def _get(redis: FakeRedis, build: Builder, **settings: Any) -> tuple[str, str]:
    response = await cached_json(
        redis,  # type: ignore[arg-type]
        make_settings(**settings),
        "meta:periods",
        build,
        seconds=300,
    )
    return bytes(response.body).decode(), response.headers["cache-control"]


async def test_a_miss_builds_and_keeps_the_answer_for_its_lifetime() -> None:
    redis, build = FakeRedis(), Builder()

    body, cache_control = await _get(redis, build, git_sha="9f2c1a0")

    assert body == '{"built":1}'
    assert cache_control == "public, max-age=300"
    assert redis.values == {f"cache:{VERSION}:9f2c1a0:meta:periods": body}
    assert redis.ttls == {f"cache:{VERSION}:9f2c1a0:meta:periods": 300}


async def test_a_hit_is_served_as_cached_for_what_is_left_of_its_lifetime() -> None:
    """A browser keeping it for the full five minutes again would show an answer
    up to ten minutes old."""
    redis, build = FakeRedis(), Builder()
    await _get(redis, build)
    [key] = redis.ttls
    redis.ttls[key] = 120

    body, cache_control = await _get(redis, build)

    assert body == '{"built":1}'
    assert build.calls == 1
    assert cache_control == "public, max-age=120"


async def test_an_answer_expiring_as_it_is_read_is_not_kept_by_the_client() -> None:
    redis, build = FakeRedis(), Builder()
    await _get(redis, build)
    [key] = redis.ttls
    del redis.ttls[key]  # gone between GET and TTL, which then answers -2

    body, cache_control = await _get(redis, build)

    assert body == '{"built":1}'
    assert cache_control == "public, max-age=0"


async def test_another_release_does_not_read_this_ones_answer() -> None:
    redis, build = FakeRedis(), Builder()
    await _get(redis, build, git_sha="aaaaaaa")

    body, _ = await _get(redis, build, git_sha="bbbbbbb")

    assert body == '{"built":2}'
    assert len(redis.values) == 2


async def test_with_redis_down_every_request_is_answered_built() -> None:
    """Slower, not unavailable. And no write after a failed read, which would
    only wait out a second timeout."""
    redis, build = FakeRedis(), Builder()
    redis.down = True

    first, cache_control = await _get(redis, build)
    second, _ = await _get(redis, build)

    assert (first, second) == ('{"built":1}', '{"built":2}')
    assert cache_control == "public, max-age=300"
    assert redis.commands == ["get", "get"]
