"""A Redis that keeps keys in a dict, for tests of what is cached.

Only the commands :mod:`app.api.cache` sends. Time does not pass: a key's TTL
is what it was set with until a test changes it, so a test says exactly what
it means by "expiring".
"""

from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = False
        """Every command raises, as a Redis that cannot be reached does."""
        self.commands: list[str] = []

    def _send(self, command: str) -> None:
        self.commands.append(command)
        if self.down:
            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def get(self, key: str) -> str | None:
        self._send("get")
        return self.values.get(key)

    async def ttl(self, key: str) -> int:
        """-2 for a key with no TTL recorded, as Redis answers for one that has
        just expired: a test deletes a key's TTL to have it expire mid-request."""
        self._send("ttl")
        return self.ttls.get(key, -2)

    async def set(self, key: str, value: str, ex: int | None = None, **_: Any) -> None:
        self._send("set")
        self.values[key] = value
        self.ttls[key] = ex if ex is not None else -1

    def expire_all(self) -> None:
        self.values.clear()
        self.ttls.clear()
