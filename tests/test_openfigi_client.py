"""Tests for the OpenFIGI client.

OpenFIGI is never called from here. Every client is built on an
``httpx.MockTransport`` playing OpenFIGI's part, and :func:`_no_network` makes
httpx's real transport raise for the length of every test in this module — so a
client built without a mock fails loudly instead of spending quota from CI.

The response log is held in memory (:class:`MemoryLog`). That it behaves the
same on Postgres is tests/integration/test_figi_response_log.py's job.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from tenacity import wait_none

from app.core.config import Settings
from app.core.rate_limit import get_openfigi_limiter, reset_limiters
from app.db.models.figi_response_log import FIGIOutcome
from app.ingestion.enrichment.figi import (
    _MAX_ATTEMPTS,
    _MAX_RATE_LIMIT_WAIT_SECONDS,
    _MIN_RATE_LIMIT_WAIT_SECONDS,
    _OPENFIGI_RETRY,
    _QUOTA_HEADROOM,
    API_KEY_HEADER,
    KEYED_QUOTA,
    OPENFIGI_MAPPING_URL,
    UNKEYED_QUOTA,
    FIGIMatch,
    FIGIResult,
    OpenFIGIClient,
    OpenFIGIRateLimited,
    OpenFIGIResponseError,
    _reset_seconds,
    classify,
)
from tests.conftest import make_settings

APPLE = "037833100"
BERKSHIRE_B = "084670702"
NOWHERE = "000000000"

_APPLE_US = {
    "figi": "BBG000B9XRY4",
    "name": "APPLE INC",
    "ticker": "AAPL",
    "exchCode": "US",
    "compositeFIGI": "BBG000B9XRY4",
    "securityType": "Common Stock",
    "marketSector": "Equity",
    "shareClassFIGI": "BBG001S5N8V8",
    "securityType2": "Common Stock",
    "securityDescription": "AAPL",
}
_APPLE_UW = {**_APPLE_US, "figi": "BBG000B9Y5X2", "exchCode": "UW"}
_NOT_FOUND = {"warning": "No identifier found."}


class MemoryLog:
    """:class:`~app.ingestion.enrichment.figi.ResponseLog`, in a list."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any], datetime]] = []

    async def latest(self, cusips: Sequence[str]) -> dict[str, FIGIResult]:
        wanted = set(cusips)
        found: dict[str, FIGIResult] = {}
        for cusip, response, at in self.rows:  # oldest first, so the newest wins
            if cusip in wanted:
                found[cusip] = FIGIResult.from_response(cusip, response, at)
        return found

    async def record(self, responses: Sequence[tuple[str, dict[str, Any]]]) -> datetime:
        at = datetime.now(UTC)
        self.rows.extend((cusip, response, at) for cusip, response in responses)
        return at


Answer = Callable[[str], dict[str, Any]]


def _answer(known: dict[str, dict[str, Any]]) -> Answer:
    """Answer each job from ``known``, and anything else as not found."""
    return lambda cusip: known.get(cusip, _NOT_FOUND)


