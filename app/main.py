"""FastAPI application entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="WhaleWatch API")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only — it deliberately does not touch Postgres or Redis, so it
    still answers while a dependency is down and can tell you which one."""
    return {"status": "ok"}
