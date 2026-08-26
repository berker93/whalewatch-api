"""Tests for the EDGAR client and the rate limiter it is built on.

The two rules SEC enforces are the two things these assert on hardest, because
both fail the same way — an hour into a backfill, on someone else's machine. So
the header test asserts on what actually goes out on the wire rather than on
what was passed to the constructor, and the pacing test measures the clock
rather than counting calls to a mock's ``sleep``.

Nothing here opens a socket. ``httpx.MockTransport`` sits where the network
would — and ``respx`` where the retry tests need to script a *sequence* of
replies to one URL — which is also what makes it possible to assert on a 403
without provoking a real ten-minute ban.

The retry tests assert on call counts, not on elapsed time. What matters about
this policy is which failures are repeated and how often, and a test that slept
the real backoff would take four minutes to prove it. The sleeps themselves are
neutralized by :func:`_instant_retries` and the wait *arithmetic* is asserted
separately, against the pure functions that compute it.
"""

import asyncio
import io
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
import structlog
from tenacity import RetryCallState, wait_none

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket, get_edgar_limiter, reset_edgar_limiter
from app.ingestion.edgar.client import (
    _BACKOFF,
    _BACKOFF_MAX_SECONDS,
    _EDGAR_RETRY,
    _MAX_ATTEMPTS,
    _RATE_LIMIT_ATTEMPTS,
    EdgarClient,
    EdgarRateLimited,
    EdgarServerError,
    _rate_limit_wait,
)
from tests.conftest import make_settings

#: Enough of a real submissions document to be parsed as JSON.
_BODY = b'{"cik": "0000320193"}'