class FakeOpenFIGI:
    """OpenFIGI's mapping endpoint, as a transport. Records every request.

    ``statuses`` scripts the status of successive requests (then 200 forever),
    for the retry tests.
    """

    def __init__(self, answer: Answer, *, statuses: Sequence[int] = ()) -> None:
        self.answer = answer
        self.statuses = list(statuses)
        self.requests: list[httpx.Request] = []

    @property
    def batches(self) -> list[list[dict[str, str]]]:
        return [json.loads(request.content) for request in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return httpx.Response(status, headers={"ratelimit-reset": "0"})
        jobs = json.loads(request.content)
        return httpx.Response(200, json=[self.answer(job["idValue"]) for job in jobs])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class CountingLimiter:
    """Stands in for the token bucket; counts acquisitions without waiting."""

    def __init__(self) -> None:
        self.acquired = 0

    async def __aenter__(self) -> "CountingLimiter":
        self.acquired += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any request that is not mocked an error rather than a real call."""

    async def refuse(self: object, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"tests must not call OpenFIGI: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture(autouse=True)
def _fresh_limiter() -> Iterator[None]:
    reset_limiters()
    yield
    reset_limiters()


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry decisions, drop the sleeps (as in test_edgar_client)."""
    monkeypatch.setattr(_OPENFIGI_RETRY, "wait", wait_none())


@pytest.fixture
def keyed() -> Settings:
    return make_settings(openfigi_api_key="test-key")


def _client(
    settings: Settings,
    fake: FakeOpenFIGI,
    log: MemoryLog | None = None,
    limiter: CountingLimiter | None = None,
) -> OpenFIGIClient:
    bucket: Any = limiter if limiter is not None else CountingLimiter()
    return OpenFIGIClient(
        settings,
        log if log is not None else MemoryLog(),
        limiter=bucket,
        transport=fake.transport,
    )


def _cusips(n: int) -> list[str]:
    return [f"{i:09d}" for i in range(1, n + 1)]


# --- the request ----------------------------------------------------------------


async def test_the_payload_is_one_cusip_job_per_cusip(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        await figi.map_cusips([APPLE, BERKSHIRE_B])

    (request,) = fake.requests
    assert request.method == "POST"
    assert str(request.url) == OPENFIGI_MAPPING_URL
    assert request.headers["content-type"] == "application/json"
    assert fake.batches == [
        [{"idType": "ID_CUSIP", "idValue": APPLE}, {"idType": "ID_CUSIP", "idValue": BERKSHIRE_B}]
    ]


async def test_the_api_key_is_sent_when_configured(keyed: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(keyed, fake) as figi:
        await figi.map_cusips([APPLE])

    assert fake.requests[0].headers[API_KEY_HEADER] == "test-key"


async def test_no_key_header_without_a_key(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        await figi.map_cusips([APPLE])

    assert API_KEY_HEADER.lower() not in {name.lower() for name in fake.requests[0].headers}


def test_a_blank_key_is_no_key() -> None:
    """A copied .env's ``OPENFIGI_API_KEY=`` must not be sent as an empty key."""
    assert make_settings(openfigi_api_key="").openfigi_api_key is None
    assert make_settings(openfigi_api_key="  ").openfigi_api_key is None


# --- batching -------------------------------------------------------------------


async def test_unkeyed_requests_are_batched_to_the_unkeyed_job_cap(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        results = await figi.map_cusips(_cusips(25))

    assert [len(batch) for batch in fake.batches] == [10, 10, 5]
    assert len(results) == 25


async def test_keyed_requests_are_batched_to_the_keyed_job_cap(keyed: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(keyed, fake) as figi:
        await figi.map_cusips(_cusips(250))

    assert [len(batch) for batch in fake.batches] == [100, 100, 50]


async def test_a_cusip_asked_twice_is_requested_once(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        results = await figi.map_cusips([APPLE, BERKSHIRE_B, APPLE])

    assert [job["idValue"] for job in fake.batches[0]] == [APPLE, BERKSHIRE_B]
    assert list(results) == [APPLE, BERKSHIRE_B]


async def test_nothing_asked_is_nothing_requested(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        assert await figi.map_cusips([]) == {}

    assert fake.requests == []


async def test_a_malformed_cusip_fails_before_any_request(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        with pytest.raises(ValueError, match="ABC"):
            await figi.map_cusips([APPLE, "ABC"])

    assert fake.requests == []


# --- rate limiting --------------------------------------------------------------


async def test_every_request_draws_a_token(settings: Settings) -> None:
    limiter = CountingLimiter()
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake, limiter=limiter) as figi:
        await figi.map_cusips(_cusips(25))

    assert limiter.acquired == len(fake.requests) == 3


@pytest.mark.parametrize(
    ("overrides", "quota"),
    [({}, UNKEYED_QUOTA), ({"openfigi_api_key": "test-key"}, KEYED_QUOTA)],
)
def test_the_default_limiter_paces_under_the_quota(overrides: dict[str, Any], quota: Any) -> None:
    client = OpenFIGIClient(make_settings(**overrides), MemoryLog())

    assert client.quota is quota
    assert get_openfigi_limiter(1000.0).rate_per_second == pytest.approx(
        quota.requests_per_second * _QUOTA_HEADROOM
    )


def test_the_published_quotas() -> None:
    """Pinned so a change to them is a deliberate edit, made with the module
    docstring's table, rather than a drive-by."""
    assert (UNKEYED_QUOTA.requests, UNKEYED_QUOTA.window_seconds) == (25, 60.0)
    assert UNKEYED_QUOTA.jobs_per_request == 10
    assert (KEYED_QUOTA.requests, KEYED_QUOTA.window_seconds) == (25, 6.0)
    assert KEYED_QUOTA.jobs_per_request == 100


# --- what comes back ------------------------------------------------------------


async def test_a_match_carries_ticker_figi_name_exchange_and_type(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({APPLE: {"data": [_APPLE_US]}}))
    async with _client(settings, fake) as figi:
        result = (await figi.map_cusips([APPLE]))[APPLE]

    assert result.outcome is FIGIOutcome.FOUND
    assert result.found and not result.is_ambiguous
    assert result.message is None
    assert result.matches == (
        FIGIMatch(
            figi="BBG000B9XRY4",
            ticker="AAPL",
            name="APPLE INC",
            exchange_code="US",
            security_type="Common Stock",
            security_type2="Common Stock",
            market_sector="Equity",
            composite_figi="BBG000B9XRY4",
            share_class_figi="BBG001S5N8V8",
        ),
    )


async def test_an_ambiguous_cusip_returns_every_match_in_order(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({APPLE: {"data": [_APPLE_US, _APPLE_UW]}}))
    async with _client(settings, fake) as figi:
        result = (await figi.map_cusips([APPLE]))[APPLE]

    assert result.is_ambiguous
    assert [match.exchange_code for match in result.matches] == ["US", "UW"]


async def test_not_found_is_a_result_not_an_exception(settings: Settings) -> None:
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake) as figi:
        result = (await figi.map_cusips([NOWHERE]))[NOWHERE]

    assert result.outcome is FIGIOutcome.NOT_FOUND
    assert result.matches == ()
    assert result.message == "No identifier found."


async def test_a_job_error_is_a_result_and_the_rest_of_the_batch_stands(
    settings: Settings,
) -> None:
    fake = FakeOpenFIGI(
        _answer({APPLE: {"data": [_APPLE_US]}, NOWHERE: {"error": "Invalid idValue format."}})
    )
    async with _client(settings, fake) as figi:
        results = await figi.map_cusips([APPLE, NOWHERE])

    assert results[APPLE].found
    assert results[NOWHERE].outcome is FIGIOutcome.ERROR
    assert results[NOWHERE].message == "Invalid idValue format."


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        ({"data": [_APPLE_US]}, FIGIOutcome.FOUND),
        ({"data": []}, FIGIOutcome.NOT_FOUND),
        ({"warning": "No identifier found."}, FIGIOutcome.NOT_FOUND),
        ({"error": "Invalid idValue format."}, FIGIOutcome.ERROR),
        ({"something": "new"}, FIGIOutcome.ERROR),
    ],
)
def test_classify(response: dict[str, Any], outcome: FIGIOutcome) -> None:
    assert classify(response) is outcome


async def test_a_response_that_cannot_be_paired_is_refused_and_not_logged(
    settings: Settings,
) -> None:
    def short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_NOT_FOUND])  # one answer for two jobs

    log = MemoryLog()
    limiter: Any = CountingLimiter()
    figi = OpenFIGIClient(settings, log, limiter=limiter, transport=httpx.MockTransport(short))
    async with figi:
        with pytest.raises(OpenFIGIResponseError):
            await figi.map_cusips([APPLE, NOWHERE])

    assert log.rows == []


# --- the response log -----------------------------------------------------------


async def test_every_response_is_logged_verbatim(settings: Settings) -> None:
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({APPLE: {"data": [_APPLE_US, _APPLE_UW]}}))
    async with _client(settings, fake, log) as figi:
        await figi.map_cusips([APPLE, NOWHERE])

    assert [(cusip, response) for cusip, response, _ in log.rows] == [
        (APPLE, {"data": [_APPLE_US, _APPLE_UW]}),
        (NOWHERE, _NOT_FOUND),
    ]


async def test_a_re_run_costs_no_requests(settings: Settings) -> None:
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({APPLE: {"data": [_APPLE_US]}}))
    async with _client(settings, fake, log) as figi:
        first = await figi.map_cusips([APPLE, NOWHERE])
        second = await figi.map_cusips([NOWHERE, APPLE])

    assert len(fake.requests) == 1
    assert second == {NOWHERE: first[NOWHERE], APPLE: first[APPLE]}
    assert list(second) == [NOWHERE, APPLE]


async def test_only_the_cusips_not_yet_logged_are_requested(settings: Settings) -> None:
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake, log) as figi:
        await figi.map_cusips([APPLE])
        await figi.map_cusips([APPLE, BERKSHIRE_B])

    assert [[job["idValue"] for job in batch] for batch in fake.batches] == [
        [APPLE],
        [BERKSHIRE_B],
    ]


