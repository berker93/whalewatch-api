"""Tests for the request_id middleware and the per-request access log."""

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.api.middleware import REQUEST_ID_HEADER
from app.core.config import Settings
from app.core.logging import configure_logging
from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture
def log_stream(app: FastAPI, settings: Settings) -> Iterator[io.StringIO]:
    """Re-point logging at a buffer *after* the app fixture has built the app.

    ``create_app`` configures logging itself, so configuring before it would be
    undone. Production JSON is used regardless of the app's environment because
    these assertions are about fields, and parsing them beats regexing a console
    line.
    """
    stream = io.StringIO()
    configure_logging(make_settings(environment="production"), stream=stream)
    yield stream
    structlog.contextvars.clear_contextvars()
    configure_logging(settings)


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _access_line(stream: io.StringIO) -> dict[str, Any]:
    (line,) = [e for e in _events(stream) if e["event"] in {"request_completed", "request_failed"}]
    return line


async def test_response_carries_a_request_id_header(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


async def test_generated_request_ids_differ_between_requests(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_inbound_request_id_is_reused(client: AsyncClient) -> None:
    """A trace that starts at the edge proxy must keep one id across every hop,
    or correlating the two services' logs means joining on timestamps."""
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "edge-abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "edge-abc-123"


@pytest.mark.parametrize(
    ("hostile", "reason"),
    [
        ("x" * 129, "longer than the cap"),
        ("has spaces", "not in the safe alphabet"),
        ('", "level": "info', "would forge JSON fields"),
        ("line\nbreak", "would forge a second log line"),
        ("", "empty"),
    ],
)
async def test_unusable_inbound_request_id_is_replaced(
    client: AsyncClient, hostile: str, reason: str
) -> None:
    """The inbound header is attacker-controlled and lands in every log line for
    the request, so it is validated rather than trusted."""
    response = await client.get("/health", headers={REQUEST_ID_HEADER: hostile})

    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != hostile, reason
    assert echoed.isalnum()


async def test_access_line_has_the_required_fields(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    response = await client.get("/health")

    line = _access_line(log_stream)
    assert line["event"] == "request_completed"
    assert line["method"] == "GET"
    assert line["path"] == "/health"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    # The join key: the id in the log is the id the caller was handed back.
    assert line["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_access_line_records_the_real_status_code(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    await client.get("/no-such-route")

    assert _access_line(log_stream)["status"] == 404


async def test_query_string_is_logged_when_present(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    await client.get("/health?verbose=1")

    assert _access_line(log_stream)["query"] == "verbose=1"


async def test_query_string_is_omitted_when_absent(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    await client.get("/health")

    assert "query" not in _access_line(log_stream)


async def test_request_id_is_bound_for_logs_emitted_inside_the_handler(
    settings: Settings,
) -> None:
    """The point of the whole exercise: a line logged deep inside a handler is
    findable by the id the caller was given, without being passed one."""
    app = create_app(settings)

    @app.get("/emits")
    async def emits() -> dict[str, str]:
        structlog.get_logger("test.handler").info(
            "filing.parsed", accession_no="0001234567-24-000123"
        )
        return {}

    stream = io.StringIO()
    configure_logging(make_settings(environment="production"), stream=stream)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/emits", headers={REQUEST_ID_HEADER: "trace-42"})
    finally:
        structlog.contextvars.clear_contextvars()
        configure_logging(settings)

    assert response.headers[REQUEST_ID_HEADER] == "trace-42"
    inner = next(e for e in _events(stream) if e["event"] == "filing.parsed")
    assert inner["request_id"] == "trace-42"
    assert inner["accession_no"] == "0001234567-24-000123"


async def test_handler_can_read_the_request_id_off_request_state(settings: Settings) -> None:
    """Exposed on the scope as well as the context so a handler that wants to
    put the id in a response body does not have to import structlog."""
    app = create_app(settings)

    @app.get("/echo-id")
    async def echo_id(request: Request) -> dict[str, str]:
        request_id: str = request.state.request_id
        return {"request_id": request_id}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/echo-id", headers={REQUEST_ID_HEADER: "state-7"})

    assert response.json() == {"request_id": "state-7"}


async def test_unhandled_exception_is_logged_with_its_request_id(settings: Settings) -> None:
    """The failure path is the one that matters. The exception must reach the
    log with the request's id and a traceback, and must still propagate so that
    Starlette's error middleware — not this one — decides what a 500 looks like.
    """
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    stream = io.StringIO()
    configure_logging(make_settings(environment="production"), stream=stream)
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/boom", headers={REQUEST_ID_HEADER: "doomed-1"})
    finally:
        structlog.contextvars.clear_contextvars()
        configure_logging(settings)

    assert response.status_code == 500
    line = _access_line(stream)
    assert line["event"] == "request_failed"
    assert line["request_id"] == "doomed-1"
    assert line["status"] == 500
    assert line["path"] == "/boom"
    assert "RuntimeError: kaboom" in line["exception"]


async def test_context_does_not_leak_between_requests(
    client: AsyncClient, log_stream: io.StringIO
) -> None:
    """Two requests must never share a request_id, which is exactly what a
    thread-local would get wrong once the event loop interleaves them."""
    first = await client.get("/health")
    second = await client.get("/health")

    ids = [e["request_id"] for e in _events(log_stream) if e["event"] == "request_completed"]
    assert ids == [first.headers[REQUEST_ID_HEADER], second.headers[REQUEST_ID_HEADER]]
    assert len(set(ids)) == 2
    # And nothing is left bound once the request is over.
    assert structlog.contextvars.get_contextvars() == {}


async def test_request_id_survives_every_response_path(settings: Settings) -> None:
    """A 200, a handled 4xx and an unhandled 500 must all come back with the id.

    The 500 is the interesting one: Starlette renders it in ServerErrorMiddleware,
    which is outside every middleware this app can install, so it is reached by
    an exception handler instead — and it is the case where the caller most
    needs an id to quote.
    """
    app = create_app(settings)

    @app.get("/boom-2")
    async def boom_2() -> None:
        raise RuntimeError("kaboom")

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403, detail="nope")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        results = {
            path: await http.get(path, headers={REQUEST_ID_HEADER: "probe-1"})
            for path in ("/health", "/forbidden", "/boom-2")
        }

    assert [r.status_code for r in results.values()] == [200, 403, 500]
    for path, response in results.items():
        assert response.headers.get(REQUEST_ID_HEADER) == "probe-1", path
    # The 500 body is left exactly as Starlette would have rendered it; the
    # handler exists to add a header, not to redesign the error response.
    assert results["/boom-2"].text == "Internal Server Error"
