"""The two probes, and the rules an orchestrator relies on.

The dependency stubs below are deliberate: a readiness test that needs a live
Postgres cannot assert the interesting case — the one where Postgres is *gone* —
without stopping a container mid-suite. Overriding ``get_engine``/``get_redis``
makes "the database times out" a one-line fixture.
"""

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.api.deps import get_engine, get_redis
from app.api.routers import health
from app.core.config import Settings
from app.core.version import VERSION
from app.main import create_app
from tests.conftest import make_settings

# --- stubs -------------------------------------------------------------------


class StubConnection:
    def __init__(self, engine: "StubEngine") -> None:
        self._engine = engine

    async def __aenter__(self) -> "StubConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: Any) -> None:
        await asyncio.sleep(self._engine.delay)
        if self._engine.error is not None:
            raise self._engine.error


class StubEngine:
    """Stands in for AsyncEngine. /ready only ever calls connect(); the lifespan
    also calls dispose()."""

    def __init__(self, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.disposed = False

    def connect(self) -> StubConnection:
        return StubConnection(self)

    async def dispose(self) -> None:
        self.disposed = True


class StubRedis:
    def __init__(self, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.closed = False

    async def ping(self) -> bool:
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return True

    async def aclose(self) -> None:
        self.closed = True


def use_stubs(app: FastAPI, engine: StubEngine, redis: StubRedis) -> None:
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_redis] = lambda: redis


def declared_version() -> str:
    """The version in pyproject.toml, read independently of the app's resolver."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        version: str = tomllib.load(handle)["project"]["version"]
    return version


DECLARED_VERSION = declared_version()


def operational_error(message: str) -> OperationalError:
    """A DBAPI-shaped error, since that is what a dead Postgres actually raises."""
    return OperationalError("SELECT 1", {}, Exception(message))


# --- /health -----------------------------------------------------------------


class TestLiveness:
    async def test_answers_without_any_dependency_configured(self, client: AsyncClient) -> None:
        """The AC as a test: no stubs are installed and no lifespan has run, so
        ``app.state`` holds no engine and no Redis. A /health that did any I/O
        could not answer this request — which is exactly the failure mode this
        protects against, where a dead database turns into a restart loop."""
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_reports_version_and_git_sha(self, app: FastAPI) -> None:
        """Deploy forensics: the two fields that answer "what is actually
        serving?" without shelling into the container."""
        app.state.settings = make_settings(git_sha="9f2c1a0deadbeef")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        body = response.json()
        assert body["git_sha"] == "9f2c1a0deadbeef"
        assert body["version"] == VERSION

    async def test_version_matches_pyproject(self, client: AsyncClient) -> None:
        """The fallback must stay a fallback. This project has no
        build-system, so nothing installs a distribution for
        ``importlib.metadata`` to find — without the pyproject fallback every
        deployed container reports "unknown" and the field is decoration."""
        response = await client.get("/health")

        assert response.json()["version"] == DECLARED_VERSION
        assert response.json()["version"] != "unknown"

    async def test_git_sha_falls_back_when_unstamped(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local uvicorn has no build to stamp; that must not be a 500."""
        monkeypatch.delenv("GIT_SHA", raising=False)
        app.state.settings = make_settings()
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.json()["git_sha"] == "unknown"


# --- /ready ------------------------------------------------------------------


class TestReadiness:
    async def test_all_dependencies_up(self, app: FastAPI, client: AsyncClient) -> None:
        use_stubs(app, StubEngine(), StubRedis())

        response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}

    async def test_redis_down_is_503_naming_redis(self, app: FastAPI, client: AsyncClient) -> None:
        use_stubs(app, StubEngine(), StubRedis(error=RedisConnectionError("connection refused")))

        response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["postgres"] == "ok"
        assert body["checks"]["redis"].startswith("error:")

    async def test_postgres_down_is_503_naming_postgres(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        use_stubs(app, StubEngine(error=operational_error("connection refused")), StubRedis())

        response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["checks"]["redis"] == "ok"
        assert body["checks"]["postgres"].startswith("error:")

    async def test_every_dependency_is_reported_not_just_the_first(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """No short-circuiting. When both are down, one probe should tell you
        both — otherwise fixing Postgres just reveals the Redis outage."""
        use_stubs(
            app,
            StubEngine(error=operational_error("down")),
            StubRedis(error=RedisConnectionError("down")),
        )

        response = await client.get("/ready")

        checks = response.json()["checks"]
        assert checks["postgres"].startswith("error:")
        assert checks["redis"].startswith("error:")

    async def test_a_hanging_dependency_times_out(
        self, app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AC that matters most: a probe that hangs leaves the instance
        neither in nor out of rotation, so the deadline is ours, not the
        socket's. The stub never returns; the response still arrives."""
        monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
        use_stubs(app, StubEngine(), StubRedis(delay=30))

        response = await asyncio.wait_for(client.get("/ready"), timeout=5)

        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "error: timeout"

    async def test_checks_run_concurrently(self, app: FastAPI, client: AsyncClient) -> None:
        """Sequential checks add up: with n dependencies the probe's worst case
        becomes n x the timeout, which is how a readiness probe outlives the
        orchestrator's patience."""
        use_stubs(app, StubEngine(delay=0.2), StubRedis(delay=0.2))

        started = asyncio.get_running_loop().time()
        await client.get("/ready")
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.35

    async def test_failure_detail_does_not_leak_the_dsn(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """/ready is unauthenticated, and asyncpg puts the whole DSN in its
        connection errors. The check reports the exception type, not its text."""
        use_stubs(
            app,
            StubEngine(error=operational_error("postgresql://whalewatch:hunter2@db:5432/w")),
            StubRedis(),
        )

        response = await client.get("/ready")

        assert "hunter2" not in response.text
        assert response.json()["checks"]["postgres"] == "error: OperationalError"


# --- docs --------------------------------------------------------------------


class TestDocs:
    async def test_exposed_outside_production(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200

    async def test_hidden_in_production(self) -> None:
        """The schema goes too — hiding /docs while still serving
        /openapi.json publishes the same map in a less convenient format."""
        app = create_app(make_settings(environment="production"))
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/redoc")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404

    async def test_probes_still_work_in_production(self) -> None:
        """Turning docs off must not turn the probes off with them."""
        app = create_app(make_settings(environment="production"))
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200


# --- lifespan ----------------------------------------------------------------


class TestLifespan:
    def test_creates_pools_on_startup_and_disposes_them_on_shutdown(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undisposed pools are the kind of leak that only shows up as
        ``too many clients already`` after the fiftieth reload. TestClient is
        used here rather than ASGITransport precisely because it runs the
        lifespan."""
        engine, redis = StubEngine(), StubRedis()
        monkeypatch.setattr("app.main.create_engine", lambda _settings: engine)
        monkeypatch.setattr("app.main.create_redis", lambda _settings: redis)

        app = create_app(settings)

        with TestClient(app) as test_client:
            assert app.state.engine is engine
            assert app.state.redis is redis
            assert not engine.disposed
            assert test_client.get("/ready").status_code == 200

        assert engine.disposed
        assert redis.closed

    def test_pools_are_disposed_even_when_serving_raises(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash on the way down still has to return the connections."""
        engine, redis = StubEngine(), StubRedis()
        monkeypatch.setattr("app.main.create_engine", lambda _settings: engine)
        monkeypatch.setattr("app.main.create_redis", lambda _settings: redis)

        app = create_app(settings)

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError), TestClient(app) as test_client:
            test_client.get("/boom")

        assert engine.disposed
        assert redis.closed

    def test_pools_are_built_from_the_apps_own_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The factory's contract: an app built from other settings must build
        its pools from those, not from the process-wide ``get_settings()``."""
        seen: list[Settings] = []

        def record(settings: Settings) -> StubEngine:
            seen.append(settings)
            return StubEngine()

        monkeypatch.setattr("app.main.create_engine", record)
        monkeypatch.setattr("app.main.create_redis", lambda _settings: StubRedis())

        other = make_settings(postgres_host="elsewhere.internal")

        with TestClient(create_app(other)):
            pass

        assert seen == [other]
