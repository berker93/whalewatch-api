"""Responses cached in Redis: keyed on what was asked, kept by what they are about,
revalidated by ETag, dropped by prefix, and served with Redis down."""

import inspect
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from starlette.routing import BaseRoute

from app.api.cache import (
    KEY_PREFIX,
    STALE_WHILE_REVALIDATE,
    Lifetime,
    cache_key,
    cached,
    invalidate,
    period_lifetime,
)
from app.api.deps import get_app_settings, get_redis
from app.api.schemas.types import Period
from app.main import create_app
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

CLOSED = "2024Q1"
DAY = 24 * 60 * 60


class Answer(BaseModel):
    built: int
    slug: str
    period: date | None = None


class Builds:
    """Counts the handler's calls, so a test can tell a hit from a miss."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, slug: str, period: date | None = None) -> Answer:
        if slug == "missing":
            raise HTTPException(status_code=404)
        self.calls += 1
        return Answer(built=self.calls, slug=slug, period=period)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def builds() -> Builds:
    return Builds()


@pytest.fixture
def git_sha() -> str:
    return "9f2c1a0"


@pytest.fixture
def toy(redis: FakeRedis, builds: Builds, git_sha: str) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/things/{slug}", response_model=Answer)
    @cached(period="period")
    async def read_thing(slug: str, period: Annotated[Period | None, Query()] = None) -> Answer:
        return builds(slug, period)

    @app.get("/v1/search", response_model=Answer)
    @cached(Lifetime.SEARCH)
    async def search(q: str) -> Answer:
        return builds(q)

    @app.get("/v1/stable/{slug}", response_model=Answer)
    @cached(Lifetime.CURRENT_PERIOD)
    async def stable(slug: str) -> Answer:
        """The same answer however often it is built."""
        return Answer(built=0, slug=slug)

    @app.get("/v1/meta/thing", response_model=Answer)
    @cached(Lifetime.METADATA)
    async def meta() -> Answer:
        return builds("meta")

    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_app_settings] = lambda: make_settings(git_sha=git_sha)
    return app


@pytest.fixture
async def http(toy: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=toy), base_url="http://test") as client:
        yield client


# --- hits and misses ------------------------------------------------------------


async def test_the_second_of_two_identical_requests_is_a_hit(
    http: AsyncClient, builds: Builds
) -> None:
    first = await http.get("/v1/things/a", params={"period": CLOSED})
    second = await http.get("/v1/things/a", params={"period": CLOSED})

    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert second.content == first.content
    assert second.headers["etag"] == first.headers["etag"]
    assert builds.calls == 1


async def test_after_invalidation_the_same_request_is_a_miss(
    http: AsyncClient, redis: FakeRedis, builds: Builds
) -> None:
    await http.get("/v1/things/a", params={"period": CLOSED})

    assert await invalidate(redis) == 1  # type: ignore[arg-type]
    again = await http.get("/v1/things/a", params={"period": CLOSED})

    assert again.headers["x-cache"] == "MISS"
    assert again.json()["built"] == 2


async def test_the_key_is_the_handler_and_a_hash_of_its_arguments(
    http: AsyncClient, redis: FakeRedis, git_sha: str
) -> None:
    await http.get("/v1/things/a", params={"period": CLOSED})

    arguments = {"slug": "a", "period": date(2024, 3, 31)}
    expected = cache_key("test_cache.read_thing", arguments, make_settings(git_sha=git_sha))
    assert list(redis.values) == [expected]
    assert expected.startswith("ww:v1:test_cache.read_thing:")


@pytest.mark.parametrize(
    "spelling",
    [
        "/v1/things/a?period=2024-03-31",
        "/v1/things/a?period=2024q1",
        "/v1/things/a?utm_source=newsletter&period=2024Q1",
    ],
)
async def test_every_spelling_of_the_same_question_is_one_entry(
    http: AsyncClient, redis: FakeRedis, spelling: str
) -> None:
    """Keyed on the parsed arguments, not the query string, so neither a
    period's other spelling nor a parameter the endpoint ignores splits it."""
    await http.get(f"/v1/things/a?period={CLOSED}")

    response = await http.get(spelling)

    assert response.headers["x-cache"] == "HIT"
    assert len(redis.values) == 1


async def test_different_arguments_are_different_entries(
    http: AsyncClient, redis: FakeRedis
) -> None:
    await http.get("/v1/things/a", params={"period": CLOSED})
    await http.get("/v1/things/b", params={"period": CLOSED})
    await http.get("/v1/things/a", params={"period": "2023Q4"})
    await http.get("/v1/things/a")

    assert len(redis.values) == 4


