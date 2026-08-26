"""Tests for the EDGAR client and the rate limiter it is built on.

The two rules SEC enforces are the two things these assert on hardest, because
both fail the same way — an hour into a backfill, on someone else's machine. So
the header test asserts on what actually goes out on the wire rather than on
what was passed to the constructor, and the pacing test measures the clock
rather than counting calls to a mock's ``sleep``.

Nothing here opens a socket. ``httpx.MockTransport`` sits where the network
would, which is also what makes it possible to assert on a 403 without
provoking a real ten-minute ban.
"""

import asyncio
import io
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import structlog

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket, get_edgar_limiter, reset_edgar_limiter
from app.ingestion.edgar.client import EdgarClient, EdgarRateLimited
from tests.conftest import make_settings

#: Enough of a real submissions document to be parsed as JSON.
_BODY = b'{"cik": "0000320193"}'


@pytest.fixture(autouse=True)
def _fresh_limiter() -> Iterator[None]:
    """Give every test its own process-global bucket.

    The singleton outlives a test function but the event loop does not, so a
    bucket left behind by one test would carry accrued tokens — and possibly a
    lock whose waiters belong to a closed loop — into the next one, making
    timing assertions depend on collection order.
    """
    reset_edgar_limiter()
    yield
    reset_edgar_limiter()


def _transport(
    *,
    status: int = 200,
    body: bytes = _BODY,
    headers: dict[str, str] | None = None,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body, headers=headers)

    return httpx.MockTransport(handler)


def _client(settings: Settings, **kwargs: Any) -> EdgarClient:
    """An EdgarClient whose requests never leave the process."""
    kwargs.setdefault("transport", _transport())
    return EdgarClient(settings, **kwargs)


# --- identification ---------------------------------------------------------


async def test_user_agent_goes_out_on_every_request(settings: Settings) -> None:
    """SEC 403s anything without a descriptive User-Agent, so this is asserted
    on the outbound request rather than on the constructor argument."""
    seen: list[httpx.Request] = []
    async with _client(settings, transport=_transport(seen=seen)) as edgar:
        await edgar.get_bytes("https://data.sec.gov/one")
        await edgar.get_bytes("https://data.sec.gov/two")

    assert [r.headers["user-agent"] for r in seen] == [settings.sec_user_agent] * 2
    assert "ops@whalewatch.io" in settings.sec_user_agent


async def test_requests_ask_for_compression(settings: Settings) -> None:
    seen: list[httpx.Request] = []
    async with _client(settings, transport=_transport(seen=seen)) as edgar:
        await edgar.get_bytes("https://data.sec.gov/one")

    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


# --- pacing -----------------------------------------------------------------


async def test_twenty_concurrent_requests_are_paced_to_the_configured_rate(
    settings: Settings,
) -> None:
    """The acceptance criterion: 20 requests at 8/s cannot finish sooner than
    2.5 seconds, however many tasks issue them at once.

    A bucket that permitted an initial burst would land near 1.5s here, which is
    the failure mode worth having a slow test for.
    """
    assert settings.sec_rate_limit_per_second == 8.0
    urls = [f"https://data.sec.gov/{i}" for i in range(20)]

    async with _client(settings) as edgar:
        started = time.perf_counter()
        bodies = await asyncio.gather(*(edgar.get_bytes(url) for url in urls))
        elapsed = time.perf_counter() - started

    assert bodies == [_BODY] * 20
    assert elapsed >= 20 / 8
    # Upper bound so a limiter that is accidentally serial — one request per
    # second, say — fails here rather than merely making the suite slow.
    assert elapsed < 20 / 8 + 1.0


async def test_limiter_is_shared_between_clients(settings: Settings) -> None:
    """SEC counts our IP, not our objects. Two clients in one process draw from
    one budget or the process runs at twice the configured rate."""
    first = _client(settings)
    second = _client(settings)

    assert first._limiter is second._limiter

    await first.aclose()
    await second.aclose()


def test_a_slower_rate_tightens_the_shared_bucket_instead_of_replacing_it() -> None:
    """Replacing it would orphan tasks already waiting on the old bucket, and
    they would then run alongside the new one's."""
    original = get_edgar_limiter(8.0)

    assert get_edgar_limiter(2.0) is original
    assert original.rate_per_second == 2.0
    # A faster request never loosens what someone else asked for.
    assert get_edgar_limiter(10.0) is original
    assert original.rate_per_second == 2.0


async def test_bucket_admits_waiters_in_arrival_order() -> None:
    """FIFO, so a long queue cannot starve the task that has waited longest."""
    bucket = AsyncTokenBucket(200.0)
    order: list[int] = []

    async def take(n: int) -> None:
        await bucket.acquire()
        order.append(n)

    await asyncio.gather(*(take(n) for n in range(10)))

    assert order == list(range(10))


# --- limiting and errors ----------------------------------------------------


async def test_403_raises_edgar_rate_limited_with_a_block_length_wait(
    settings: Settings,
) -> None:
    """A 403 on a request that carried a valid User-Agent means the IP is
    blocked, and that block runs about ten minutes."""
    async with _client(settings, transport=_transport(status=403)) as edgar:
        with pytest.raises(EdgarRateLimited) as caught:
            await edgar.get_bytes("https://data.sec.gov/blocked")

    assert caught.value.status_code == 403
    assert caught.value.url == "https://data.sec.gov/blocked"
    assert caught.value.retry_after == 600.0


