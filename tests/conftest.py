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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


def make_settings(**overrides: Any) -> Settings:
    """Settings with the two required fields filled in and nothing from ``.env``."""
    defaults: dict[str, Any] = {
        "postgres_password": SecretStr("localdev"),
        "sec_contact_email": "ops@whalewatch.io",
        "environment": "test",
    }
    return Settings(_env_file=None, **{**defaults, **overrides})


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
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
