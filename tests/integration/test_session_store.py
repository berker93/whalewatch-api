"""The session store, run by a real Redis.

The API tests use a fake, which answers the commands the store sends the way
the fake was written to; this is where those commands meet the real thing:
the pipeline, ``SET ... EXAT``, the per-user set, and what a key holds. One
container for the module; each test flushes it.
"""

import time
from collections.abc import AsyncIterator, Iterator

import pytest
from redis.asyncio import Redis
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.accounts.entitlements import Grant
from app.accounts.sessions import (
    REMEMBERED,
    SESSION_KEY_PREFIX,
    UNREMEMBERED,
    USER_SESSIONS_PREFIX,
    SessionStore,
    SessionStoreError,
    digest,
)
from tests.integration.test_rate_limit import REDIS_IMAGE


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


async def test_a_session_is_found_by_its_cookie_and_kept_by_its_hash(redis: Redis) -> None:
    store = SessionStore(redis)

    started = await store.start(7, remember=True)
    found = await store.find(started.token)

    assert found is not None
    assert (found.user_id, found.remember, found.id) == (7, True, digest(started.token))
    assert abs((found.expires_at - started.expires_at).total_seconds()) < 1
    keys = await redis.keys("*")
    assert sorted(keys) == sorted([SESSION_KEY_PREFIX + found.id, f"{USER_SESSIONS_PREFIX}7"])
    # The cookie itself is nowhere in Redis.
    for key in keys:
        assert started.token not in key
    assert started.token not in (await redis.get(SESSION_KEY_PREFIX + found.id) or "")


@pytest.mark.parametrize(("remember", "length"), [(True, REMEMBERED), (False, UNREMEMBERED)])
async def test_redis_drops_a_session_when_it_ends(
    redis: Redis, remember: bool, length: object
) -> None:
    store = SessionStore(redis)

    started = await store.start(7, remember=remember)

    ttl = await redis.ttl(SESSION_KEY_PREFIX + digest(started.token))
    assert length.total_seconds() - 5 <= ttl <= length.total_seconds()  # type: ignore[attr-defined]
    # The index outlives every session in it.
    assert await redis.ttl(f"{USER_SESSIONS_PREFIX}7") >= ttl


async def test_an_unknown_cookie_is_no_session(redis: Redis) -> None:
    assert await SessionStore(redis).find("not a cookie") is None


async def test_ending_a_session_is_immediate_and_leaves_the_others(redis: Redis) -> None:
    store = SessionStore(redis)
    first, second = await store.start(7, remember=True), await store.start(7, remember=False)

    await store.end(first.token)
    await store.end(first.token)  # twice is fine

    assert await store.find(first.token) is None
    assert await store.find(second.token) is not None
    members = await redis.smembers(f"{USER_SESSIONS_PREFIX}7")  # type: ignore[misc]
    assert members == {digest(second.token)}


async def test_signing_out_everywhere_keeps_only_the_one_asked(redis: Redis) -> None:
    store = SessionStore(redis)
    mine = await store.start(7, remember=True)
    others = [await store.start(7, remember=True) for _ in range(3)]
    someone_else = await store.start(8, remember=True)

    await store.end_all(7, keep=digest(mine.token))

    assert await store.find(mine.token) is not None
    assert [await store.find(o.token) for o in others] == [None, None, None]
    assert await store.find(someone_else.token) is not None

    await store.end_all(7, keep=None)
    await store.end_all(7, keep=None)  # nothing left is fine

    assert await store.find(mine.token) is None
    assert await redis.exists(f"{USER_SESSIONS_PREFIX}7") == 0


async def test_an_expired_session_is_gone(redis: Redis) -> None:
    store = SessionStore(redis)
    started = await store.start(7, remember=False)
    await redis.expireat(SESSION_KEY_PREFIX + digest(started.token), int(time.time()) - 1)

    assert await store.find(started.token) is None


async def test_a_redis_that_cannot_be_reached_is_said_so() -> None:
    store = SessionStore(Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2))

    with pytest.raises(SessionStoreError):
        await store.find("a cookie")
    with pytest.raises(SessionStoreError):
        await store.start(7, remember=True)


async def test_a_users_sessions_are_listed_soonest_to_end_first(redis: Redis) -> None:
    store = SessionStore(redis)
    remembered = await store.start(7, remember=True)
    await store.start(8, remember=True)
    short = await store.start(7, remember=False)
    gone = await store.start(7, remember=True)
    # Expired on its own: the key is gone, the index still names it.
    await redis.delete(SESSION_KEY_PREFIX + digest(gone.token))

    listed = await store.list_for(7)

    assert [(record.id, record.remember) for record in listed] == [
        (digest(short.token), False),
        (digest(remembered.token), True),
    ]


async def test_a_kept_grant_keeps_the_sessions_expiry_and_never_revives_one(redis: Redis) -> None:
    store = SessionStore(redis)
    started = await store.start(7, remember=False)
    record = await store.find(started.token)
    assert record is not None
    key = SESSION_KEY_PREFIX + record.id
    ttl = await redis.ttl(key)
    grant = Grant(plan="pro", grandfathered=None, overrides={"watchlist_items": 50}, ends_at=None)

    await store.keep_grant(record, grant)

    found = await store.find(started.token)
    assert found is not None and found.grant == grant and found.granted_at is not None
    assert 0 < await redis.ttl(key) <= ttl

    await store.forget_grants(7)
    forgotten = await store.find(started.token)
    assert forgotten is not None and (forgotten.grant, forgotten.granted_at) == (None, None)
    assert 0 < await redis.ttl(key) <= ttl

    # Ended meanwhile: the grant is not written back over the end.
    await store.end(started.token)
    await store.keep_grant(record, grant)
    assert await redis.exists(key) == 0
