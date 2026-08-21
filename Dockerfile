# syntax=docker/dockerfile:1

# The prod stage lands in CLOUD-1; dev is deliberately the only target for now.
FROM python:3.12-slim AS dev

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /src

ENV UV_LINK_MODE=copy \
    # Keeping the venv out of /src is what makes the bind mount safe: a mount at
    # /src would otherwise hide a /src/.venv built during the image build.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    # uv resolves against the lockfile the bind mount provides; never silently
    # re-resolve at container start.
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

# Dependency layer, cached until pyproject.toml or uv.lock actually change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Baked in so the image runs standalone; the compose bind mount shadows it in dev.
COPY . .