#: Any absolute URL will do; respx matches on it exactly.
_URL = "https://data.sec.gov/submissions/CIK0000320193.json"


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


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry policy's decisions and drop only its sleeps.

    Five attempts at the real backoff is about two minutes, and a single 403 is
    ten; a suite that waited those out would be a suite nobody runs. Swapping the
    wait leaves ``retry``, ``stop`` and ``before_sleep`` — everything these tests
    are actually about — untouched. The durations are covered by the arithmetic
    tests at the bottom of this file.

    Patching the policy object rather than the wrapped method matters: tenacity
    copies the object on every call, so the copy that runs during the test reads
    this wait, and monkeypatch puts the real one back afterwards.
    """
    monkeypatch.setattr(_EDGAR_RETRY, "wait", wait_none())


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


def _respx_client(settings: Settings) -> EdgarClient:
    """A client with httpx's *real* transport, for respx to intercept.

    Deliberately not ``_client``: that one injects a ``MockTransport``, which
    respx never sees and which cannot script a different reply per attempt —
    exactly what the retry tests need.
    """
    return EdgarClient(settings)


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


# --- retrying ---------------------------------------------------------------
# The whole point of the policy is that these three groups behave differently,
# so each test asserts on the call count as well as on the outcome. A test that
# only checked "it eventually raised" would pass against a policy that retried
# 404s forever.


@respx.mock
async def test_a_503_is_retried_and_the_next_attempt_succeeds(settings: Settings) -> None:
    """The acceptance criterion: a transient 5xx costs an extra request, not a
    failed filing."""
    route = respx.get(_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        assert await edgar.get_bytes(_URL) == _BODY

    assert route.call_count == 2


@respx.mock
async def test_a_404_raises_immediately_after_a_single_call(settings: Settings) -> None:
    """The other acceptance criterion. A missing document is missing on the
    fifth attempt too, and a backfill spends that time once per absent file."""
    route = respx.get(_URL).mock(return_value=httpx.Response(404))

    async with _respx_client(settings) as edgar:
        with pytest.raises(httpx.HTTPStatusError):
            await edgar.get_bytes(_URL)

    assert route.call_count == 1


@respx.mock
async def test_a_400_is_not_retried_either(settings: Settings) -> None:
    """A malformed request is our bug. Repeating it just delays the traceback."""
    route = respx.get(_URL).mock(return_value=httpx.Response(400))

    async with _respx_client(settings) as edgar:
        with pytest.raises(httpx.HTTPStatusError):
            await edgar.get_bytes(_URL)

    assert route.call_count == 1


@respx.mock
async def test_a_500_is_not_retried(settings: Settings) -> None:
    """Only 502/503/504 are treated as "EDGAR is briefly unwell". A 500 is an
    unhandled error on their side and does not resolve itself in eight seconds;
    this test exists so that widening the set is a deliberate edit."""
    route = respx.get(_URL).mock(return_value=httpx.Response(500))

    async with _respx_client(settings) as edgar:
        with pytest.raises(httpx.HTTPStatusError):
            await edgar.get_bytes(_URL)

    assert route.call_count == 1


@respx.mock
async def test_a_dropped_connection_is_retried(settings: Settings) -> None:
    """EDGAR closes connections under load, which surfaces as a transport error
    with no response at all rather than as a status code."""
    route = respx.get(_URL).mock(
        side_effect=[httpx.ConnectError("connection reset"), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        assert await edgar.get_bytes(_URL) == _BODY

    assert route.call_count == 2


@respx.mock
async def test_a_read_timeout_is_retried(settings: Settings) -> None:
    route = respx.get(_URL).mock(
        side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        assert await edgar.get_bytes(_URL) == _BODY

    assert route.call_count == 2


@respx.mock
async def test_a_persistent_503_gives_up_after_five_attempts(settings: Settings) -> None:
    """Bounded, so a genuinely broken EDGAR fails the job instead of pinning a
    worker forever — and reraised as itself, not wrapped in a RetryError."""
    route = respx.get(_URL).mock(return_value=httpx.Response(503))

    async with _respx_client(settings) as edgar:
        with pytest.raises(EdgarServerError) as caught:
            await edgar.get_bytes(_URL)

    assert route.call_count == _MAX_ATTEMPTS == 5
    assert caught.value.status_code == 503


@respx.mock
async def test_a_403_is_retried_at_most_twice(settings: Settings) -> None:
    """A 403 means the IP is already blocked, so every extra request lands on a
    host that is counting them. Two retries, then the caller decides."""
    route = respx.get(_URL).mock(return_value=httpx.Response(403))

    async with _respx_client(settings) as edgar:
        with pytest.raises(EdgarRateLimited):
            await edgar.get_bytes(_URL)

    assert route.call_count == _RATE_LIMIT_ATTEMPTS == 3


@respx.mock
async def test_a_429_that_clears_is_retried_into_a_success(settings: Settings) -> None:
    route = respx.get(_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        assert await edgar.get_bytes(_URL) == _BODY

    assert route.call_count == 2


@respx.mock
async def test_get_json_is_retried_too(settings: Settings) -> None:
    """The policy lives on the shared request path, so it is not something
    ``get_json`` callers have to opt into separately."""
    route = respx.get(_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        assert await edgar.get_json(_URL) == {"cik": "0000320193"}

    assert route.call_count == 2


# --- retry waits ------------------------------------------------------------
# Asserted against the functions that compute them rather than by timing a
# sleep, so the numbers are checked exactly and the suite stays fast.


def test_a_rate_limit_never_retries_sooner_than_a_minute() -> None:
    """Even when SEC suggests otherwise. Its counter runs on a window longer
    than any one request, so obeying a short hint just spends an attempt."""
    assert _rate_limit_wait(1.0) == 60.0
    assert _rate_limit_wait(0.0) == 60.0
    # A longer suggestion is honoured as given.
    assert _rate_limit_wait(90.0) == 90.0


def test_a_rate_limit_wait_is_capped_at_the_length_of_a_block() -> None:
    """So a malformed Retry-After — an HTTP-date a week out — cannot park a
    worker for a week."""
    assert _rate_limit_wait(600.0) == 600.0
    assert _rate_limit_wait(86_400.0) == 600.0


def test_backoff_grows_and_is_capped() -> None:
    attempts = [_BACKOFF(_state(attempt)) for attempt in range(1, 12)]

    # Exponential: ~1, ~2, ~4, ~8 …, each within one second of jitter.
    assert 1.0 <= attempts[0] <= 2.0
    assert 2.0 <= attempts[1] <= 3.0
    assert 4.0 <= attempts[2] <= 5.0
    # …and never beyond the cap, however long the failure persists.
    assert all(wait <= _BACKOFF_MAX_SECONDS for wait in attempts)
    assert attempts[-1] == _BACKOFF_MAX_SECONDS


def test_backoff_is_jittered() -> None:
    """Without jitter, twenty requests that fail together sleep for the same
    interval and wake in the same millisecond — reconverging into the very burst
    that provoked the failure."""
    waits = {_BACKOFF(_state(attempt=3)) for _ in range(50)}

    assert len(waits) > 1


def _state(attempt: int) -> RetryCallState:
    """A retry state at ``attempt``, which is all wait_exponential_jitter reads."""
    state = RetryCallState(retry_object=_EDGAR_RETRY, fn=None, args=(), kwargs={})
    state.attempt_number = attempt
    return state


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
    the attempt would otherwise leave no trace at all.

    One line per *attempt*, not per call: which of the five failed and how is the
    question being asked when a backfill is slow rather than dead.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route to sec.gov", request=request)

    async with _client(settings, transport=httpx.MockTransport(handler)) as edgar:
        with pytest.raises(httpx.ConnectTimeout):
            await edgar.get_bytes("https://data.sec.gov/one")

    events = _events(log_stream, "edgar.request_failed")
    assert len(events) == _MAX_ATTEMPTS
    assert events[0]["url"] == "https://data.sec.gov/one"
    assert events[0]["error"] == "ConnectTimeout"


@respx.mock
async def test_every_retry_logs_its_attempt_number_and_reason(
    settings: Settings, log_stream: io.StringIO
) -> None:
    """The acceptance criterion, and the line that answers "why did this take
    four minutes?" — as fields, so the question can be asked of a whole run."""
    respx.get(_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(504), httpx.Response(200, content=_BODY)]
    )

    async with _respx_client(settings) as edgar:
        await edgar.get_bytes(_URL)

    first, second = _events(log_stream, "edgar.retry")
    assert first["attempt"] == 1
    assert first["url"] == _URL
    assert first["error"] == "EdgarServerError"
    assert first["status"] == 503
    # The retry before the *successful* attempt is logged too; the second
    # attempt is only known to have failed once the third one is being set up.
    assert second["attempt"] == 2
    assert second["status"] == 504


@respx.mock
async def test_a_successful_first_attempt_logs_no_retry(
    settings: Settings, log_stream: io.StringIO
) -> None:
    """The common case stays quiet, so an ``edgar.retry`` line in production
    always means something actually went wrong."""
    respx.get(_URL).mock(return_value=httpx.Response(200, content=_BODY))

    async with _respx_client(settings) as edgar:
        await edgar.get_bytes(_URL)

    assert _events(log_stream, "edgar.retry") == []


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
