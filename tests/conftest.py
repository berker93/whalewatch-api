"""Fixtures for building throwaway apps.

Every app here is built by ``create_app`` from settings constructed in the test
with ``_env_file=None``. Nothing reads the developer's real ``.env``, and nothing
touches ``get_settings()``, so no test can be made to pass or fail by a
gitignored file or by the order tests happen to run in.
"""

import os
from collections.abc import AsyncIterator
from typing import Any

# Before any import of app.main, which builds the module-level app — and so
# validates the environment — at import time. That is the behaviour we want in
# production (uvicorn dies at boot naming the missing variable) but it means a
# developer whose .env lacks SEC_CONTACT_EMAIL cannot even collect this suite.
# setdefault, not assignment: a CI job that exports real values still wins.
os.environ.setdefault("POSTGRES_PASSWORD", "localdev")
os.environ.setdefault("SEC_CONTACT_EMAIL", "tests@whalewatch.io")

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.accounts.sessions import SessionStore
from app.api.auth import OptionalViewerDep
from app.api.deps import get_redis
from app.api.entitlements import GATED, EntitlementContext, entitlement_context
from app.core.config import Settings
from app.derived.scope import Pair
from app.main import create_app
from tests.fake_redis import FakeRedis


def make_settings(**overrides: Any) -> Settings:
    """Settings with the two required fields filled in and nothing from ``.env``."""
    defaults: dict[str, Any] = {
        "postgres_password": SecretStr("localdev"),
        "sec_contact_email": "ops@whalewatch.io",
        "environment": "test",
    }
    # Settings refuses the local raw store, unset CORS origins, an unset web
    # URL and logged email in a deployed environment, and the tests that ask for
    # "production" are asking about logging or /health, not about where
    # documents are archived, who may call, or where mail goes. Nothing here
    # opens the store or sends a message.
    if overrides.get("environment") in ("staging", "production"):
        defaults |= {
            "raw_store_backend": "s3",
            "raw_store_s3_bucket": "whalewatch-test",
            "cors_origins": ["https://whalewatch.io"],
            "web_url": "https://app.whalewatch.io",
            "api_url": "https://api.whalewatch.io",
            "email_from": "WhaleWatch <no-reply@mail.whalewatch.io>",
            "email_backend": "postmark",
            "postmark_server_token": SecretStr("postmark-test-token"),
            "postmark_webhook_secret": SecretStr("postmark-test-webhook"),
            "email_unsubscribe_secret": SecretStr("unsubscribe-test-secret"),
        }
    return Settings(_env_file=None, **{**defaults, **overrides})


async def unlimited_entitlements(request: Request, viewer: OptionalViewerDep) -> EntitlementContext:
    """What a test that is not about plans runs as: every period and day, the
    request marked gated as the real dependency marks it. The fixtures' 13Fs
    are 2024's, which Free's window, counted back from today, would hide. The
    viewer's write limits are still its plan's.

    A test about plans takes this away:
    ``app.dependency_overrides.pop(entitlement_context)``.
    """
    setattr(request.state, GATED, True)
    return EntitlementContext.unlimited(viewer=viewer)


@pytest.fixture(autouse=True)
def alert_queue(monkeypatch: pytest.MonkeyPatch) -> list[frozenset[Pair] | None]:
    """What every committed rebuild sent the alerts task, kept instead of sent.

    For every test: a load or a recompute anywhere in the suite would
    otherwise send a message to whatever broker the environment names.
    ``None`` is a rebuild of everything.
    """
    sent: list[frozenset[Pair] | None] = []
    monkeypatch.setattr("app.alerts.dispatch.enqueue", sent.append)
    return sent


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client speaking to the app in-process.

    ASGITransport does not run the lifespan, which is the point: these tests
    supply their own stub dependencies and must never open a socket to a real
    Postgres or Redis. The lifespan gets its own test, with TestClient.

    Each request gets an empty fake Redis unless the test overrides
    ``get_redis`` first, so a cached endpoint builds its answer every time.
    """
    app.dependency_overrides.setdefault(get_redis, lambda: FakeRedis())
    app.dependency_overrides.setdefault(entitlement_context, unlimited_entitlements)
    app.state.sessions = SessionStore(FakeRedis())  # type: ignore[arg-type]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
