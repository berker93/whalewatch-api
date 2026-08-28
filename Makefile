# WhaleWatch — developer entrypoints. `make` on its own lists them.
#
# Anything that touches the database runs *inside* compose, because POSTGRES_HOST
# is `db` — a name that only resolves on the compose network. Running alembic
# from your Mac would mean overriding POSTGRES_HOST *and* POSTGRES_PORT to reach
# the same server, and getting that wrong migrates something else. The host-side
# targets (test, lint, fmt) never open a socket to it, so they run under uv
# against the local venv.

# Read the same file compose reads, so `make psql` cannot land on a different
# database than the app connects to. The leading dash tolerates its absence on a
# fresh clone, before `cp .env.example .env`; the ?= defaults below cover that.
-include .env

POSTGRES_USER ?= whalewatch
POSTGRES_DB ?= whalewatch

COMPOSE := docker compose

# `make logs s=db` follows one service; bare `make logs` follows all of them.
s ?=

.DEFAULT_GOAL := help
.PHONY: help up down build ps logs shell psql cli test lint fmt check migrate revision reset-db

help:  ## List the targets in this file
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | sed -E 's/:[^#]*## /|/' | awk -F'|' '{printf "  %-9s %s\n", $$1, $$2}'

# --- stack -------------------------------------------------------------------

up:  ## Start db, redis and the API in the background
	$(COMPOSE) up -d

down:  ## Stop the stack, keeping the pgdata volume
	$(COMPOSE) down

build:  ## Rebuild the api image — only needed when pyproject.toml or uv.lock change
	$(COMPOSE) up -d --build

ps:  ## Show container status and health
	$(COMPOSE) ps

logs:  ## Follow logs; `make logs s=db` for one service
	$(COMPOSE) logs -f --tail=100 $(s)

shell:  ## Open a shell in the api container
	$(COMPOSE) exec api bash

psql:  ## Open psql on the dev database
	$(COMPOSE) exec db psql -U $(POSTGRES_USER) $(POSTGRES_DB)

# Inside compose for the same reason migrate is: the CLI writes to the database,
# and POSTGRES_HOST is a name only the compose network resolves. Quote the whole
# verb — `c="ingest-filing ACC --cik N"` — because make splits on spaces and the
# unquoted form would pass only the first word.
cli:  ## Run a CLI verb in the api container: make cli c="ingest-filing 0001067983-24-000011 --cik 1067983"
	@test -n "$(c)" || { echo 'usage: make cli c="ingest-filing ACCESSION_NO --cik CIK"' >&2; exit 1; }
	$(COMPOSE) exec api uv run python -m app.cli $(c)

# --- code --------------------------------------------------------------------

test:  ## Run the whole suite; the integration half needs a Docker daemon
	uv run pytest -q

lint:  ## ruff check + mypy --strict, no writes
	uv run ruff check .
	uv run mypy .

fmt:  ## Format, and apply ruff's safe fixes
	uv run ruff format .
	uv run ruff check --fix .

check: lint test  ## Lint then test — what CI runs

# --- migrations --------------------------------------------------------------

migrate:  ## Apply every pending migration
	$(COMPOSE) exec api uv run alembic upgrade head

revision:  ## Autogenerate a draft migration: make revision m="add filings"
	@test -n "$(m)" || { echo 'usage: make revision m="add filings"' >&2; exit 1; }
	$(COMPOSE) exec api uv run alembic revision --autogenerate -m "$(m)"

# DESTRUCTIVE. `down -v` deletes the whalewatch_pgdata volume — every row you have
# ingested — and nothing else; no other compose project's volumes are in reach.
# This is the fast way back to a known-good schema when a backfill has written
# half-parsed rows, and it is the only way to pick up an edit to
# scripts/init-db.sql, which postgres runs once per volume and never again.
#
# Two recipe lines rather than one `&&` chain, and not by accident: make runs any
# line containing $(MAKE) even under `make -n`, so a single-line version deletes
# the volume during what you thought was a dry run.
reset-db:  ## DESTRUCTIVE: drop the volume, recreate the stack, migrate to head
	$(COMPOSE) down -v && $(COMPOSE) up -d && sleep 5
	$(MAKE) migrate
