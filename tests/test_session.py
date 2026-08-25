"""Tests for engine/session wiring.

None of these touch a real Postgres. What is being tested is lifetime and
configuration — that a session is per-request, that it is closed on both exit
paths, that the pool and the naming convention are what we said. Behaviour
against a live database belongs in the integration suite.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import QueuePool

from app.api.deps import SessionDep, get_session
from app.core.config import Settings
from app.db.models.base import Base
from app.db.session import create_engine, create_session_factory, session_scope
from tests.conftest import make_settings

# --- engine configuration ---------------------------------------------------


def test_engine_pool_is_configured(settings: Settings) -> None:
    """The numbers in the AC, read back off the pool that got built."""
    pool = create_engine(settings).pool
    # A NullPool here would silently mean "no pooling at all" and still pass
    # anything that only checked the numbers.
    assert isinstance(pool, QueuePool)
    assert pool.size() == 5
    # Not exposed as a public attribute; the private one is the only reader.
    assert pool._max_overflow == 10
    assert pool._pre_ping is True


def test_engine_never_echoes(settings: Settings) -> None:
    """Echo would log bound parameters — filing bodies, and the DSN on connect."""
    assert create_engine(settings).echo is False


# --- session factory --------------------------------------------------------


def test_factory_does_not_expire_on_commit(settings: Settings) -> None:
    """Expiry after commit means lazy I/O during serialization: MissingGreenlet."""
    factory = create_session_factory(create_engine(settings))
    assert factory.kw["expire_on_commit"] is False
    assert factory.kw["autoflush"] is False


# --- the request dependency -------------------------------------------------


def build_probe_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """An app with the real dependency over a caller-supplied factory."""
    app = FastAPI()
    app.state.session_factory = factory

    @app.get("/session-id")
    async def read_session_id(session: SessionDep) -> dict[str, int]:
        return {"id": id(session)}

    @app.get("/boom")
    async def boom(session: SessionDep) -> dict[str, str]:
        raise RuntimeError("handler exploded")

    return app


class RecordingSession:
    """Stands in for AsyncSession, recording whether it was closed."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.closed = False

    async def __aenter__(self) -> "RecordingSession":
        self.log.append("open")
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self.closed = True
        self.log.append("close")


class RecordingFactory:
    """Stands in for async_sessionmaker."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.sessions: list[RecordingSession] = []

    def __call__(self) -> RecordingSession:
        session = RecordingSession(self.log)
        self.sessions.append(session)
        return session


@pytest.fixture
def factory() -> RecordingFactory:
    return RecordingFactory()


@pytest.fixture
async def probe_client(factory: RecordingFactory) -> AsyncIterator[AsyncClient]:
    app = build_probe_app(factory)  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_session_is_closed_after_a_successful_request(
    probe_client: AsyncClient, factory: RecordingFactory
) -> None:
    await probe_client.get("/session-id")

    assert factory.log == ["open", "close"]
    assert factory.sessions[0].closed


async def test_session_is_closed_when_the_handler_raises(
    probe_client: AsyncClient, factory: RecordingFactory
) -> None:
    """The leak that matters: a 500 must still return the connection."""
    with pytest.raises(RuntimeError, match="handler exploded"):
        await probe_client.get("/boom")

    assert factory.sessions[0].closed


async def test_each_request_gets_its_own_session(
    probe_client: AsyncClient, factory: RecordingFactory
) -> None:
    """A session shared across requests carries one request's transaction
    into the next; two requests must never see the same object."""
    await probe_client.get("/session-id")
    await probe_client.get("/session-id")

    assert len(factory.sessions) == 2
    assert factory.sessions[0] is not factory.sessions[1]
    assert factory.log == ["open", "close", "open", "close"]


async def test_get_session_is_overridable(factory: RecordingFactory) -> None:
    """Handlers depend on get_session, so a test can swap the whole thing out."""
    app = build_probe_app(factory)  # type: ignore[arg-type]
    sentinel = object()

    async def fake_session() -> AsyncIterator[Any]:
        yield sentinel

    app.dependency_overrides[get_session] = fake_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session-id")

    assert response.json() == {"id": id(sentinel)}
    assert factory.sessions == []


def test_lifespan_builds_the_factory_the_dependency_reads(settings: Settings) -> None:
    """get_session reads app.state.session_factory, so the lifespan has to put a
    real factory there — bound to the same engine, and gone once it exits.

    TestClient runs the lifespan for real. Safe without Postgres: create_engine
    and create_redis both build lazy pools and do no I/O until first use.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app):
        factory = app.state.session_factory
        assert isinstance(factory, async_sessionmaker)
        assert factory.kw["bind"] is app.state.engine
        assert factory.kw["expire_on_commit"] is False


# --- the non-request scope --------------------------------------------------


async def test_session_scope_disposes_its_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI/Celery scope owns its pool and must give it back, or a worker
    accumulates one pool per task until Postgres refuses connections."""
    disposed: list[bool] = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    log: list[str] = []
    session = RecordingSession(log)
    committed: list[str] = []
    session.commit = _record(committed, "commit")  # type: ignore[attr-defined]
    session.rollback = _record(committed, "rollback")  # type: ignore[attr-defined]

    monkeypatch.setattr("app.db.session.create_engine", lambda _s: FakeEngine())
    monkeypatch.setattr("app.db.session.create_session_factory", lambda _e: lambda: session)

    async with session_scope(make_settings()) as scoped:
        assert scoped is session  # type: ignore[comparison-overlap]

    assert committed == ["commit"]
    assert disposed == [True]
    assert session.closed


async def test_session_scope_rolls_back_and_disposes_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[bool] = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    calls: list[str] = []
    session = RecordingSession([])
    session.commit = _record(calls, "commit")  # type: ignore[attr-defined]
    session.rollback = _record(calls, "rollback")  # type: ignore[attr-defined]

    monkeypatch.setattr("app.db.session.create_engine", lambda _s: FakeEngine())
    monkeypatch.setattr("app.db.session.create_session_factory", lambda _e: lambda: session)

    with pytest.raises(ValueError, match="task failed"):
        async with session_scope(make_settings()):
            raise ValueError("task failed")

    assert calls == ["rollback"]
    assert disposed == [True]


def _record(log: list[str], name: str) -> Any:
    async def call() -> None:
        log.append(name)

    return call


# --- naming convention ------------------------------------------------------


def test_constraints_are_named_deterministically() -> None:
    """The whole point: every constraint has a name we can write into a
    later migration's drop_constraint, identical in every environment."""
    metadata = Base.metadata
    Table(
        "widget",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("owner_id", Integer, ForeignKey("owner.id")),
        Column("slug", String, unique=True, index=True),
        UniqueConstraint("slug", name=None),
    )
    Table("owner", metadata, Column("id", Integer, primary_key=True))

    widget = metadata.tables["widget"]
    names = {c.name for c in widget.constraints if c.name is not None}

    assert "pk_widget" in names
    assert "fk_widget_owner_id_owner" in names
    assert "uq_widget_slug" in names
    assert {i.name for i in widget.indexes} == {"ix_widget_slug"}

    metadata.remove(widget)
    metadata.remove(metadata.tables["owner"])


def test_naming_convention_is_attached_to_the_metadata() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
