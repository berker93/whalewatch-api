"""Liveness and readiness endpoints.

Two endpoints because an orchestrator asks two different questions and takes two
very different actions on the answer:

``/health`` — *is this process alive?* A failure here gets the container killed
and restarted, so it must not depend on anything a restart cannot fix. It does
no I/O at all: if Postgres is down, restarting the API does not bring it back,
it just adds a crash-loop to the incident.

``/ready`` — *should traffic go to this instance?* A failure here only removes
the pod from the load balancer, which is exactly right while a dependency is
unreachable, and reverses itself the moment the dependency returns.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import EngineDep, RedisDep, SettingsDep
from app.core.logging import get_logger
from app.core.version import VERSION

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

# A readiness probe that hangs is worse than one that fails: the orchestrator's
# own probe timeout eventually fires, but until it does the instance is neither
# in nor out of rotation. Two seconds is below any sane probe timeout, so the
# answer is always ours to give.
CHECK_TIMEOUT_SECONDS = 2.0


class HealthResponse(BaseModel):
    """Liveness answer, plus the two things you want when a deploy looks wrong."""

    status: Literal["ok"] = "ok"
    version: str = Field(examples=["0.1.0"])
    git_sha: str = Field(examples=["9f2c1a0"])


class ReadinessResponse(BaseModel):
    """``checks`` maps dependency name to ``"ok"`` or ``"error: <reason>"``."""

    status: Literal["ok", "degraded"]
    checks: dict[str, str] = Field(examples=[{"postgres": "ok", "redis": "error: timeout"}])


async def _run_check(name: str, probe: Callable[[], Awaitable[Any]]) -> str:
    """Run one dependency probe under a hard deadline, never raising."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await probe()
    except TimeoutError:
        logger.warning(
            "readiness.check_timeout", dependency=name, timeout_s=CHECK_TIMEOUT_SECONDS
        )
        return "error: timeout"
    except Exception as exc:
        # The exception type, not str(exc): a connection error from asyncpg
        # carries the DSN, and /ready is unauthenticated. The full traceback goes
        # to the logs, where it is already inside the trust boundary.
        logger.warning("readiness.check_failed", dependency=name, exc_info=exc)
        return f"error: {type(exc).__name__}"
    return "ok"


async def _ping_postgres(engine: AsyncEngine) -> None:
    """Take a pooled connection and round-trip a statement.

    ``SELECT 1`` on a real connection, rather than inspecting pool counters:
    what readiness needs to know is that this process can still get a working
    connection, which a pool full of stale sockets will happily lie about.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def health(settings: SettingsDep) -> HealthResponse:
    """Answer without touching Postgres or Redis, so this stays 200 while a
    dependency is down and ``/ready`` is the endpoint that says which one."""
    return HealthResponse(version=VERSION, git_sha=settings.git_sha)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "At least one dependency is unreachable.",
        }
    },
)
async def ready(response: Response, engine: EngineDep, redis: RedisDep) -> ReadinessResponse:
    """Check every dependency and report all of them.

    Concurrently and without short-circuiting: when both Postgres and Redis are
    down, one probe telling you about both is the difference between one page
    and two. The body is the same shape on success and failure — only the status
    code differs — so a client never has to branch on which it got.
    """
    postgres_status, redis_status = await asyncio.gather(
        _run_check("postgres", lambda: _ping_postgres(engine)),
        _run_check("redis", redis.ping),
    )
    checks = {"postgres": postgres_status, "redis": redis_status}

    healthy = all(result == "ok" for result in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(status="ok" if healthy else "degraded", checks=checks)
