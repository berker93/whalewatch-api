"""Token bucket rate limiter for EDGAR requests.

SEC publishes a 10 requests/second ceiling per IP and enforces it by blocking
the offending address for about ten minutes. A backfill fires thousands of
requests, so the ceiling cannot be something a call site remembers to respect:
it has to be a property of the only object that makes EDGAR requests, and the
budget it meters has to be shared by everything in the process, because the
thing SEC counts is our IP, not our objects.

Hence the module-level singleton at the bottom. Two ``EdgarClient`` instances —
one in the request path, one in a Celery task that happens to run in the same
worker — must draw from the same bucket, or the process quietly runs at twice
the configured rate and gets the IP banned mid-backfill.

Why start empty, and why a burst of one
---------------------------------------
The obvious token bucket holds a full second's worth of tokens, so an idle
client can fire ``rate`` requests instantly. That is exactly the behaviour to
avoid here. SEC's limit is a *sliding* window: a burst of 8 at the end of one
second plus a burst of 8 at the start of the next is 16 requests inside a 1.0s
window, which is over the ceiling even though the configured rate is under it.
So :class:`AsyncTokenBucket` defaults to a capacity of one token and starts
empty, which makes it a pacer — consecutive requests are spaced at least
``1/rate`` apart, and N requests take N/rate seconds with no burst at the front.
The cost is that a single occasional request also waits ``1/rate`` (125ms at the
default 8/s), which is nothing against a fetch that takes ten times that.

Not aiolimiter
--------------
``AsyncLimiter(8, 1)`` is one line and would do, but it allows the burst
described above and pulls in a dependency for forty lines of arithmetic. The
implementation below is small enough to reason about at the moment it matters,
which is when a backfill is being throttled and someone has to answer "is this
us or is this SEC?".
"""

import asyncio
import threading
from time import monotonic
from types import TracebackType
from typing import Self


class AsyncTokenBucket:
    """An asyncio-only token bucket, shared by every task that awaits it.

    One instance meters one budget. It is safe for any number of concurrent
    tasks on a single event loop: acquisitions are serialized by an
    ``asyncio.Lock``, which is FIFO, so waiters are admitted in arrival order
    rather than whichever one the loop happens to wake first.

    It is *not* safe to share across two event loops running at the same time
    (two threads each with their own loop), because the lock's waiters are
    futures belonging to one loop. Loops that run one after another — the usual
    ``asyncio.run`` per Celery task — are fine, since an uncontended lock
    creates no futures.
    """

    def __init__(self, rate_per_second: float, *, capacity: float = 1.0) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if capacity < 1.0:
            raise ValueError("capacity must be at least one token")
        self._rate = rate_per_second
        self._capacity = capacity
        self._tokens = 0.0
        # Lazily stamped on the first acquire rather than here. A bucket built
        # at import time and first used a minute later would otherwise have
        # accrued a minute of tokens against a limit nobody was using.
        self._updated_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def rate_per_second(self) -> float:
        return self._rate

    @rate_per_second.setter
    def rate_per_second(self, value: float) -> None:
        """Change the rate in place, including while tasks are waiting.

        Refill is computed incrementally from the elapsed time since the last
        acquire, so a waiter that wakes after this is set simply refills at the
        new rate from that point. Used by :func:`get_edgar_limiter` to tighten
        the shared budget without swapping the object out from under waiters.
        """
        if value <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = value

    async def acquire(self) -> None:
        """Wait until one token is available, then take it."""
        async with self._lock:
            while True:
                now = monotonic()
                # monotonic, not time(): a clock correction mid-backfill must
                # not hand out a windfall of tokens (or stall for hours).
                elapsed = now - (self._updated_at if self._updated_at is not None else now)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated_at = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Looped rather than slept once and returned: asyncio.sleep
                # guarantees a lower bound, not an exact wake time, and the rate
                # may have been lowered while we slept. Recomputing costs one
                # extra pass and removes both assumptions.
                await asyncio.sleep((1.0 - self._tokens) / self._rate)

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Nothing to release: the bucket meters how often requests *start*.

        A semaphore would be released here, and would be the wrong tool — it
        would cap requests in flight, which is not what SEC counts.
        """


#: Guards construction of the singleton. A ``threading.Lock`` and not an
#: ``asyncio.Lock``: the accessor is synchronous, called from ``__init__``, and
#: the only race worth closing is two threads building two buckets.
_CREATE_LOCK = threading.Lock()
_edgar_limiter: AsyncTokenBucket | None = None


def get_edgar_limiter(rate_per_second: float) -> AsyncTokenBucket:
    """Return the process-wide EDGAR limiter, building it on first call.

    If a later caller asks for a *slower* rate than the bucket already runs at,
    the bucket is slowed rather than replaced. Replacing it would orphan any
    task already waiting on the old one, and those tasks would then run
    alongside the new bucket's — briefly doubling the rate, which is precisely
    the failure this module exists to prevent. A faster request is ignored: the
    tightest ceiling anyone asked for is the only safe one to honour.
    """
    global _edgar_limiter
    with _CREATE_LOCK:
        if _edgar_limiter is None:
            _edgar_limiter = AsyncTokenBucket(rate_per_second)
        elif rate_per_second < _edgar_limiter.rate_per_second:
            _edgar_limiter.rate_per_second = rate_per_second
        return _edgar_limiter


def reset_edgar_limiter() -> None:
    """Drop the singleton so the next call builds a fresh one.

    For tests only. Each test function gets its own event loop, and a bucket
    that has already accrued time (or, worse, holds a lock whose waiters belong
    to a closed loop) makes timing assertions depend on execution order.
    """
    global _edgar_limiter
    with _CREATE_LOCK:
        _edgar_limiter = None
