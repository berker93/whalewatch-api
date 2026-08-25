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

### Health and readiness

Two endpoints, because an orchestrator asks two different questions and does two
different things with the answer.

```bash
curl localhost:8000/health   # is the process alive?
curl localhost:8000/ready    # should traffic go to it?
```

`/health` does **no I/O** and always returns 200 while the process is running:

```json
{ "status": "ok", "version": "0.1.0", "git_sha": "9f2c1a0" }
```

A failing liveness probe gets the container killed and restarted, so it must not
depend on anything a restart cannot fix. If Postgres is down, restarting the API
does not bring it back — it just adds a crash-loop to the incident, and takes
away the endpoint that could have told you which dependency was broken.

`/ready` checks Postgres (`SELECT 1` on a pooled connection) and Redis (`PING`),
concurrently, each under a hard 2s deadline, and reports **all** of them:

```json
{ "status": "degraded", "checks": { "postgres": "ok", "redis": "error: timeout" } }
```

Same shape either way; the status code is what differs — 200 when everything is
`ok`, 503 otherwise, which takes the instance out of the load balancer and puts
it back by itself when the dependency returns. Checks do not short-circuit, so
one probe tells you about both outages instead of revealing the second only
after you have fixed the first. Failure detail is the exception *type*, never
its message: `/ready` is unauthenticated and asyncpg puts the whole DSN in its
connection errors. The full traceback goes to the logs.

The 2s deadline is the point of the whole endpoint. A readiness probe that hangs
leaves the instance neither in nor out of rotation until the orchestrator's own
timeout fires; one that fails is a decision.

`version` comes from `pyproject.toml`, and `git_sha` from `GIT_SHA`, stamped into
the image at build time so "which commit is actually serving?" is answerable from
outside the container:

```bash
docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
```

Unstamped, it reports `unknown` — a local uvicorn has no build, and a liveness
endpoint should not refuse to boot over a cosmetic field.

Interactive docs live at [/docs](http://localhost:8000/docs) and the schema at
`/openapi.json` — in every environment except `production`, where all three
(`/docs`, `/redoc`, `/openapi.json`) return 404. Hiding the HTML page while still
serving the schema would publish the same map of the API in a less convenient
format.

### The app factory

`app.main:app` — what uvicorn and Celery import — is just `create_app(get_settings())`.
The app itself is built by a factory that takes its settings as an argument:

```python
from app.main import create_app
from app.api.deps import get_engine

app = create_app(make_settings(environment="production"))
app.dependency_overrides[get_engine] = lambda: stub_engine
```

A module-level app is configured by whatever the environment held at import, so
a test that wants a different one has to mutate global state and remember to put
it back. Connection pools are created in the `lifespan`, not at import — an
engine built at import binds its pool to whichever event loop imported the
module, and nothing ever closes it. The lifespan disposes both pools in a
`finally`, so a crash on the way down still returns the connections.

Neither pool connects at startup. The app boots with Postgres unreachable and
says so through `/ready`, rather than dying before it can serve the probe that
would explain why.

```bash
uv run pytest tests/test_health.py
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

### Migrations

Alembic, async template, run through `uv run` so it uses the project venv:

```bash
docker compose exec api alembic upgrade head          # apply
docker compose exec api alembic downgrade -1          # undo the last one
docker compose exec api alembic current               # what is applied
docker compose exec api alembic history --verbose     # the chain
```

Run it inside the `api` container, not on your Mac. `POSTGRES_HOST` is `db`, a
name that only resolves on the compose network; from the host you would have to
override it *and* `POSTGRES_PORT` (5433, per the table above) to reach the same
database, and getting that wrong migrates something else.

There is no `sqlalchemy.url` in [alembic.ini](alembic.ini). `env.py` builds it
from the same [`Settings`](app/core/config.py) the app connects with, so "which
database" has one definition, rotating the password touches one place, and no
credential is committed. `target_metadata` is `Base.metadata`, and `env.py`
imports `app.db.models` for the side effect of populating it — a model that is
not imported there is invisible to autogenerate, and autogenerate will propose
*dropping* its table.

Migration filenames lead with the date
(`20260825_0001_baseline.py`), so `ls alembic/versions` reads chronologically
and a reviewer can see how old a pending migration is. The revision id is still
in the name, because that is what `down_revision` and `alembic history` refer
to: the date orders them for humans, the id chains them for Alembic.

#### Autogenerate is a draft

```bash
docker compose exec api alembic revision --autogenerate -m "add filings"
```

Then open the file and rewrite it. Autogenerate diffs SQLAlchemy metadata
against the live schema, and most of what this project needs is outside what
that diff can see:

- **Generated columns, partitions, materialised views** — not modelled, so not
  detected. They are `op.execute()` and you write them by hand.
- **Native enums** — it emits `CREATE TYPE` on first use but will not notice a
  new label, and `ALTER TYPE ... ADD VALUE` cannot run inside a transaction
  block, which Alembic gives you by default.
- **Renames** — a renamed column is rendered as a drop plus an add. That is
  data loss, and the test suite stays green through it.

`compare_type` and `compare_server_default` are switched on in `env.py`. They
are off by default, and off is how a `varchar(20)` widened to `varchar(40)`, or
a `server_default` added to an existing column, becomes "no changes detected"
and a schema that has quietly diverged from the models. They make autogenerate
noisier — Postgres normalises defaults, so it occasionally proposes a no-op —
which is the right trade when every migration is read before it is committed.

To review one as DDL, or hand it to someone applying it in a window, render it
offline. No connection is opened:

```bash
docker compose exec api alembic upgrade head --sql
```

#### Downgrades are implemented, not `pass`

A migration you cannot reverse is a deploy you cannot roll back, and the moment
you need one is the moment you cannot test writing it. Where the reverse
genuinely loses data — a dropped column — say so in the docstring and restore
the structure anyway: an empty column beats a failed downgrade that strands the
database between two revisions.

`0001_baseline` is empty and is the one exception, because downgrading past the
root means "no schema at all", which an empty upgrade already leaves you at.

The rules are tests, not conventions:

```bash
uv run pytest tests/test_migrations.py
```

It asserts there is exactly one head (two means someone branched, and `upgrade
head` fails mid-deploy with "Multiple head revisions are present"), that every
file on disk is on the path from base to head, that filenames carry a real date
and sort in chain order, that both directions are defined, and that no revision
with a parent has a bare `pass` for a downgrade. It also runs `env.py` for real
in offline mode, so a broken import or an unset variable there fails in CI
rather than in a deploy. None of it needs a database.

New migrations are formatted and linted on the way out —
[alembic.ini](alembic.ini) runs `ruff check --fix` and `ruff format` as
post-write hooks, so a generated file does not land with unsorted imports and
100-column violations for you to fix by hand.

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
