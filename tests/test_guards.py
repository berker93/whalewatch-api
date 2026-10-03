"""The layer in front of every route: errors, CORS, rate limiting, timeouts, headers, body size.

The rate limiter here is a stub on ``app.state``; the Lua script that does the
counting runs against a real Redis in ``tests/integration/test_rate_limit.py``.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.errors import INTERNAL_ERROR_MESSAGE
from app.api.middleware import REQUEST_ID_HEADER
from app.api.rate_limit import Decision, bucket
from app.main import create_app
from tests.conftest import make_settings

FRONTEND = "http://localhost:5173"


class StubLimiter:
    """Admits ``budget`` requests, then refuses with ``retry_after``."""

    def __init__(self, budget: int = 60, retry_after: int = 17, error: Exception | None = None):
        self.limit = budget
        self.budget = budget
        self.retry_after = retry_after
        self.error = error
        self.clients: list[str] = []

    async def hit(self, client: str) -> Decision:
        if self.error is not None:
            raise self.error
        self.clients.append(client)
        if self.budget == 0:
            return Decision(admitted=False, remaining=0, retry_after=self.retry_after)
        self.budget -= 1
        return Decision(admitted=True, remaining=self.budget, retry_after=0)


def _build(**overrides: Any) -> FastAPI:
    app = create_app(make_settings(**overrides))

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("password=hunter2 in a stack frame")

    @app.get("/v1/slow")
    async def slow() -> dict[str, bool]:
        app.state.slow_started = True
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            app.state.slow_cancelled = True
            raise
        return {"finished": True}

    @app.get("/v1/socket-timeout")
    async def socket_timeout() -> None:
        raise TimeoutError("a socket's, not the request's")

    @app.get("/v1/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/echo")
    async def echo(body: dict[str, Any]) -> dict[str, Any]:
        return body

    return app


@pytest.fixture
def app() -> FastAPI:
    return _build()


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# --- errors ------------------------------------------------------------------------


async def test_an_unhandled_exception_is_a_generic_500_with_the_request_id(
    http: AsyncClient,
) -> None:
    response = await http.get("/v1/boom", headers={REQUEST_ID_HEADER: "abc-123"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": INTERNAL_ERROR_MESSAGE,
            "detail": {},
            "request_id": "abc-123",
        }
    }
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text


async def test_debug_never_turns_the_500_into_a_traceback() -> None:
    """DEBUG=true is in .env.example. Starlette's debug mode would answer with
    the traceback, and the frames' source with it."""
    app = _build(debug=True)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text


async def test_every_error_carries_the_request_id(http: AsyncClient) -> None:
    response = await http.get("/v1/nothing-here", headers={REQUEST_ID_HEADER: "find-me"})

    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == "find-me"


async def test_a_raised_http_exception_keeps_its_headers(app: FastAPI, http: AsyncClient) -> None:
    from fastapi import HTTPException

    @app.get("/v1/teapot")
    async def teapot() -> None:
        raise HTTPException(418, "short and stout", headers={"x-spout": "yes"})

    response = await http.get("/v1/teapot")

    assert response.status_code == 418
    assert response.headers["x-spout"] == "yes"
    assert response.json()["error"] | {"request_id": None} == {
        "code": "http_error",
        "message": "short and stout",
        "detail": {},
        "request_id": None,
    }


# --- security headers --------------------------------------------------------------


async def test_every_response_carries_the_security_headers(http: AsyncClient) -> None:
    for path in ("/health", "/v1/ok", "/v1/nothing-here", "/v1/boom"):
        response = await http.get(path)
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "no-referrer", path
        assert "strict-transport-security" not in response.headers, path


async def test_hsts_only_in_production() -> None:
    app = _build(environment="production")
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        for path in ("/health", "/v1/boom"):
            response = await http.get(path)
            assert response.headers["strict-transport-security"].startswith("max-age="), path


# --- CORS --------------------------------------------------------------------------


async def test_a_known_origin_may_read_responses(http: AsyncClient) -> None:
    response = await http.get("/v1/ok", headers={"origin": FRONTEND})

    assert response.headers["access-control-allow-origin"] == FRONTEND
    exposed = response.headers["access-control-expose-headers"].lower()
    for header in ("etag", "retry-after", REQUEST_ID_HEADER):
        assert header in exposed


async def test_an_unknown_origin_may_not(http: AsyncClient) -> None:
    response = await http.get("/v1/ok", headers={"origin": "https://evil.example"})

    assert response.status_code == 200  # CORS is the browser's to enforce
    assert "access-control-allow-origin" not in response.headers


async def test_the_allow_list_is_never_a_wildcard(http: AsyncClient) -> None:
    response = await http.options(
        "/v1/ok",
        headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
    )

    assert response.headers.get("access-control-allow-origin") != "*"


