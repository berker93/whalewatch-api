"""Tests for the structlog configuration.

These assert on the *rendered stream*, not on captured event dicts. What matters
about this module is the bytes a log aggregator will receive — that production
emits parseable JSON, that a uvicorn record comes out in the same shape as ours,
that the level in settings actually silences something. A test against the
in-memory event dict would pass while the renderer emitted nothing at all.
"""

import io
import json
import logging
from typing import Any

import pytest
import structlog

from app.core.logging import configure_logging, get_logger
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _reset_context() -> Any:
    """Keep bound context from leaking between tests in this module."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _configure(**overrides: Any) -> io.StringIO:
    """Configure logging into a buffer and hand the buffer back."""
    stream = io.StringIO()
    configure_logging(make_settings(**overrides), stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line.strip()]


def test_production_renders_one_json_object_per_line() -> None:
    stream = _configure(environment="production")

    get_logger("test.json").info("filing.parsed", accession_no="0001234567-24-000123", rows=3)

    (line,) = _lines(stream)
    record = json.loads(line)
    assert record["event"] == "filing.parsed"
    assert record["accession_no"] == "0001234567-24-000123"
    assert record["rows"] == 3
    assert record["level"] == "info"
    # ISO-8601 in UTC, so lines from two services can be interleaved.
    assert record["timestamp"].endswith("Z")


def test_local_renders_human_readable_text_not_json() -> None:
    stream = _configure(environment="local")

    get_logger("test.console").info("filing.parsed", accession_no="0001234567-24-000123")

    (line,) = _lines(stream)
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "filing.parsed" in line
    assert "accession_no=0001234567-24-000123" in line
    # A StringIO is not a tty, so the renderer must not emit escape codes.
    assert "\x1b[" not in line


def test_staging_renders_json_like_production() -> None:
    """Staging is where you discover a field does not survive the log pipeline,
    which it can only show if it is rendered the way production renders."""
    stream = _configure(environment="staging")

    get_logger("test.staging").info("filing.parsed")

    assert json.loads(_lines(stream)[0])["event"] == "filing.parsed"


def test_stdlib_records_are_rendered_by_structlog() -> None:
    """uvicorn and SQLAlchemy log through the standard library and know nothing
    about structlog; their output still has to be the same shape as ours."""
    stream = _configure(environment="production")

    logging.getLogger("uvicorn.error").warning("Application startup complete.")

    record = json.loads(_lines(stream)[0])
    assert record["event"] == "Application startup complete."
    assert record["level"] == "warning"
    assert record["timestamp"].endswith("Z")


def test_stdlib_records_carry_bound_context() -> None:
    """The whole point of routing stdlib through structlog: a SQLAlchemy warning
    raised while handling a request is greppable by that request's id."""
    stream = _configure(environment="production")
    structlog.contextvars.bind_contextvars(request_id="abc123", job_name="backfill_13f")

    logging.getLogger("sqlalchemy.engine").warning("connection was invalidated")

    record = json.loads(_lines(stream)[0])
    assert record["request_id"] == "abc123"
    assert record["job_name"] == "backfill_13f"


def test_stdlib_percent_formatting_is_applied() -> None:
    stream = _configure(environment="production")

    logging.getLogger("uvicorn.error").warning("listening on %s:%d", "0.0.0.0", 8000)

    assert json.loads(_lines(stream)[0])["event"] == "listening on 0.0.0.0:8000"


def test_exception_is_serialised_as_a_string_field() -> None:
    """A traceback object is not JSON-serialisable; if format_exc_info were
    missing this line would either vanish or crash the handler."""
    stream = _configure(environment="production")

    try:
        raise ValueError("bad accession")
    except ValueError:
        get_logger("test.exc").exception("filing.failed", accession_no="0001-24-000001")

    record = json.loads(_lines(stream)[0])
    assert record["level"] == "error"
    assert "ValueError: bad accession" in record["exception"]


def test_log_level_comes_from_settings() -> None:
    stream = _configure(environment="production", log_level="WARNING")
    logger = get_logger("test.level.warning")

    logger.info("suppressed")
    logger.warning("emitted")

    events = [json.loads(line)["event"] for line in _lines(stream)]
    assert events == ["emitted"]


def test_log_level_applies_to_stdlib_loggers_too() -> None:
    stream = _configure(environment="production", log_level="ERROR")

    logging.getLogger("uvicorn.error").warning("suppressed")
    logging.getLogger("uvicorn.error").error("emitted")

    events = [json.loads(line)["event"] for line in _lines(stream)]
    assert events == ["emitted"]


def test_debug_level_lets_debug_events_through() -> None:
    stream = _configure(environment="production", log_level="DEBUG")

    get_logger("test.level.debug").debug("chatty")

    assert json.loads(_lines(stream)[0])["event"] == "chatty"


def test_reconfiguring_does_not_duplicate_handlers() -> None:
    """uvicorn configures logging before importing the app, and every test that
    builds an app configures it again. Adding rather than replacing handlers
    would print each event once per call."""
    for _ in range(3):
        stream = _configure(environment="production")

    assert len(logging.getLogger().handlers) == 1
    get_logger("test.dupes").info("once")
    assert len(_lines(stream)) == 1


def test_uvicorn_access_log_is_suppressed() -> None:
    """The middleware emits the access line, with a request_id and a duration.
    uvicorn's own is a strictly worse duplicate of the same request."""
    stream = _configure(environment="production")

    logging.getLogger("uvicorn.access").info('GET /health HTTP/1.1" 200')

    assert _lines(stream) == []
