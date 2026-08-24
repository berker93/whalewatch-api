# whalewatch-api

## Local development

Everything runs in Docker — Postgres 16 (with `pg_trgm`), Redis 7, and the API
itself, so what you run locally is what ships.

```bash
cp .env.example .env
# Set SEC_CONTACT_EMAIL in .env — the app will not start without it.
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

### Configuration

All configuration lives in one validated object,
[`Settings`](app/core/config.py), reached through `get_settings()`:

```python
from app.core.config import get_settings

settings = get_settings()  # cached; one instance per process
```

Nothing else in this codebase reads `os.environ`. A scattered `getenv` has no
type, no discoverable default and no failure until the line that needs it runs —
which for a Celery task is 2am. `Settings` is built at import of
[`app/main.py`](app/main.py), so a missing or malformed variable stops uvicorn
immediately with a `ValidationError` naming the field.

Every variable is documented in [.env.example](.env.example), which is tracked;
`.env` is gitignored. Real environment variables take precedence over `.env`, so
compose injects config in dev and a secret manager can inject it in production
without a code change.

Three things worth knowing:

- **`SEC_CONTACT_EMAIL` is required and has no default.** SEC's fair-access
  policy wants a real contact address in the User-Agent of every EDGAR request
  and throttles traffic that omits or fakes one. A default would be a
  plausible-looking value that gets us blocked in production, so the app refuses
  to boot instead. `settings.sec_user_agent` derives the header from it.
- **Secrets are `SecretStr`.** `postgres_password` does not appear in `repr()`,
  `str()` or `model_dump()`, so it cannot ride along in a traceback or a
  structured log line. Read it deliberately with `.get_secret_value()`.
- **The database URL is assembled, not pasted.** `POSTGRES_HOST/PORT/USER/
  PASSWORD/DB` are the source of truth; `settings.database_url` and
  `settings.test_database_url` build the DSN from them, percent-encoding the
  credentials so a rotated password containing `@` or `/` cannot corrupt the
  host portion. The same variables configure the `db` container in
  [docker-compose.yml](docker-compose.yml), so the credentials Postgres is
  created with and the ones the app connects with cannot drift apart.

```bash
uv run pytest tests/test_config.py   # the rules above, as tests
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
`POSTGRES_HOST`/`POSTGRES_PORT` stay `db`/`5432` on the compose network
regardless, and that is what `settings.database_url` is built from.

The compose project is pinned to `name: whalewatch`, so containers, the network
and the volume are all `whalewatch*` and can never collide with another
project's resources.
