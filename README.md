# whalewatch-api

**A read-only API over two SEC disclosure streams: quarterly institutional
holdings (Form 13F) and insider transactions (Forms 3/4/5).** It crawls EDGAR,
archives and parses the XML, resolves CUSIPs to tickers, and serves the part
nobody gets for free — what a filer holds, what changed since last quarter, who
is accumulating a given stock, and which insiders bought with their own money.

Both feeds are public and both are close to unusable in their native form. A 13F
is a CUSIP list with dollar values and no tickers and no deltas. A Form 4 is
transaction-code soup where a routine tax withholding looks exactly like a
director dumping stock. The product is the normalisation, the joins, and the
diffs.

FastAPI, Postgres 16, Redis, Celery. Async throughout, and everything runs in
Docker.

## Where the docs live

| Document | What is in it |
| --- | --- |
| [Product spec](docs/product-spec.md) | What the API answers, the endpoint surface, non-goals, the epic roadmap, the domain glossary |
| [Data model](docs/data-model.md) | Tables, natural keys, the raw → normalised → derived split, and the invariants worth a constraint |
| [Ingestion spec](docs/ingestion-spec.md) | EDGAR sources, rate limits, the 13F and Form 4 parsers, enrichment, and every way the numbers can be quietly wrong |

The rest of this file is how to run it; the specs are what it is.

## Prerequisites

- **Docker**, with Compose v2 — `docker compose version` should print 2.x.
  Postgres, Redis and the API all run in containers, so nothing has to be
  installed on your Mac to serve a request.
