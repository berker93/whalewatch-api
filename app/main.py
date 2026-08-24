"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.core.config import get_settings

# At import, not inside a request handler. Building Settings validates the whole
# environment, so a missing SEC_CONTACT_EMAIL or an out-of-range rate limit stops
# uvicorn here with a ValidationError naming the field — rather than surfacing
# hours later inside the first Celery task that happened to need it.
settings = get_settings()

app = FastAPI(title=f"{settings.app_name} API", debug=settings.debug)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only — it deliberately does not touch Postgres or Redis, so it
    still answers while a dependency is down and can tell you which one."""
    return {"status": "ok"}