async def test_a_logged_error_is_asked_again(settings: Settings) -> None:
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({NOWHERE: {"error": "Internal error."}}))
    async with _client(settings, fake, log) as figi:
        await figi.map_cusips([NOWHERE])
        fake.answer = _answer({})
        result = (await figi.map_cusips([NOWHERE]))[NOWHERE]

    assert len(fake.requests) == 2
    assert result.outcome is FIGIOutcome.NOT_FOUND


async def test_refresh_asks_again_and_the_newer_answer_wins(settings: Settings) -> None:
    """How a miss is retried once a listing may have appeared."""
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({}))
    async with _client(settings, fake, log) as figi:
        await figi.map_cusips([APPLE])
        fake.answer = _answer({APPLE: {"data": [_APPLE_US]}})
        refreshed = await figi.map_cusips([APPLE], refresh=True)
        afterwards = await figi.map_cusips([APPLE])

    assert len(fake.requests) == 2
    assert refreshed[APPLE].found
    assert afterwards[APPLE] == refreshed[APPLE]


# --- retries ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_transient_failures_are_retried(settings: Settings, status: int) -> None:
    limiter = CountingLimiter()
    fake = FakeOpenFIGI(_answer({}), statuses=[status, status])
    async with _client(settings, fake, limiter=limiter) as figi:
        result = (await figi.map_cusips([APPLE]))[APPLE]

    assert result.outcome is FIGIOutcome.NOT_FOUND
    assert len(fake.requests) == 3
    # Retries spend quota like any request, so each takes its own token.
    assert limiter.acquired == 3