- **[uv](https://docs.astral.sh/uv/)** — `brew install uv`. Runs the tests, the
  linter and the formatter on the host, and owns `uv.lock`. It fetches Python
  3.12 itself per [.python-version](.python-version); no system Python needed.
- **make** — ships with the Xcode command line tools.
- **A real email address**, for `SEC_CONTACT_EMAIL`. Step 2 below explains why it
  cannot be a placeholder.

About 2GB of disk for the images and the Postgres volume.

## Setup

Five commands from a fresh clone:

```bash
cp .env.example .env              # 1. working defaults for everything but one field
$EDITOR .env                      # 2. set SEC_CONTACT_EMAIL=you@example.com
make up                           # 3. build the image, start db + redis + api
make migrate                      # 4. alembic upgrade head
curl localhost:8000/health        # 5. {"status":"ok","version":"0.1.0",...}
```

**Step 2 is not optional and a placeholder will not do.** SEC's fair-access
policy requires a real, monitored contact address in the User-Agent of every
EDGAR request, and throttles or blocks traffic that omits or fakes one. Rather
than ship a default that would get us blocked in production, the app refuses to
start: if `make up` leaves the `api` container restarting, `make logs s=api`
shows a pydantic `ValidationError` naming `sec_contact_email`, and this is why.

Then `make test`, and read the specs above.

## Everyday commands

`make` on its own prints this list.

| Command | |
| --- | --- |
| `make up` / `make down` | start / stop the stack |
| `make build` | rebuild the api image — only needed when `pyproject.toml` or `uv.lock` change |
| `make ps` | container status and health |
| `make logs` | follow every service; `make logs s=db` for one |
| `make shell` | a shell inside the api container |
| `make psql` | psql on the dev database |
| `make cli c="ingest-filing ..."` | run a CLI verb in the api container — see [The CLI](#the-cli) |
| `make test` | the whole pytest suite |
| `make lint` / `make fmt` | ruff check + mypy --strict / ruff format + safe fixes |
| `make check` | lint, then test — what CI runs |
| `make migrate` | `alembic upgrade head` |
| `make revision m="add filings"` | autogenerate a draft migration |
| `make reset-db` | **destructive** — drop the volume, recreate the stack, migrate |

Anything touching the database runs inside compose, because `POSTGRES_HOST` is
`db` — a name that only resolves on the compose network. `test`, `lint` and `fmt`
open no socket to it, so they run on the host under `uv`.

`make reset-db` deletes the `whalewatch_pgdata` volume and everything in it. It
will earn its keep during Epic 3, when a backfill bug means you want a clean
slate rather than an archaeology project — and it is the only way to pick up an
edit to [scripts/init-db.sql](scripts/init-db.sql), which Postgres runs once per
volume and never again. Nothing outside this project is in reach; see
[Databases](#databases).

## The CLI

[app/cli.py](app/cli.py) is the operational interface: the same ingestion code
Celery runs on a schedule, wrapped in a verb and a summary a person can read. It
exists so that a quarter that came out wrong can be re-run by hand, with no
broker in the loop and without anyone writing a throwaway script at the point in
the incident where throwaway scripts are least trustworthy.

```bash
make cli c="ingest-filing 0001067983-24-000011 --cik 1067983"
```

or, from a shell that can reach the database directly:

```bash
uv run python -m app.cli ingest-filing 0001067983-24-000011 --cik 1067983
```

### `ingest-filing ACCESSION_NO [--cik] [--force] [--dry-run]`

Fetches, parses and loads one 13F. It looks the accession number up in EDGAR's
submissions index for the CIK, lists the filing directory, identifies the cover
page and the information table, and writes the result in one transaction.

```
0001193125-26-352200  13F-HR
  filer       Berkshire Hathaway Inc  (CIK 0001067983)
  period      2026-06-30  (2026Q2)
  filed       2026-08-14 20:05:04+00:00  (values x1)
  documents   primary_doc.xml + 56757.xml
  rows        89 rows parsed, 89 declared, 29 positions loaded, 60 folded into another line
  value       $299,253,556,246.00
  status      ok
  written     filing #1, 29 holdings, 29 new securities
```

| Flag | |
| --- | --- |
| `--cik` | Which filer's archive the filing lives under. Optional only for a filing already in the database, whose CIK is then already known — see below |
| `--force` | Re-fetch and re-load a filing that is already loaded |
| `--dry-run` | Fetch, parse and print the same summary. Write nothing |

**`--cik` is not optional as often as you would like.** EDGAR's archive path is
`/Archives/edgar/data/<cik>/<accession>/`, and the CIK in it is the *filer's* —
not the ten digits at the front of the accession number, which identify whoever
transmitted the submission and are usually a filing agent. Berkshire's own 13F
lives under `data/1067983/` with an accession number beginning `0001193125`, and
the path built from the latter does not exist. So the CIK has to come from
somewhere, and the command will take it from an existing `filing` row — which is
the common case, because discovery writes that row before anything fetches the
documents — or from this flag.

**Re-running is safe and cheap.** A filing that is already loaded is left alone
and reported as such, at exit 0, without a single EDGAR request; `--force`
re-fetches it. Either way the database ends up with one filing and one set of
holdings, because [the loader](app/ingestion/loaders/filing.py) upserts on the
accession number and replaces the holdings wholesale.

**Exit codes.** Zero when the filing is loaded, and zero when it was already
loaded — "already done" has to be a success or a resumed backfill fails on every
filing it had finished. Non-zero for anything else, with a one-line reason on
stderr. The summary goes to stdout and the operational log to stderr, so
`... > report.txt` keeps the two apart.

**What the summary will not let you misread.** `0 holdings` means two opposite
things — a `13F-NT`, which reports no positions by design, and a filing whose
CIK is not yet a known filer, whose positions are waiting on `holding.filer_id`
being `NOT NULL`. The second prints `DEFERRED` and tells you to re-run it once
the filer is resolved. Likewise a `suspect` status means every guard's finding
is printed here and stored on `filing.parse_notes`; the filing is still loaded,
because withholding a portfolio that is 99% right leaves a hole shaped exactly
like a manager who filed nothing.

## The API

One read endpoint so far, and it is the one everything else gets debugged
through.

### `GET /filings/{accession_no}`

A filing, its provenance, and every position it reports — largest first.

```bash
curl localhost:8000/filings/0001067983-24-000011
curl localhost:8000/filings/000106798324000011              # same filing
curl "localhost:8000/filings/0001067983-24-000011?include_options=false"
```

```json
{
  "accession_no": "0001067983-24-000011",
  "cik": "0001067983",
  "form_type": "13F-HR",
  "period_of_report": "2024-03-31",
  "quarter": "2024Q1",
  "filer_name": "Berkshire Hathaway Inc",
  "value_multiplier": 1,
  "parse_status": "suspect",
  "parse_notes": [
    { "kind": "entry_count", "detail": "parsed 3 rows, cover page declares 99" }
  ],
  "holdings": [
    {
      "cusip": "037833100",
      "issuer_name": "APPLE INC",
      "ticker": null,
      "value_usd": "2040000000.00",
      "shares": "12000000.0000",
      "sshprnamt_type": "SH",
      "put_call": null
    }
  ]
}
```

Five things about that response are decisions rather than defaults:

- **Every `numeric` is a JSON string.** `value_usd` and `shares` are
  `numeric(20,2)` and `numeric(20,4)`; a JSON number is an IEEE 754 double at
  the far end of every client, which carries fewer significant digits than
  either column and cannot represent the difference between `1000` and
  `1000.00` at all. Strings round-trip exactly, and a client that wants
  arithmetic has to parse into its own decimal type deliberately.
- **`value_multiplier` and `parse_status` are in the response on purpose.** They
  are what ingestion *decided*: which units the filing's own `value` column used
  (1000 before the 2023-01-03 cutover, 1 after), and whether any normalisation
  guard fired. A portfolio that is out by 1000x looks entirely normal — every
  position is wrong by the same factor — so the field that distinguishes "the
  filing said thousands" from "we multiplied when we should not have" has to be
  visible from outside the container. A `suspect` filing is returned, not
  withheld, with `parse_notes` saying which rows provoked it.
- **Both spellings of the accession number work.** Dashed as EDGAR's indexes
  print it, undashed as its archive URLs do. The path parameter is normalised
  before anything is looked up, so the undashed form cannot produce a 404 for a
  filing that is sitting in the table. A string that is not an accession number
  at all is a 422 naming the shape expected — a different answer from 404,
  because it sends you somewhere different.
- **404 says what to do about it.** On this endpoint "not found" almost always
  means "not ingested yet", and the reader is usually the person who can fix
  that, so the message carries the `ingest-filing` command that would.
- **`include_options=false` filters on `put_call IS NOT NULL`, not on the
  security.** An option line's `value_usd` is the notional value of the
  underlying rather than a premium, so a total that includes it is inflated by
  the whole exposure — but the option and the underlying position share a CUSIP,
  and a filter that worked by security would take the real holding with it.

An empty `holdings` list means one of three things, and the rest of the response
says which: a `13F-NT`, which reports no positions by design; a `parse_status` of
`failed`, with `parse_error` saying why; or a filing whose `filer_id` is still
null, whose positions are waiting on a CIK being resolved to a filer.

## Data sources and limitations

Write these down once so you are not re-deriving them from a 13F XML at midnight.

### Where the data comes from

| Source | Auth | What we take |
| --- | --- | --- |
| `data.sec.gov/submissions/CIK##########.json` | none | every filing a known CIK has made — how we find 13Fs |
| EDGAR daily index | none | everything filed on one day — how we find Form 4s, whose filers we cannot know in advance |
| EDGAR archives | none | the documents themselves |
| OpenFIGI | free API key | CUSIP → ticker, because 13F reports neither ticker nor name we can trust |

All public, all free, hard-capped at **10 requests/second across all of sec.gov**
by SEC's fair-access policy. `SEC_RATE_LIMIT_PER_SECOND` defaults to 8 and
`Settings` refuses anything above 10. There is no vendor to fall back on when a
filing is ambiguous: the filing is the only authority.

### Two things that will bite you

**1. 13F holdings are between 45 and 135 days old. Always.**

Managers file within 45 days of quarter end:

| Period ends | Due |
| --- | --- |
| Mar 31 | May 15 |
| Jun 30 | Aug 14 |
| Sep 30 | Nov 14 |
| Dec 31 | Feb 14 |

So on May 14 the newest holdings anyone has are December 31's. **Nothing in this
API is ever labelled "current"**: every 13F-derived payload states its `period`,
and no endpoint quietly defaults "latest" in a way that hides which period the
caller actually received.

The lag has a second edge. A period is not final once you have built it —
amendments (`13F-HR/A`) restate or extend it years later, and positions filed
under confidential treatment appear afterwards dated to the original quarter.
Ingestion is therefore an upsert on natural keys, and everything derived from
holdings is recomputable rather than incrementally patched.

**2. 13F dollar values changed units on 2023-01-03.**

The information table's `value` field was reported in **thousands of dollars**
for filings submitted before 2023-01-03, and in **whole dollars** from then on. A
$1.2B position reads `1200000` on one side of that line and `1200000000` on the
other.

Getting it wrong is a **1000× error that does not announce itself**. Every filer
in a mis-parsed quarter is wrong by the same factor, so rankings, percentages and
quarter-over-quarter shapes all look perfectly normal; it surfaces months later
as "why does this fund have $40 million in it".

Three rules, and the middle one is the one people get backwards:

- **Normalise at parse time.** `holding.value_usd` is whole dollars for every row,
  always. Storing the filing's own units and converting on read means every
  consumer has to know about the cutover, and one of them will not.
- **Key off the filing date, not the period.** The convention follows the
  submission. An amendment filed in 2024 for a 2019 period is in **whole
  dollars**, even though the original filing for that same period was in
  thousands — so a `period < 2023` test gets amendments exactly inverted.
- **Verify, do not assume.** `app/ingestion/normalisation.py` runs three guards
  over every normalised filing — the implied share price, the row count against
  the cover page's `tableEntryTotal`, and the summed value against its
  `tableValueTotal` — and records what fires in `filing.parse_status` and
  `filing.parse_notes`. The price check is the one that matters most: it is the
  only one that reaches outside the document, so it catches a filer who kept
  using the old convention as well as a parser that did. Guards **flag, they do
  not reject** — a suspect filing still loads, because the alternative is a hole
  that looks exactly like a manager who filed nothing. Fixtures exist for both
  sides of the boundary *and* for a post-cutover amendment of a pre-cutover
  period; that third case is the one that regresses.

### Also true, and also enough to make a number wrong

- **13F is long-only US equity.** No shorts, no cash, no bonds beyond convertibles,
  no foreign listings, no commodities or FX. A fund that looks "100% tech" may
  have an invisible book many times the size of the one it discloses.
- **Options are notional.** A `putCall` line's value is the value of the
  underlying, not the premium; summing it with common stock inflates a portfolio
  by the whole underlying exposure.
- **`PRN` is not `SH`.** Convertibles report a principal amount, not a share
  count. Adding the two adds dollars to shares.
- **Combination reports double-count.** Affiliated managers can each file the same
  position; aggregating without honouring the cover page's report type counts it
  twice.
- **Splits.** Share counts are as reported at the time. Comparing across Apple's
  4-for-1 split unadjusted shows every holder quadrupling their stake.
- **Form 4 codes are not sentiment.** `P` and `S` are open-market purchases and
  sales; `M` is an option exercise, `F` is tax withholding, `A` a grant, `G` a
  gift. And a sale under a 10b5-1 plan was decided months before its date.

Each of these is worked through in the
[ingestion spec](docs/ingestion-spec.md#the-two-traps).

## Local development

Everything runs in Docker — Postgres 16 (with `pg_trgm`), Redis 7, and the API
itself, so what you run locally is what ships.

`docker compose up` builds the `dev` stage of the [Dockerfile](Dockerfile), waits
for `db` and `redis` to report healthy, then starts uvicorn with `--reload`. The
repo is bind-mounted at `/src`, so saving a file restarts the app in about a
second — no rebuild. Rebuild only when `pyproject.toml` or `uv.lock` change:

```bash
make build
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

### Logging

Every log line is a structured event, rendered as one JSON object per line in
`staging` and `production` and as a coloured console line everywhere else. The
renderer is the only thing that changes between the two — the fields are
identical, so what you read locally is what the aggregator will index.

```python
from app.core.logging import get_logger

log = get_logger(__name__)
log.info("filing.parsed", accession_no=accession_no, cik=cik, rows=len(holdings))
```

Do not format values into the message. A backfill of 2,000 filings that dies on
number 1,347 is only debuggable if `grep 0001234567-24-000123` returns *every*
line that touched that filing, and free text cannot promise that because the
number lands in a different sentence in each message that mentions it.

**Correlation.** [`RequestContextMiddleware`](app/api/middleware.py) gives every
request a `request_id`, taken from an inbound `X-Request-ID` when there is one so
a trace started at the edge proxy keeps a single id across every hop, and echoes
it in the response header. It is bound to the logging context, so anything logged
anywhere inside that request carries it without being passed one:

```
{"event": "request_completed", "method": "GET", "path": "/ready", "status": 200,
 "duration_ms": 3.21, "request_id": "d5ba98bedf344d1c93b534184024906d", ...}
```

Give a customer the id from the header and their whole request is one grep. Bind
the same way in batch work, and one run is one grep:

```python
import structlog

structlog.contextvars.bind_contextvars(job_name="backfill_13f", run_id=run_id)
```

`contextvars` rather than thread-locals, deliberately: a thread-local is shared
by every coroutine the event loop interleaves on that thread, so two concurrent
requests would overwrite each other's `request_id`. A `ContextVar` is copied into
each task and survives `await`.

**Vocabulary.** Queryability comes from consistent keys, not from any one call
site being clever. Use these names, add to them, but never spell one of them a
second way — no `accession`, no `accessionNumber`:

| Key | Meaning |
| --- | --- |
| `accession_no` | EDGAR accession number, dashed: `0001234567-24-000123` |
| `cik` | Central Index Key, zero-padded 10-char string, never an int |
| `filer_slug` | Our stable slug for a filer, e.g. `berkshire-hathaway` |
| `period` | Reporting period the data belongs to, `YYYY-MM-DD` |
| `job_name` | Name of the batch job, e.g. `backfill_13f` |
| `run_id` | One execution of a job; every line from that run shares it |
| `request_id` | One HTTP request; bound by the middleware |

**One stream.** uvicorn, SQLAlchemy and Alembic log through the standard library
and know nothing about structlog. Their records are routed through the same
processors and the same handler, so they arrive with the same `timestamp`,
`level` and bound `request_id` as ours instead of forming a second, differently
shaped stream that whatever ships these logs has to parse twice. uvicorn's own
access log is switched off, because the middleware already emits one access line
per request and uvicorn's is a strictly worse duplicate.

`LOG_LEVEL` sets the threshold for all of it. Note that `DEBUG` is enough to make
SQLAlchemy log every statement it emits.

```bash
uv run pytest tests/test_logging.py tests/test_request_context.py
```

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
poking at by hand. (Pytest does *not* use it — the integration suite starts its
own container; see below.) That script runs **once**, on volume creation; to pick up edits to it, wipe and
recreate:

```bash
make reset-db
```

`down -v` deletes the `whalewatch_pgdata` volume and nothing else — no other
Docker project's data is in reach, because the compose project is pinned to
`name: whalewatch`.

```bash
make psql                                                     # dev data
docker compose exec db psql -U whalewatch -d whalewatch_test  # test data
```

### Tests

```bash
make test                            # everything, quietly — this is the one to run
uv run pytest                        # everything, verbosely
uv run pytest -m "not integration"   # everything that does not need Docker
uv run pytest -m integration         # only the tests that talk to Postgres
```

These run on the host, not in the container: nothing in the suite connects to
the compose Postgres, so `uv` and the local venv are all they need.

Two suites, in one command. `tests/` is the unit suite: it builds throwaway apps
from `create_app`, stubs its dependencies, and opens no sockets.
`tests/integration/` runs against a real **PostgreSQL 16** in a container that
[testcontainers](https://testcontainers-python.readthedocs.io) starts once per
run and throws away at the end — so a Docker daemon has to be up, which is what
the `integration` marker exists to let you opt out of.

There is no SQLite mode and there will not be one. The queries in this project
are Postgres — aggregate `FILTER`, window functions, `gin_trgm_ops` indexes,
generated columns, partitions, materialised views — and none of it parses in
SQLite. A suite on SQLite would test a different program than the one that
ships, and would go green on exactly the query that fails in production.

The container's schema is built by **`alembic upgrade head`**, not by
`create_all`. `Base.metadata` is the schema we think we have; the migration
chain is the one production will actually have, so running it is what turns a
broken or drifted migration into a red test rather than a bad deploy.
[alembic/env.py](alembic/env.py) takes the connection to run it on through
`config.attributes["connection"]` — the same hook is why the test database can
be a container that did not exist when `Settings` was built.

Each test is wrapped in a transaction that is rolled back:

```python
async def test_something(db_session: AsyncSession, client: AsyncClient) -> None: ...
```

`db_session` opens a connection, begins a transaction on it, and binds the
session to that connection — so a `commit()` in the code under test releases a
savepoint *inside* that transaction and disappears when the fixture rolls back.
No TRUNCATE between tests, no database per test, and no test that can be made to
pass or fail by what ran before it;
[tests/integration/test_rollback_isolation.py](tests/integration/test_rollback_isolation.py)
asserts exactly that. `client` is an `httpx.AsyncClient` against the app with
`get_session` overridden to hand every request that same session, so you can POST
through the API and read the result back through the session.

The whole suite is a few seconds on a warm image; the container start is the only
slow part of it, and it happens once.

### Migrations

Alembic, async template, run through `uv run` so it uses the project venv:

```bash
make migrate                                           # apply everything pending
docker compose exec api uv run alembic downgrade -1    # undo the last one
docker compose exec api uv run alembic current         # what is applied
docker compose exec api uv run alembic history --verbose   # the chain
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
make revision m="add filings"
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
docker compose exec api uv run alembic upgrade head --sql
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