async def test_another_release_does_not_read_this_ones_answer(
    toy: FastAPI, http: AsyncClient, redis: FakeRedis
) -> None:
    await http.get("/v1/meta/thing")
    toy.dependency_overrides[get_app_settings] = lambda: make_settings(git_sha="bbbbbbb")

    response = await http.get("/v1/meta/thing")

    assert response.headers["x-cache"] == "MISS"
    assert len(redis.values) == 2


async def test_an_error_is_not_cached(http: AsyncClient, redis: FakeRedis) -> None:
    response = await http.get("/v1/things/missing")

    assert response.status_code == 404
    assert redis.values == {}


async def test_an_invalid_argument_is_refused_before_the_cache_is_asked(
    http: AsyncClient, redis: FakeRedis
) -> None:
    response = await http.get("/v1/things/a", params={"period": "2024-03-30"})

    assert response.status_code == 422
    assert redis.commands == []


# --- lifetimes ------------------------------------------------------------------


async def test_a_closed_period_is_kept_for_a_day_and_may_be_served_stale(
    http: AsyncClient, redis: FakeRedis
) -> None:
    response = await http.get("/v1/things/a", params={"period": CLOSED})

    assert list(redis.ttls.values()) == [DAY]
    assert response.headers["cache-control"] == (
        f"public, max-age={DAY}, stale-while-revalidate={STALE_WHILE_REVALIDATE}"
    )


@pytest.mark.parametrize(
    ("path", "seconds"),
    [
        ("/v1/things/a", 300),  # the latest period, whichever it is
        ("/v1/things/a?period=2099Q4", 300),  # not closed yet
        ("/v1/search?q=berk", 60),
        ("/v1/meta/thing", 300),
    ],
)
async def test_everything_else_is_kept_for_its_lifetime(
    http: AsyncClient, redis: FakeRedis, path: str, seconds: int
) -> None:
    response = await http.get(path)

    assert list(redis.ttls.values()) == [seconds]
    assert response.headers["cache-control"] == f"public, max-age={seconds}"


@pytest.mark.parametrize(
    ("today", "lifetime"),
    [
        # 2024Q1's deadline is 15 May, which is not rolled to a business day:
        # a few days' grace before the quarter is closed.
        (date(2024, 5, 15), Lifetime.CURRENT_PERIOD),
        (date(2024, 5, 19), Lifetime.CURRENT_PERIOD),
        (date(2024, 5, 20), Lifetime.CLOSED_PERIOD),
    ],
)
def test_a_period_closes_a_few_days_after_its_filing_deadline(
    today: date, lifetime: Lifetime
) -> None:
    assert period_lifetime(date(2024, 3, 31), today=today) is lifetime


def test_no_period_is_the_current_one() -> None:
    assert period_lifetime(None) is Lifetime.CURRENT_PERIOD


async def test_a_hit_is_served_for_what_is_left_of_its_lifetime(
    http: AsyncClient, redis: FakeRedis
) -> None:
    """A browser keeping it for the full five minutes again would show an answer
    up to ten minutes old."""
    await http.get("/v1/meta/thing")
    [key] = redis.ttls
    redis.ttls[key] = 120

    response = await http.get("/v1/meta/thing")

    assert response.headers["cache-control"] == "public, max-age=120"


async def test_an_answer_expiring_as_it_is_read_is_not_kept_by_the_client(
    http: AsyncClient, redis: FakeRedis
) -> None:
    await http.get("/v1/meta/thing")
    [key] = redis.ttls
    del redis.ttls[key]  # gone between GET and TTL, which then answers -2

    response = await http.get("/v1/meta/thing")

    assert response.json()["built"] == 1
    assert response.headers["cache-control"] == "public, max-age=0"


# --- ETags ----------------------------------------------------------------------