async def test_429_raises_edgar_rate_limited(settings: Settings) -> None:
    async with _client(settings, transport=_transport(status=429)) as edgar:
        with pytest.raises(EdgarRateLimited) as caught:
            await edgar.get_bytes("https://data.sec.gov/throttled")

    assert caught.value.status_code == 429
    assert caught.value.retry_after == 60.0


async def test_retry_after_header_beats_our_guess(settings: Settings) -> None:
    transport = _transport(status=429, headers={"Retry-After": "15"})
    async with _client(settings, transport=transport) as edgar:
        with pytest.raises(EdgarRateLimited) as caught:
            await edgar.get_bytes("https://data.sec.gov/throttled")

    assert caught.value.retry_after == 15.0


async def test_unparseable_retry_after_falls_back_to_the_default(settings: Settings) -> None:
    """Failing to read a hint is not a reason to lose the 429 itself."""
    transport = _transport(status=429, headers={"Retry-After": "soon-ish"})
    async with _client(settings, transport=transport) as edgar:
        with pytest.raises(EdgarRateLimited) as caught:
            await edgar.get_bytes("https://data.sec.gov/throttled")

    assert caught.value.retry_after == 60.0


async def test_404_is_an_ordinary_http_error_not_a_rate_limit(settings: Settings) -> None:
    """A missing document should fail one filing; a rate limit should stop the
    run. Conflating them would abandon a backfill over a typo'd accession."""
    async with _client(settings, transport=_transport(status=404)) as edgar:
        with pytest.raises(httpx.HTTPStatusError):
            await edgar.get_bytes("https://data.sec.gov/missing")


async def test_get_json_parses_the_body(settings: Settings) -> None:
    async with _client(settings) as edgar:
        assert await edgar.get_json("https://data.sec.gov/submissions") == {"cik": "0000320193"}


async def test_closing_the_context_closes_the_pool(settings: Settings) -> None:
    edgar = _client(settings)
    async with edgar:
        await edgar.get_bytes("https://data.sec.gov/one")

    assert edgar._client.is_closed


# --- logging ----------------------------------------------------------------


@pytest.fixture
def log_stream(settings: Settings) -> Iterator[io.StringIO]:
    """Render logs as production JSON into a buffer, then restore.

    These assertions are about fields, so parsing JSON beats regexing a console
    line — the same reason ``tests/test_request_context.py`` does it this way.
    """
    stream = io.StringIO()
    configure_logging(make_settings(environment="production"), stream=stream)
    yield stream
    structlog.contextvars.clear_contextvars()
    configure_logging(settings)


def _events(stream: io.StringIO, name: str) -> list[dict[str, Any]]:
    parsed = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [event for event in parsed if event["event"] == name]


async def test_every_request_is_logged_with_url_status_duration_and_size(
    settings: Settings, log_stream: io.StringIO
) -> None:
    async with _client(settings) as edgar:
        await edgar.get_bytes("https://data.sec.gov/submissions/CIK0000320193.json")

    (event,) = _events(log_stream, "edgar.request")
    assert event["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert event["status"] == 200
    assert event["bytes"] == len(_BODY)
    assert event["duration_ms"] >= 0


async def test_a_limited_response_is_logged_before_it_raises(
    settings: Settings, log_stream: io.StringIO
) -> None:
    """The line that explains a dead backfill has to survive the exception."""
    async with _client(settings, transport=_transport(status=429)) as edgar:
        with pytest.raises(EdgarRateLimited):
            await edgar.get_bytes("https://data.sec.gov/throttled")

    assert _events(log_stream, "edgar.request")[0]["status"] == 429


async def test_a_transport_failure_is_logged_even_though_it_has_no_status(
    settings: Settings, log_stream: io.StringIO
) -> None:
    """A timeout produces no response, so the access line above never runs and
    the attempt would otherwise leave no trace at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route to sec.gov", request=request)

    async with _client(settings, transport=httpx.MockTransport(handler)) as edgar:
        with pytest.raises(httpx.ConnectTimeout):
            await edgar.get_bytes("https://data.sec.gov/one")

    (event,) = _events(log_stream, "edgar.request_failed")
    assert event["url"] == "https://data.sec.gov/one"
    assert event["error"] == "ConnectTimeout"


# --- URL construction -------------------------------------------------------


def test_submissions_url_zero_pads_the_cik() -> None:
    expected = "https://data.sec.gov/submissions/CIK0000320193.json"
    assert EdgarClient.submissions_url(320193) == expected
    # Callers hold the CIK as a padded string; both spellings must land here.
    assert EdgarClient.submissions_url("0000320193") == expected


def test_filing_index_url_unpads_the_cik_and_strips_the_dashes() -> None:
    """The inverse of submissions_url, which is exactly why neither is built by
    hand at a call site."""
    assert EdgarClient.filing_index_url("0000320193", "0001234567-24-000123") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000123456724000123/index.json"
    )