async def test_retries_give_up_after_the_attempt_budget(settings: Settings) -> None:
    log = MemoryLog()
    fake = FakeOpenFIGI(_answer({}), statuses=[429] * 10)
    async with _client(settings, fake, log) as figi:
        with pytest.raises(OpenFIGIRateLimited):
            await figi.map_cusips([APPLE])

    assert len(fake.requests) == _MAX_ATTEMPTS
    assert log.rows == []


@pytest.mark.parametrize("status", [400, 401, 413, 500])
async def test_other_failures_are_not_retried(settings: Settings, status: int) -> None:
    fake = FakeOpenFIGI(_answer({}), statuses=[status])
    async with _client(settings, fake) as figi:
        with pytest.raises(httpx.HTTPStatusError):
            await figi.map_cusips([APPLE])

    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("7", 7.0),
        ("0", _MIN_RATE_LIMIT_WAIT_SECONDS),
        ("3600", _MAX_RATE_LIMIT_WAIT_SECONDS),
        ("soon", _MAX_RATE_LIMIT_WAIT_SECONDS),
        (None, _MAX_RATE_LIMIT_WAIT_SECONDS),
    ],
)
def test_a_429_waits_for_the_quota_reset(header: str | None, seconds: float) -> None:
    headers = {"ratelimit-reset": header} if header is not None else {}
    assert _reset_seconds(httpx.Response(429, headers=headers)) == seconds