async def test_a_preflight_for_a_revalidation_is_allowed(http: AsyncClient) -> None:
    response = await http.options(
        "/v1/ok",
        headers={
            "origin": FRONTEND,
            "access-control-request-method": "GET",
            "access-control-request-headers": "if-none-match",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND


async def test_a_preflight_for_a_write_is_refused(http: AsyncClient) -> None:
    response = await http.options(
        "/v1/ok", headers={"origin": FRONTEND, "access-control-request-method": "DELETE"}
    )

    assert response.status_code == 400


async def test_a_browser_can_read_the_500_it_needs_to_report(http: AsyncClient) -> None:
    response = await http.get("/v1/boom", headers={"origin": FRONTEND})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == FRONTEND


async def test_configured_origins_replace_the_dev_servers() -> None:
    app = _build(cors_origins=["https://whalewatch.io"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        ours = await http.get("/v1/ok", headers={"origin": "https://whalewatch.io"})
        dev = await http.get("/v1/ok", headers={"origin": FRONTEND})

    assert ours.headers["access-control-allow-origin"] == "https://whalewatch.io"
    assert "access-control-allow-origin" not in dev.headers


# --- rate limiting -----------------------------------------------------------------


async def test_over_the_limit_is_a_429_with_retry_after(app: FastAPI, http: AsyncClient) -> None:
    app.state.rate_limiter = StubLimiter(budget=1, retry_after=17)

    first = await http.get("/v1/ok")
    second = await http.get("/v1/ok", headers={REQUEST_ID_HEADER: "too-many", "origin": FRONTEND})

    assert first.status_code == 200
    assert (first.headers["x-ratelimit-limit"], first.headers["x-ratelimit-remaining"]) == (
        "1",
        "0",
    )
    assert second.status_code == 429
    assert second.headers["retry-after"] == "17"
    assert second.headers["access-control-allow-origin"] == FRONTEND
    body = second.json()["error"]
    assert (body["code"], body["request_id"]) == ("rate_limited", "too-many")
    assert "17 seconds" in body["message"]


async def test_the_limit_is_per_client_address(app: FastAPI, http: AsyncClient) -> None:
    limiter = app.state.rate_limiter = StubLimiter()

    await http.get("/v1/ok")

    assert limiter.clients == ["127.0.0.1"]


async def test_probes_are_not_counted(app: FastAPI, http: AsyncClient) -> None:
    limiter = app.state.rate_limiter = StubLimiter(budget=0)

    assert (await http.get("/health")).status_code == 200
    assert limiter.clients == []


async def test_a_redis_outage_lets_requests_through(app: FastAPI, http: AsyncClient) -> None:
    app.state.rate_limiter = StubLimiter(error=RedisConnectionError("Connection refused."))

    response = await http.get("/v1/ok")

    assert response.status_code == 200
    assert "x-ratelimit-limit" not in response.headers


@pytest.mark.parametrize(
    ("client", "counted_as"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
    ],
)
def test_an_ipv6_client_is_counted_by_its_64(client: str, counted_as: str) -> None:
    """An ISP hands a subscriber a /64; counting each address in it would give
    one client 2^64 budgets."""
    assert bucket(client) == counted_as


# --- timeout -----------------------------------------------------------------------


@pytest.fixture
def quick() -> Iterator[FastAPI]:
    yield _build(request_timeout_seconds=0.05)


async def test_a_slow_request_is_a_504_and_is_cancelled(quick: FastAPI) -> None:
    transport = ASGITransport(app=quick, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/slow", headers={REQUEST_ID_HEADER: "slow-1"})

    assert response.status_code == 504
    body = response.json()["error"]
    assert (body["code"], body["request_id"]) == ("timeout", "slow-1")
    assert quick.state.slow_cancelled


async def test_a_timeout_from_inside_the_handler_is_not_ours(quick: FastAPI) -> None:
    """A socket's TimeoutError is an unhandled exception, not a slow request."""
    transport = ASGITransport(app=quick, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/socket-timeout")

    assert response.status_code == 500


# --- body size ---------------------------------------------------------------------


@pytest.fixture
def small() -> FastAPI:
    return _build(max_request_body_bytes=16)


async def test_a_declared_body_over_the_limit_is_a_413(small: FastAPI) -> None:
    transport = ASGITransport(app=small)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/v1/echo", json={"padding": "x" * 64})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_a_streamed_body_over_the_limit_is_a_413(small: FastAPI) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"padding": "'
        yield b"x" * 64
        yield b'"}'

    transport = ASGITransport(app=small)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/v1/echo", content=chunks(), headers={"content-type": "application/json"}
        )

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_a_body_under_the_limit_is_read(small: FastAPI) -> None:
    transport = ASGITransport(app=small)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/v1/echo", json={"a": 1})

    assert response.json() == {"a": 1}
