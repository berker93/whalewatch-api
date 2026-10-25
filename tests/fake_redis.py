"""A Redis that keeps keys in a dict, for tests of what is cached and who is signed in.

Only the commands :mod:`app.api.cache` and :class:`app.accounts.sessions.SessionStore`
send. Time does not pass: a key's TTL is what it was set with until a test
changes it, so a test says exactly what it means by "expiring". The store's
own test runs against a real Redis (tests/integration/test_session_store.py).
"""

import builtins
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Self

from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    def __init__(self, *, down: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        self.down = down
        """Every command raises, as a Redis that cannot be reached does."""
        self.commands: list[str] = []

    def _send(self, command: str) -> None:
        self.commands.append(command)
        if self.down:
            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def get(self, key: str) -> str | None:
        self._send("get")
        return self.values.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self._send("mget")
        return [self.values.get(key) for key in keys]

    async def ttl(self, key: str) -> int:
        """-2 for a key with no TTL recorded, as Redis answers for one that has
        just expired: a test deletes a key's TTL to have it expire mid-request."""
        self._send("ttl")
        return self.ttls.get(key, -2)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        exat: int | None = None,
        *,
        xx: bool = False,
        keepttl: bool = False,
        **_: Any,
    ) -> bool | None:
        self._send("set")
        if xx and key not in self.values:
            return None
        self.values[key] = value
        if keepttl and key in self.ttls:
            return True
        if exat is not None:
            ex = exat - int(time.time())
        self.ttls[key] = ex if ex is not None else -1
        return True

    async def delete(self, *keys: str) -> int:
        self._send("delete")
        present = [key for key in keys if key in self.values or key in self.sets]
        for key in present:
            self.values.pop(key, None)
            self.sets.pop(key, None)
            self.ttls.pop(key, None)
        return len(present)

    async def sadd(self, key: str, *members: str) -> int:
        self._send("sadd")
        members_now = self.sets.setdefault(key, set())
        added = set(members) - members_now
        members_now.update(members)
        return len(added)

    async def srem(self, key: str, *members: str) -> int:
        self._send("srem")
        members_now = self.sets.get(key, set())
        removed = members_now & set(members)
        members_now -= removed
        if not members_now:
            self.sets.pop(key, None)
        return len(removed)

    # builtins.set: in the class body, `set` is the method above.
    async def smembers(self, key: str) -> builtins.set[str]:
        self._send("smembers")
        return set(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> bool:
        self._send("expire")
        if key not in self.values and key not in self.sets:
            return False
        self.ttls[key] = seconds
        return True

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)

    async def scan_iter(self, match: str, count: int | None = None) -> AsyncIterator[str]:
        """``*`` and ``?`` as globs, a backslash escaping the next character,
        and nothing else: the patterns :func:`app.api.cache.invalidate` sends."""
        self._send("scan")
        parts = re.findall(r"\\.|\*|\?|[^\\*?]+", match)
        globs = {"*": ".*", "?": "."}
        pattern = "".join(globs.get(part) or re.escape(part.removeprefix("\\")) for part in parts)
        for key in [key for key in self.values if re.fullmatch(pattern, key)]:
            yield key

    async def unlink(self, *keys: str) -> int:
        self._send("unlink")
        present = [key for key in keys if key in self.values]
        for key in present:
            del self.values[key]
            self.ttls.pop(key, None)
        return len(present)

    async def aclose(self) -> None:
        pass

    def expire_all(self) -> None:
        self.values.clear()
        self.ttls.clear()


class FakePipeline:
    """Queues commands and runs them, in order, on :meth:`execute`. Atomic, as
    a MULTI/EXEC is, because nothing else runs between them in a test."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self._queued.clear()

    def __getattr__(self, name: str) -> Any:
        def queue(*args: Any, **kwargs: Any) -> "FakePipeline":
            self._queued.append((name, args, kwargs))
            return self

        return queue

    async def execute(self) -> list[Any]:
        results = [
            await getattr(self._redis, name)(*args, **kwargs) for name, args, kwargs in self._queued
        ]
        self._queued.clear()
        return results