async def test_a_matching_if_none_match_is_a_304_with_no_body(http: AsyncClient) -> None:
    first = await http.get("/v1/things/a", params={"period": CLOSED})

    revalidated = await http.get(
        "/v1/things/a",
        params={"period": CLOSED},
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == first.headers["etag"]
    assert revalidated.headers["cache-control"] == first.headers["cache-control"]
    assert revalidated.headers["x-cache"] == "HIT"


@pytest.mark.parametrize(
    "header",
    ['"stale", {etag}', "W/{etag}", "*"],
)
async def test_if_none_match_compares_weakly_and_takes_a_list(
    http: AsyncClient, header: str
) -> None:
    etag = (await http.get("/v1/meta/thing")).headers["etag"]

    response = await http.get("/v1/meta/thing", headers={"If-None-Match": header.format(etag=etag)})

    assert response.status_code == 304


async def test_a_changed_answer_has_a_new_etag_and_is_sent_whole(
    http: AsyncClient, redis: FakeRedis
) -> None:
    old = (await http.get("/v1/meta/thing")).headers["etag"]
    redis.expire_all()

    response = await http.get("/v1/meta/thing", headers={"If-None-Match": old})

    assert response.status_code == 200
    assert response.json()["built"] == 2
    assert response.headers["etag"] != old


async def test_the_etag_is_the_bodys_so_a_rebuild_can_answer_304_to_it(
    toy: FastAPI, http: AsyncClient, redis: FakeRedis
) -> None:
    """Whether cached or built, by any process, and with Redis down."""
    built = await http.get("/v1/stable/a")
    cached = await http.get("/v1/stable/a")
    redis.down = True

    rebuilt = await http.get("/v1/stable/a", headers={"If-None-Match": built.headers["etag"]})

    assert (built.headers["x-cache"], cached.headers["x-cache"]) == ("MISS", "HIT")
    assert cached.headers["etag"] == built.headers["etag"]
    assert rebuilt.status_code == 304
    assert rebuilt.headers["x-cache"] == "MISS"


# --- Redis down -----------------------------------------------------------------


async def test_with_redis_down_every_request_is_built_and_none_fails(
    http: AsyncClient, redis: FakeRedis, builds: Builds
) -> None:
    """Slower, not unavailable. And no write after a failed read, which would
    only wait out a second timeout."""
    redis.down = True

    first = await http.get("/v1/things/a", params={"period": CLOSED})
    second = await http.get("/v1/things/a", params={"period": CLOSED})

    assert (first.status_code, second.status_code) == (200, 200)
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "MISS")
    assert builds.calls == 2
    assert redis.commands == ["get", "get"]


async def test_a_failed_write_still_answers(http: AsyncClient, redis: FakeRedis) -> None:
    async def refuse(*_: Any, **__: Any) -> None:
        redis.down = True
        redis._send("set")

    redis.set = refuse  # type: ignore[method-assign]

    response = await http.get("/v1/meta/thing")

    assert response.status_code == 200
    assert response.headers["x-cache"] == "MISS"


# --- invalidation ---------------------------------------------------------------


async def test_invalidation_by_prefix_drops_only_that_endpoints_answers(
    http: AsyncClient, redis: FakeRedis
) -> None:
    await http.get("/v1/things/a", params={"period": CLOSED})
    await http.get("/v1/things/b")
    await http.get("/v1/meta/thing")
    redis.values["unrelated"] = "kept"

    dropped = await invalidate(redis, "test_cache.read_thing")  # type: ignore[arg-type]

    assert dropped == 2
    assert [key for key in redis.values if key.startswith(KEY_PREFIX)] == [
        key for key in redis.values if key.startswith(f"{KEY_PREFIX}test_cache.meta:")
    ]
    assert redis.values["unrelated"] == "kept"


async def test_invalidating_everything_leaves_only_what_is_not_the_caches(
    http: AsyncClient, redis: FakeRedis
) -> None:
    await http.get("/v1/things/a")
    await http.get("/v1/search", params={"q": "x"})
    redis.values["unrelated"] = "kept"

    assert await invalidate(redis) == 2  # type: ignore[arg-type]
    assert list(redis.values) == ["unrelated"]


async def test_a_prefix_is_matched_literally(redis: FakeRedis) -> None:
    redis.values[f"{KEY_PREFIX}a*b:1"] = "x"
    redis.values[f"{KEY_PREFIX}aXb:1"] = "y"

    assert await invalidate(redis, "a*b") == 1  # type: ignore[arg-type]
    assert list(redis.values) == [f"{KEY_PREFIX}aXb:1"]


# --- misuse ---------------------------------------------------------------------


def test_a_lifetime_or_a_period_parameter_not_both_or_neither() -> None:
    with pytest.raises(TypeError):
        cached()
    with pytest.raises(TypeError):
        cached(Lifetime.SEARCH, period="period")


def test_the_period_parameter_must_be_the_handlers() -> None:
    async def handler(slug: str) -> Answer:
        raise AssertionError

    with pytest.raises(TypeError, match="no parameter 'period'"):
        cached(period="period")(handler)


def _routes(routes: Sequence[BaseRoute], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every route with its whole path. FastAPI 0.141 nests an included router
    rather than copying its routes into the app's, with the prefix beside it."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif (included := getattr(route, "original_router", None)) is not None:
            context: Any = route.include_context  # type: ignore[attr-defined]
            yield from _routes(included.routes, prefix + context.prefix)


def test_every_public_read_is_cached() -> None:
    """A new /v1 endpoint is cached unless it is added here with the reason."""
    uncached: set[str] = set()
    reads = {
        path: route
        for path, route in _routes(create_app(make_settings()).routes)
        if path.startswith("/v1/") and "GET" in (route.methods or set())
    }

    assert "/v1/investors/{slug}/portfolio" in reads
    assert {
        path
        for path, route in reads.items()
        if "_cache_redis" not in inspect.signature(route.endpoint).parameters
    } == uncached
