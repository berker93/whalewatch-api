# whalewatch-api

## Local development

Everything runs in Docker — Postgres 16 (with `pg_trgm`), Redis 7, and the API
itself, so what you run locally is what ships.

```bash
cp .env.example .env
docker compose up -d
curl localhost:8000/health
```

`docker compose up` builds the `dev` stage of the [Dockerfile](Dockerfile), waits
for `db` and `redis` to report healthy, then starts uvicorn with `--reload`. The
repo is bind-mounted at `/src`, so saving a file restarts the app in about a
second — no rebuild. Rebuild only when `pyproject.toml` or `uv.lock` change:

```bash
docker compose up -d --build
```

### Databases

First creation of the `pgdata` volume runs [scripts/init-db.sql](scripts/init-db.sql),
which installs `pg_trgm` and creates a second database, `whalewatch_test`, for
pytest. That script runs **once**; to pick up edits to it, wipe and recreate:

```bash
docker compose down -v && docker compose up -d
```

`down -v` deletes the `whalewatch_pgdata` volume and nothing else — no other
Docker project's data is in reach.

```bash
docker compose exec db psql -U whalewatch -d whalewatch       # dev data
docker compose exec db psql -U whalewatch -d whalewatch_test  # test data
```

### Ports, and coexisting with other stacks

Ports *inside* the compose network are fixed and are what the app uses:
`db:5432`, `redis:6379`, `api:8000`. Only the host-side mapping is configurable,
via `.env`:

| Variable     | Default in `.env` | Host URL                 |
| ------------ | ----------------- | ------------------------ |
| `DB_PORT`    | `5433`            | `localhost:5433`         |
| `REDIS_PORT` | `6379`            | `localhost:6379`         |
| `API_PORT`   | `8000`            | `http://localhost:8000`  |

`DB_PORT` ships as **5433** because the `predictor-api` stack publishes its
Postgres on host 5432. If that stack is stopped, `DB_PORT=5432` works fine. Note
this only affects tools connecting from your Mac (psql, TablePlus, DBeaver);
`DATABASE_URL` points at `db:5432` on the compose network regardless.

The compose project is pinned to `name: whalewatch`, so containers, the network
and the volume are all `whalewatch*` and can never collide with another
project's resources.
