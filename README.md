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
| [Query performance](docs/query-performance.md) | The API's ten queries under `EXPLAIN ANALYZE` on the full dataset, the indexes that changed because of it, and how to read a plan |

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
| `make explain a="--only stock_holders"` | the API's queries, timed and under `EXPLAIN (ANALYZE, BUFFERS)` — see [Query performance](docs/query-performance.md) |
| `make cli c="ingest-filing ..."` | run a CLI verb in the api container — see [The CLI](#the-cli) |
| `make reconcile` | check the published tables add up, on the dev database — see [`reconcile`](#reconcile---include-suspect---sample-n) |
| `make verify-investors` | check every investor CIK against EDGAR, on the host; needs `SEC_CONTACT_EMAIL` |
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

Every verb records its run in `ingestion_run` — when it started and finished,
how it ended, what it counted — and [`runs`](#runs---job-name---limit-n) lists
them.

### `discover-filings [--filer SLUG] [--since DATE | --all]`

Finds the work. For every tracked filer, or only `--filer`, it lists each of the
filer's CIKs in EDGAR's submissions index and takes the `13F-HR`s and
`13F-HR/A`s. It subtracts the ones already loaded and queues the rest in
`pending_filing`, where `ingest-filing` finds their CIK. Per filer it reports
what it found, how much of that is already ingested, and what is new:

```
discover-filings  13F-HR and 13F-HR/A, filed since 2021-09-30
  slug                       found  ingested     new
  berkshire-hathaway            21        20       1
  pershing-square               20         0      20  FAILED
  renaissance-technologies      20        20       0
  3 filers: 61 found, 40 already ingested, 21 new in pending_filing
  error       pershing-square CIK 0001336528: EDGAR has no submissions index for CIK 0001336528
```

| Flag | |
| --- | --- |
| `--filer` | One filer, by slug, instead of all of them |
| `--since` | Earliest filing date to look at. Defaults to five years ago |
| `--all` | Every filing EDGAR lists, however old. Not with `--since` |

**It is a set difference, so it heals itself.** Nothing records where the last
run stopped. A filing that failed to load weeks ago is still "new" on the next
run. A queued filing that is already loaded (say, by hand) is marked `done`.
Re-running is always safe. It never duplicates a queue row, and a row keeps its
`discovered_at`, `attempts` and `last_error`. "Loaded" means `parse_status` `ok`
or `suspect`, the same test `ingest-filing` uses to skip.

**Draining the queue** is [`backfill`](#backfill---filer-slug---since-date---concurrency-n---limit-n---force---no-refresh-views),
or `ingest-filing ACCESSION_NO` for one row, with no `--cik`. Either way each
attempt is written back to the row: `done` on a load, or `failed` with
`attempts` incremented and `last_error` set.

**Exit codes.** Non-zero if any CIK could not be listed. The filer is marked
`FAILED` and the error is printed, but the filer's other CIKs and every other
filer are still queued. A rate-limit block from EDGAR stops the run, because
every request after it would fail the same way. Filers finished before the block
are already committed.

### `backfill [--filer SLUG] [--since DATE] [--concurrency N] [--limit N] [--force] [--no-refresh-views]`

Drains the queue. It takes every filing in `pending_filing` and every 13F
already in `filing`, skips the loaded ones, and ingests the rest several at a
time. Each filing goes through `ingest-filing`'s steps in the same order:
archive, parse, load and publish. It prints one line per filing as it finishes, then a
summary:

```
backfill  every filer: 2103 filings, 1756 already loaded, 347 to ingest · 5 at a time
[1/347] berkshire-hathaway 2022Q3 · 41 rows · ok
[2/347] pershing-square 2022Q3 · FAILED · 0001193125-22-000123 · FilingDocumentsError: ...
...
backfill  done: succeeded 340 · skipped 1756 · failed 3 · suspect 4 · elapsed 3m12s
  failed      0001193125-22-000123  pershing-square 2022Q3  FilingDocumentsError: ...
```

| Flag | |
| --- | --- |
| `--filer` | One filer, by slug, instead of all of them |
| `--since` | Only filings filed on or after this date |
| `--concurrency` | Filings in flight at once, default 5, at most 15. They all share the one EDGAR rate limiter, so more than about 5 does not make it faster |
| `--limit` | Work on at most N filings, oldest first. Skipped filings do not count |
| `--force` | Also reprocess the loaded filings, from the raw store |
| `--no-refresh-views` | Do not refresh the materialised views at the end |

**Resuming is running it again.** The work is planned from the database before
any worker starts. A filing that is already loaded (`ok` or `suspect`) is
skipped there, with no EDGAR request. A run that died on filing 1,347 is resumed
by running the same command, and the first 1,346 cost one query.

**`--force` reprocesses from the raw store, with no EDGAR request.** A loaded
filing's archived documents are parsed again and reloaded. `filed_at` comes from
its `filing` row, because neither document carries it and it decides the units.
This is how a parser fix reaches filings already loaded: a reparse of local
bytes, not a recrawl. A filing loaded before the archive existed fails with a
message saying so. `ingest-filing ACCESSION_NO --force` fetches it again.
Filings that are not loaded are ingested from EDGAR as usual, with or without
`--force`. That includes one whose last parse failed, because the fix reaches
it on the next plain run anyway.

**One filing failing does not stop the run.** The failure is logged with its
traceback on stderr and recorded on the queue row. It is also printed in the
progress output and listed again under the summary. The one exception is a
rate-limit block from EDGAR, which stops the run. SEC blocks by IP, so every
filing after it would fail the same way.

**Ctrl-C finishes what is in flight.** The first Ctrl-C starts no new filings
and lets the ones already running load, then prints the summary with a
`not started` count. A second Ctrl-C abandons the in-flight filings. Their loads
roll back, and each document is either archived whole or not at all, so the
next run picks them up.

**The views are refreshed once, at the end.** Each filing is published as it
loads, but [`refresh-views`](#refresh-views---view-name---no-concurrent) runs
only after the last one, and only if anything loaded. Refreshing after each
filing would cost a full refresh per filing, and would publish a quarter
partway through with only some of its filers in it. A run that stopped short
still refreshes what it loaded. A second Ctrl-C during the refresh rolls the
refresh back and leaves the loads in place, so run `refresh-views` by hand.

**Exit codes.** 0 when everything planned was loaded or skipped. 1 if any
filing failed or EDGAR blocked the run. 130 if it was interrupted with nothing
failed. Every line from one run carries the same `run_id` in the log, and every
line about one filing carries its `accession_no`. The run's `ingestion_run` row
is `partial` whenever it exits 1 or 130, with each failed filing on a line of
its `error`.

### `ingest-filing ACCESSION_NO [--cik] [--force] [--dry-run] [--no-refresh-views]`

Fetches, parses and loads one 13F. It looks the accession number up in EDGAR's
submissions index for the CIK, lists the filing directory, identifies the cover
page and the information table, archives all of it to the
[raw store](#the-raw-archive), and only then parses it and writes the result in
one transaction. The same transaction publishes it: `position_snapshot` for
the filing's period, and `position_change` for that period and the filer's next
one, as [`recompute`](#recompute---filer-slug---period-yyyyqn---all---include-suspect---no-refresh-views)
would rebuild them.

```
0001193125-26-352200  13F-HR
  filer       Berkshire Hathaway Inc  (CIK 0001067983)
  period      2026-06-30  (2026Q2)
  filed       2026-08-14 20:05:04+00:00  (values x1)
  documents   primary_doc.xml + 56757.xml
  archived    raw/13f/0001067983/0001193125-26-352200/
  rows        89 rows parsed, 89 declared, 29 positions loaded, 60 folded into another line
  value       $299,253,556,246.00
  status      ok
  written     filing #1, 29 holdings, 29 new securities
  published   29 positions in 2026Q2
  changes     29 new, 0 add, 0 trim, 0 hold, 0 exit in 2026Q2
```

A load that publishes then runs
[`refresh-views`](#refresh-views---view-name---no-concurrent), after its own
transaction has committed. A skipped filing, a dry run and a deferred filing
publish nothing, so they refresh nothing.

| Flag | |
| --- | --- |
| `--cik` | Which filer's archive the filing lives under. Optional only for a filing already in the database, whose CIK is then already known — see below |
| `--force` | Re-fetch and re-load a filing that is already loaded, replacing its archived documents |
| `--dry-run` | Fetch, parse and print the same summary. Write nothing, archive included |
| `--no-refresh-views` | Publish, but do not refresh the materialised views. For a script that loads many filings and runs `refresh-views` once at the end |

**`--cik` is not optional as often as you would like.** EDGAR's archive path is
`/Archives/edgar/data/<cik>/<accession>/`, and the CIK in it is the *filer's* —
not the ten digits at the front of the accession number, which identify whoever
transmitted the submission and are usually a filing agent. Berkshire's own 13F
lives under `data/1067983/` with an accession number beginning `0001193125`, and
the path built from the latter does not exist. So the CIK has to come from
somewhere. The command takes it from this flag, or from an existing `filing`
row, or from the filing's `pending_filing` row. The last is the common case,
because `discover-filings` records the CIK it listed the filing under.

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
because deleting a portfolio that is 99% right leaves a hole shaped exactly
like a manager who filed nothing. What waits is publishing it: the summary says
`withheld` instead of `published`, and the period stays out of
`position_snapshot` until someone looks. See
[`recompute`](#recompute---filer-slug---period-yyyyqn---all---include-suspect---no-refresh-views).

### The raw archive

Every document `ingest-filing` fetches is written to the raw store *before* it
is parsed — the cover page, the information table and EDGAR's `index.json` for
the directory — uncompressed and byte for byte as EDGAR served it:

```
raw/13f/{cik}/{accession_no}/{filename}
raw/13f/0001067983/0001193125-26-352200/primary_doc.xml
raw/13f/0001067983/0001193125-26-352200/56757.xml
raw/13f/0001067983/0001193125-26-352200/index.json
```

That ordering is what makes a parser bug cheap: a filing whose parse crashes is
already archived, and fixing the bug means re-parsing stored bytes rather than
re-crawling EDGAR at ten requests a second. It also keeps our copy of a filing
EDGAR later restates or withdraws. Writes are once-only — a key that exists is
left alone, so the first copy is the one kept — unless `--force` says otherwise.
`filing.raw_key` holds the filing's prefix.

`RAW_STORE_BACKEND` picks the implementation ([app/storage](app/storage/)):

- **`local`** (the default) writes under `./data/raw/`, gitignored. Refused in
  staging and production, where a container's disk does not survive a deploy.
- **`s3`** is any S3-compatible bucket. For Cloudflare R2, point
  `RAW_STORE_S3_ENDPOINT_URL` at `https://<account-id>.r2.cloudflarestorage.com`
  and set `RAW_STORE_S3_REGION=auto`; nothing else changes. The tests run the
  S3 store against a moto server through that same endpoint setting.

### `seed-investors [--file] [--dry-run]`

Upserts [`data/investors.yaml`](data/investors.yaml) — the ~100 institutions the
site covers — into `filer` and `filer_cik`. Run it once on a fresh database and
again whenever the list changes; until a filer's CIK is seeded, its filings load
with their holdings deferred.

```
seed-investors  investors.yaml
  filers      100 listed: 0 created, 1 updated, 99 unchanged
  ciks        114 listed: 0 added, 0 reprioritised
  categories  value 30, growth 25, activist 16, quant 11, multi_strategy 10, macro 8
  overlap     sum: two-sigma; every other filer: successor
  updated     berkshire-hathaway
```

**Idempotent, and additive only.** A second run reports every filer unchanged.
Nothing is ever deleted: a CIK dropped from the list stays mapped (and is
printed as `kept`), because filings already resolve through it.

**Slugs are public URLs, and the seed defends them.** A CIK that the database
maps to a different slug is refused, and nothing is written. The usual cause is
a renamed slug. Restore the old slug, or move the CIK on purpose with a
migration.

**The file is validated first,** before any connection is opened, by the schema in
[app/ingestion/investors.py](app/ingestion/investors.py). The same schema runs
over the committed file in `tests/test_investors.py`, so a duplicate slug, a
duplicate CIK or an unknown category fails the unit suite, not a deploy.

**The order of an entry's `ciks` is data.** When two of a filer's CIKs both filed
for one period, only the one listed last counts, unless the entry says
`overlap: sum`. See [Which filings count](docs/data-model.md#which-filings-count-effective_filing).

### `verify-investors [--file] [--csv PATH] [--as-of DATE]`

Checks every CIK in [`data/investors.yaml`](data/investors.yaml) against EDGAR's
submissions API. Hand-typed CIKs go wrong, and a wrong one validates and seeds
without complaint. This command catches it. It reads no database, so run it on
the host with `make verify-investors`, or from Actions → *verify-investors* →
*Run workflow*. It is manual only, because every run makes ~120 requests to
data.sec.gov. Illustrative output (from the test fixtures, not live EDGAR):

```
verify-investors  investors.yaml  as of 2026-09-30, stale before 2026-03-31
        slug                cik         13F-HR  earliest    latest       sim  edgar name                            flags
  ok    berkshire-hathaway  0001067983       2  2013-03-31  2026-06-30  1.00  BERKSHIRE HATHAWAY INC
  warn  pershing-square     0001336528       1  2024-12-31  2024-12-31  1.00  Pershing Square Capital Management,…  stale (predecessor)
  warn  pershing-square     0002026053       2  2025-03-31  2026-06-30  0.24  PSH Holdco 2025 LLC                   name_mismatch
  2 filers, 3 CIKs: 0 failed, 2 with warnings, 1 ok
```

| Flag | Fails the run? | |
| --- | --- | --- |
| `NOT_FOUND` | yes | EDGAR has no such CIK — a typo |
| `NO_13F` | yes | never filed a 13F-HR (amendments and notices do not count) — usually the wrong entity's CIK |
| `STALE` | on a current CIK | latest 13F-HR period is more than two quarters behind: two due quarters missed, counting a quarter as due 45 days after it ends |
| `stale` | no | the same, on a predecessor CIK — expected, since it was succeeded |
| `name_mismatch` | no | EDGAR's name is under 0.6 `SequenceMatcher` similarity to our display or manager name, after dropping case, punctuation and legal suffixes. For a person to look at |
| `FETCH_FAILED` | yes | EDGAR could not be read for that CIK, so it went unchecked |

A CIK is *current* when it is the last one listed on its entry, or when the entry
is `overlap: sum`. Failures print in capitals and warnings in lower case. Exit 1
if anything failed. `--csv PATH` also writes the report as CSV, and `--csv -`
writes CSV to stdout in place of the table. `--as-of` re-judges staleness for
another date.

### `audit-overlaps [--filer SLUG]`

For every period in which two of a filer's own CIKs both filed, compares the
loaded holdings and says whether they are one book filed twice or two separate
books — and whether the filer's `overlap` policy agrees. Read-only, exit 0.
Illustrative output (the figures are made up):

```
audit-overlaps  4 overlapping periods across 1 filers, 0 disagreeing with their policy
  pershing-square  2025-06-30  0002026053 vs 0001336528
              12 vs 12 positions, $13,812,443,210 vs $13,812,443,210, 100% identical -> same book
              policy successor: agrees
```

The test is matching share counts, not shared tickers: two books from one shop
hold many of the same names, but only one book filed twice reports the same
number of shares of each. It only sees periods that have been ingested, so run
it after loading a filer's overlap quarters. When it disagrees with a policy,
change `overlap` in the YAML and re-seed.

### `audit-amendments [--filer SLUG]`

Every `(filer, period)` with more than one 13F, each filing listed in the
order EDGAR accepted it, with whether it counts toward the period and why.
Whether it counts is read from the `effective_filing` view, so the totals are
the ones the read path sums. Read-only, exit 0. Output over the Berkshire
fixtures:

```
audit-amendments  2 periods with more than one filing across 1 filers, 0 to look at
  berkshire-hathaway  2023Q3  restated, plus 1 addition: 46 positions, $314,952,628,264
    2023-11-14  0000950123-23-010898  13F-HR                          45  $    313,257,308,189  replaced by 0000950123-23-011029
    2023-11-16  0000950123-23-011029  13F-HR/A no.1 restatement       45  $    313,257,308,189  counts: the whole period
    2024-05-15  0000950123-24-005653  13F-HR/A no.2 new holdings       1  $      1,695,320,075  counts: adds to 0000950123-23-011029
  berkshire-hathaway  2023Q4  original, plus 1 addition: 42 positions, $351,900,674,461
    2024-02-14  0000950123-24-002518  13F-HR                          41  $    347,358,074,461  counts: the whole period
    2024-05-15  0000950123-24-005664  13F-HR/A no.1 new holdings       1  $      4,542,600,000  counts: adds to 0000950123-24-002518
```

A period that resolves by the rules but may still be wrong gets a `!` line:
an amendment with no `amendmentType`, which is left out rather than guessed
at; new-holdings amendments counting with no original or restatement loaded
under them; two originals from one CIK; a gap in the amendment numbers, which
usually means an amendment has not been discovered or loaded. Run it before
publishing a backfill. Filings loaded before migration `0008` have no
`amendment_no` until `backfill --force` reparses them from the archive.

### `check-data [--filer SLUG] [--include-suspect]`

The checks that need more than one filing, run before publishing. The guards
judge each filing against its own cover page at ingest; these judge filings
against each other. Read-only. Output over the seven Berkshire fixtures, with
2023Q4's cover page edited to declare one row more than its table has:

```
check-data  every filer: 2 findings — 1 suspect period, 0 concentrated periods, 0 position jumps, 1 filing gap
  suspect periods: withheld from position_snapshot
    berkshire-hathaway  2023Q4  0000950123-24-002518  13F-HR  failed entry_count
  filing gaps: quarters with no 13F loaded
    berkshire-hathaway  2023Q1-2023Q2  between 2022Q4 and 2023Q3; nothing on file: check EDGAR, then discover-filings
```

| Check | Finds |
| --- | --- |
| suspect periods | Every `(filer, period)` a suspect filing counts toward — exactly what `recompute` withholds. A suspect filing a later restatement replaced is not listed: nothing reads it |
| concentrated periods | A period whose largest position is over 90% of its value |
| position jumps | A security whose share count grew more than 10,000% on the calendar quarter before. Shares of stock only, as `position_snapshot` holds them. Printed with the implied price on both sides |
| filing gaps | Quarters with no loaded 13F between a filer's first and last. A `13F-NT` fills its quarter. Says whether filings for the gap are on file and did not load (run `backfill`) or were never found (check EDGAR) |

**Most of these fire legitimately, and that is the point.** The jump check
exists for stock splits: shares multiply and the price divides, so a split
looks exactly like a manager buying many times over until you read the price.
Only an extreme split crosses 10,000% — 20-for-1 is +1,900% — and a share count
read from the wrong column looks the same. A holding company does keep 95% of
its book in one name. Each finding is there to be looked at.

`--include-suspect` also checks suspect periods' positions, as `recompute
--include-suspect` would publish them. **Exit codes.** 0 when every check comes
back empty, 1 when anything is found, 2 when `--filer` names no filer — a typo
that checked nothing must not read as a clean bill.

### `recompute [--filer SLUG] [--period YYYYQN] [--all] [--include-suspect] [--no-refresh-views]`

Rebuilds `position_snapshot`, the published portfolio: one row per security per
`(filer, period)`, summed over the filings that count once amendments and
overlapping CIKs are resolved, with its `weight_pct` of the period and the
`source_filing_id` it was read from. Common stock only: option lines and `PRN`
principal amounts stay in `holding` and are not published.

Then `position_change`, from the snapshot just built: each of those rows against
the filer's previous published period, as `new`, `add`, `trim` or `hold`, with
the previous shares, value and weight and the deltas. A change in shares within
±0.01% is a `hold`, so the few shares a count drifts by between quarters do not
read as trading. Each position the previous period had and this one does not is
an `exit`, a row of zero shares whose deltas are the whole previous position. A
filer's latest period has no exits until it files the next one: a missing
filing is not an exit.

**What it rebuilds is a set of `(filer, period)` pairs.** `--filer` is every
period the filer has filed for or published, `--period 2026Q1` is every filer's
2026Q1, the two together are one pair, and `--all` is every pair there is. One
of them is required: a bare `recompute` would be exactly the five years of
everything that one new filing should not cost. Each pair's rows in both tables
are deleted and inserted again, all in one transaction. Not upserted, because a
rebuild can remove rows, such as a position a restatement no longer lists, and
an upsert would leave them behind. Over the same fixtures as above:

```
recompute  position_snapshot for every filer: 144 positions in 3 periods of 1 filer
  changes     position_change: 62 new, 8 add, 15 trim, 59 hold, 16 exit
  withheld    1 period with a suspect filing — check-data lists them; --include-suspect publishes them
```

The 49 positions of 2022Q3 are among the `new`: it is the first period loaded,
and a null `prev_period_of_report` says so. 2023Q3 is compared with 2022Q4, the
period before it that was loaded.

**The period after comes too.** A period's changes are against the filer's
previous published period. So when 2022Q4 is rebuilt, 2023Q3's changes are
stale, because they were computed against the old 2022Q4. Each filer's next
published period after a rebuilt one has its `position_change` rebuilt as well,
and the `next` line says which:

```
recompute  position_snapshot for 2022Q4: 49 positions in 1 period of 1 filer
  changes     position_change: 13 new, 8 add, 15 trim, 59 hold, 16 exit
  next        also the changes of 1 next period, which start from a rebuilt one: 2023Q3
```

Next *published*, not next calendar quarter: 2023Q3 follows 2022Q4 across two
quarters with nothing loaded, and 2023Q3 has no next period at all while
2023Q4 is withheld. One period is enough, because a change depends on its own
period and the one before it and nothing else. Only the changes walk forward.
The next period's snapshot does not depend on this one, and rebuilding it would
withhold it again if it had been published with `--include-suspect`.

**Ingesting a filing runs it for you.** `ingest-filing` and `backfill` rebuild
the pair each filing is filed under, and the period after, in the transaction
that loads it. Rebuilds take turns on a Postgres advisory lock, so backfill's
concurrent loads of one filer's quarters cannot build a change from a snapshot
another load is replacing. Run it by hand after anything else that changes what
counts toward a period. `seed-investors` moving a CIK or changing an overlap
policy needs `--filer`, and a change to how either table is built needs `--all`.
Either way it then runs
[`refresh-views`](#refresh-views---view-name---no-concurrent), unless told
`--no-refresh-views`. It does this even after a rebuild that published nothing,
since the rebuild may have deleted rows that the views still count.
`--all` is also the backstop when in doubt. On a synthetic dataset the size of
the curated universe, 1.6 million positions, it took about 45 seconds on stock
Postgres settings, two thirds of it the derived tables' foreign key checks.

**A period a suspect filing counts toward is withheld, all of it.** Not just
that filing: 2023Q4's addition was fine, but the original without it is the
portfolio before confidential treatment expired, and would read next quarter as
Berkshire buying Chubb. `--include-suspect` publishes those periods and marks
every row `suspect`, so data published without a check never looks like data
published with one. Exit 1 if `--filer` names no filer, and 2 without `--filer`,
`--period` or `--all`, or with `--all` and either of them.

Migrations `0011` and `0012` create the two tables empty, in their current
shape, and `0013` adds exits without writing any, so run `recompute --all` once
after upgrading past them. That refreshes the views too.

### `refresh-views [--view NAME] [--no-concurrent]`

Refreshes the five materialised views that the market-wide and per-filer reads
are served from. Each aggregates the derived tables:

| View | One row per | What it holds |
| --- | --- | --- |
| `mv_consensus_holdings` | security, period | holder count, total value and shares, average and median weight, rank by value |
| `mv_quarter_flows` | security, period | bought, sold and net value, net shares, new positions, exits, buyers, sellers |
| `mv_filer_summary` | filer, period | portfolio value, position count, top-10 weight, turnover |
| `mv_year_flows` | security, period | `mv_quarter_flows` over the four quarters ending at the period, each filer counted once |
| `mv_filing_feed` | effective filing of a published period | filed at, the period's position count and largest trade |

On a synthetic dataset the size of the curated universe, 100 filers over 20
quarters with 1.6 million positions and 2.6 million changes, on stock Postgres
settings:

```
refresh-views  3 materialised views refreshed in 4.5s
  mv_consensus_holdings     80,000 rows    2.1s
  mv_quarter_flows          76,000 rows    1.2s
  mv_filer_summary           2,000 rows    1.2s
```

Each view's live query takes one to two seconds over the same data. The 50
most-held stocks of one quarter, read from `mv_consensus_holdings`, take 2ms.
On the dev database's real five-year backfill all five refresh in 5.6s, of
which `mv_year_flows` (161,000 rows) is 2.5s and `mv_filing_feed` 0.6s.

**It runs after whatever publishes.** `recompute` runs it after every rebuild.
`ingest-filing` runs it after a load that publishes. `backfill` runs it once at
the end, never after each filing, because a refresh partway through a backfill
would publish a quarter with a third of its filers in it, as the consensus of
all of them. Nothing runs it on a timer. Each of the three takes
`--no-refresh-views`, and then `refresh-views` is yours to run. Until it runs,
each view is as of its last refresh.

**It is always a run of its own.** The automatic refresh starts after the
publishing run has committed and recorded how it went. It is then recorded as
a `refresh-views` run, with the publishing run's id as `after_run_id` in its
`context`. A refresh that fails therefore cannot mark a finished rebuild as
failed, and `runs --job refresh-views` lists every refresh, whoever started it.

**Readers are not blocked.** Each view is refreshed `CONCURRENTLY`, which its
unique index allows. Postgres builds the new rows beside the old ones and
applies the difference, and readers see the old rows until the commit. That is
more work than a plain refresh, which is an acceptable price for never blocking
the API. The refresh waits for any `recompute` in progress, and holds off the
next until it commits, so the views are refreshed from the same tables.

| Flag | |
| --- | --- |
| `--view` | Only this view, and any view that reads it, so that none is left disagreeing with one it reads. The others stay as of their own last refresh |
| `--no-concurrent` | Plain `REFRESH`. It does less work, but each view is locked against reads from its refresh until the last one commits. For a database nobody is reading |

**A view that reads another is refreshed after it.** The order comes from the
Postgres catalog, including dependencies through a plain view, so a new view
that reads an existing one is ordered correctly without anyone declaring it.
None of the three reads another yet.

**When each view was last refreshed is recorded.** `matview_refresh` has one
row per view, which every refresh replaces in the same transaction as the
view's rows. A reader never sees a refresh time newer than the data. This is
what lets an endpoint say honestly when its aggregates were computed:

```sql
SELECT view_name, refreshed_at, run_id FROM matview_refresh;
```

A view with no row has not been refreshed since migration `0015`, and its
last refresh is unknown. How long each view took is in the run's `ingestion_run`
row, and in a `materialised_view.refreshed` log line per view:

```sql
SELECT started_at, view, (detail ->> 'seconds')::numeric AS seconds, detail ->> 'rows' AS rows
FROM ingestion_run, jsonb_each(metrics -> 'views') AS v(view, detail)
WHERE job_name = 'refresh-views'
ORDER BY started_at DESC, view;
```

What the numbers mean, including turnover's formula and why flows count traded
dollars rather than `value_delta`, is in
[the data model](docs/data-model.md#materialised-views). Migration `0014`
creates the views filled from whatever the derived tables held then.

### `reconcile [--include-suspect] [--sample N]`

The published tables checked after the fact: against each other, against the
filings they came from, and against the views built on them. `check-data`
looks at filings before they are published; this looks at what was. Read-only,
one `REPEATABLE READ` snapshot for every check, about 7 s over the full
backfill. `make reconcile` runs it in the container. Output with one exit
missing:

```
reconcile  29 positions and 32 changes in 10 periods: 2 of 8 invariants fail
  ok    weights_sum_to_100              every (filer, period)'s weights sum to 100 ± 0.01
  ok    snapshot_traces_to_filing       every position traces to a non-suspect filing that counts toward its period
  ok    changes_match_snapshot          every change but an exit is a snapshot row, and every snapshot row has its change
  FAIL  changes_follow_previous_period  every change is against the filer's previous published period, exits included
        1 row break it:
          charlie-fund  2024Q4  22222B202  sold out of with no exit
            1000.0000 shares, $20000.00 in 2024-06-30
  ok    new_and_exit_rows               new rows alone have no previous shares, and exit rows hold nothing
  FAIL  value_deltas_add_up             every (filer, period)'s value deltas sum to its change in portfolio value, exactly
        1 row break it:
          charlie-fund  2024Q4  value deltas do not add up to the change in portfolio value
            deltas sum to $20000.00; the portfolio went from $40000.00 to $40000.00, a change of $0.00
  ok    nothing_negative                no position has negative shares or value
  ok    views_match_live                every materialised view holds what its live query returns now
```

Each invariant is a query for the rows that break it, in
[app/derived/reconcile.py](app/derived/reconcile.py), which says what each one
allows. Two are worth knowing about:

- **Value deltas add up exactly**, not within a tolerance. Every position the
  previous period held is a change row, held on or exited, so the sum is the
  difference of the two totals, with nothing to round. Any difference is a
  wrong row, and a tolerance would only let a slightly wrong `value_delta`
  through.
- **A position must trace to a non-suspect filing**, so a snapshot rebuilt with
  `recompute --include-suspect` fails it on every row of a suspect period.
  `--include-suspect` says that was intended, and then checks only that every
  such row is marked.

The same queries run in
[tests/integration/test_reconcile.py](tests/integration/test_reconcile.py),
over three filers and four quarters whose every derived number was worked out by
hand, then over that fixture broken one way at a time, each breakage caught by
its invariant on its row. **Exit codes.** 0 when every invariant holds, 1 when
any fails; under `make`, which exits 2 for any failing command.

### `runs [--job NAME] [--limit N]`

Lists the most recent job runs, newest first, from `ingestion_run`. Every verb
above writes one row as it starts and fills in how it ended, so "did the
backfill finish?" is answered here or in SQL rather than by scrolling back
through a terminal:

```
runs  2 most recent runs
  started                    job               status   elapsed    seen  written  run_id
  2026-10-01 11:50:00+00:00  backfill_13f      partial    3m12s      21       19  5c1d0e6a-…
                             0001193125-22-000123: FilingDocumentsError: no information table  (+1 more)
  2026-10-01 10:30:00+00:00  discover-filings  success    12.0s      61       21  9b2f4a17-…
```

```sql
SELECT status, items_seen, items_written, error
FROM ingestion_run WHERE job_name = 'backfill_13f'
ORDER BY started_at DESC LIMIT 1;
```

| Flag | |
| --- | --- |
| `--job` | Only runs of this job. Job names are the verbs, except `backfill`, which records as `backfill_13f`. A name with no runs lists the names that have some |
| `--limit` | How many runs, default 20 |

| Status | Means |
| --- | --- |
| `running` | Started, not finished. One that started hours ago is a process that died without recording why (`SIGKILL`, OOM). Nothing else leaves a run in this state |
| `success` | Finished, and everything it took on went through |
| `partial` | Finished with something left undone: failed filings, an unreadable CIK, a backfill EDGAR blocked or Ctrl-C stopped. `error` has one line per item |
| `failed` | Raised. `error` starts with `ExceptionType: message`. A second Ctrl-C lands here, as `CancelledError` |

**`run_id` is the row's `id`.** It is bound to every log line the run writes,
so the row leads to the log and the log back to the row with one grep. `context`
holds the run's parameters as `jsonb`, e.g. `WHERE context @> '{"force": true}'`.
`metrics` holds what the run measured beyond its counters, written as it ends.
So far only `refresh-views` measures anything: each view's duration, rows, and
whether it was refreshed concurrently.

**A verb that publishes leaves two rows.** `recompute`, and an `ingest-filing`
or `backfill` that loaded something, are followed by the `refresh-views` run
they started. Its `context` names them in `after_run_id`.

**What the counters count**, per job:

| Job | `items_seen` | `items_written` |
| --- | --- | --- |
| `backfill_13f` | Filings it set out to ingest or reprocess. Skipped ones do not count, as with `--limit` | Filings loaded, `ok` or `suspect` |
| `discover-filings` | Filings found in EDGAR's listings | Filings now in `pending_filing` |
| `ingest-filing` | 1 | 1 when it loaded, 0 when skipped or a dry run |
| `seed-investors` | Filers in the list | Filers created or updated, 0 on a dry run |
| `recompute` | Periods resolved, published or withheld | Periods published |
| `refresh-views` | Materialised views to refresh | Materialised views refreshed |
| `check-data`, `audit-*` | Findings reported | 0 |

**Every verb records its run except two.** `runs` reads the record, and a
listing that added itself would always show itself first. `verify-investors`
never opens a database: it runs on the host and in a GitHub workflow, and that
workflow's history is its record. A test fails if a new verb is neither tracked
nor one of these. Jobs written later do the same, through
[`track_run`](app/jobs/tracking.py).

## The API

One filing, which everything else gets debugged through, the investors, the
stocks, the market, and a search across the first two. All but the filing are
under `/v1`.

### Collections: `{data, meta, page}`

Every collection endpoint answers in one envelope
([`Envelope`](app/api/schemas/envelope.py)), so a client handles one shape for
all of them:

```json
{
  "data": [ ... ],
  "meta": {
    "period": "2026Q1",
    "period_end": "2026-03-31",
    "latest_filing_at": "2026-08-14T13:34:05Z",
    "coverage": { "filers_reported": 69, "filers_tracked": 100 },
    "quarters": null,
    "refreshed_at": "2026-10-02T08:40:11Z",
    "generated_at": "2026-10-02T09:12:44Z"
  },
  "page": { "limit": 50, "next_cursor": "eyJ2IjoxLCJrIjoi..." }
}
```

- **`meta` states the period, always.** Nothing in this API is "current". A
  13F describes the last day of a quarter and arrives up to 45 days later, so
  a period's numbers are partial while its filings come in. `coverage` says how
  partial: filers with a published portfolio for the period, out of all the
  filers we track. `latest_filing_at` is the newest of those published filings.
  Both are counted from `mv_filer_summary`, so they move when the views are
  refreshed. For a collection that is not about a period, the period fields
  are null. For figures over a year, `quarters` names its four, and `period`
  is the last.
- **`refreshed_at` says how old an aggregate is.** On the market endpoints,
  which read only the materialised views, it is when they were last
  refreshed: a filing published since is not in the answer yet. Null on the
  endpoints that read live.
- **Pages are cursors, not offsets.** Pass `page.next_cursor` back as
  `?cursor=` with the same filters until it comes back null. A cursor is a
  position (the last row's sort values), so rows that are inserted or
  deleted while you walk never cause a repeat or a skip, and page 200 costs
  what page 1 does. The cursor is opaque, and one that is malformed, from
  another sort order, or from an older version of the API is a `400` telling
  you to start again.
- **`?limit=` defaults to 50 and stops at 200.** Above 200 is a `422`, not a
  shorter page.

Endpoints get this from [`PageParamsDep`](app/api/deps.py) and
[`paginate`](app/api/pagination.py), which takes a statement and a `Keyset`
(the sort order, ending in a unique key) and returns the page and its cursor.
A listing that joins something to each row it returns, like the investors'
top holding, builds the page with `page_statement`, joins to that as a CTE,
and hands the rows to `page_of`. The join then costs a page, not the table.

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
    { "kind": "entry_count", "severity": "error", "detail": "parsed 3 rows, cover page declares 99" }
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

### `GET /v1/investors` and `GET /v1/investors/{slug}`

Every tracked investor, with its latest published portfolio; and one of them,
with more.

```bash
curl "localhost:8000/v1/investors?sort=value&category=value&q=buff&limit=20"
curl localhost:8000/v1/investors/berkshire-hathaway
```

```json
{
  "slug": "berkshire-hathaway",
  "display_name": "Berkshire Hathaway",
  "manager_name": "Warren Buffett",
  "category": "value",
  "latest_period": "2026-06-30",
  "last_filed_at": "2026-08-14T20:05:04Z",
  "portfolio_value_usd": "299253556246.00",
  "position_count": 29,
  "top_holding": { "cusip": "037833100", "ticker": null, "issuer_name": "APPLE INC", "weight_pct": "22.038267" },
  "sparkline": ["266378900503.00", "267175474249.00", null, "...", "299253556246.00"],
  "first_period": "2021-09-30",
  "top10_weight_pct": "88.468905",
  "turnover_pct": "4.418463",
  "ciks": ["0001067983"]
}
```

The last four are the detail's. The list's rows are the rest, in the envelope.

- **Each row names its own period.** `latest_period` is the newest quarter
  published for *that* investor, so two rows of one list can describe
  different quarters. `meta.period` is null: the list is not about one.
- **From `mv_filer_summary`**, so as of the last `refresh-views`. The top
  holding is the exception, read from `position_snapshot` for the period the
  view names.
- **Investors with nothing published are listed**, with their figures null,
  and last under `sort=value` and `sort=positions`. Not loaded yet and every
  filing withheld look the same here. `check-data` tells them apart.
- **`sparkline`** is eight quarters ending at `latest_period`, oldest first,
  with `null` for a quarter with nothing published. It is not compacted: a gap
  is a gap.
- **`top_holding` carries the issuer name** because no ticker is resolved
  until Epic 4. It is the largest position by value, and a tie goes to the
  same security every time.
- **`?q=`** matches our name, the EDGAR name and the manager's, ignoring case.
  `%` and `_` match themselves. **`?category=`** is one of the six styles; any
  other value is a `422`.
- **One query per request**, page size notwithstanding. See
  [query-performance.md](docs/query-performance.md#built-since-the-investor-list-and-detail).

### `GET /v1/stocks/{ticker}`, `/owners` and `/ownership-history`

One stock, who holds it in a period, and how that has moved quarter by
quarter.

```bash
curl localhost:8000/v1/stocks/037833100                    # by CUSIP: no ticker resolves yet
curl "localhost:8000/v1/stocks/037833100/owners?period=2026Q1&limit=20"
curl localhost:8000/v1/stocks/037833100/ownership-history
```

- **`{ticker}` is a ticker, an alias, or a CUSIP**, in any case, tried in
  that order. Aliases are `security_alias`: former tickers and other
  spellings (`FB`, `BRK-B`). Nothing fills it yet, and no ticker is resolved
  until Epic 4, so for now a stock is found by its CUSIP. When a ticker
  names more than one security, recycled or across a CUSIP change, the one
  held most recently wins.
- **An unknown one is a `404` with suggestions**, in
  `detail: {message, suggestions}`: up to five stocks whose ticker starts with
  what was asked for, then whose name has a word like it, the most dollars
  held first. `/v1/stocks/appl` suggests APPLE INC.
- **The detail is the latest period published for anyone**, which it names
  in `period` with its `coverage`, from the views. A stock nobody holds then
  is zeros, not a 404. `net_shares` and `net_value_usd` leave out filers in
  their first period, as the flows do. `sector` is null for every stock:
  nothing loaded carries one.
- **Owners** are the period's holders, largest first, each with its change
  since that investor's previous period. Exits are not owners. `?period=`
  defaults to the latest published for anyone, and one nobody published is
  a `404`.
- **The history names five holders on every row**: the largest by value in
  the latest quarter anyone held the stock, followed back through every
  quarter, with everyone else as `other`. Choosing them per quarter would
  make a chart whose series change identity. A holder's `shares` is `0` in a
  quarter it published without the stock and `null` in one it published
  nothing for. The five and `other` add up to the total on every row.

### `GET /v1/market/*` and `GET /v1/flows`

The landing page and the screener. Every one reads the materialised views and
nothing else but names, so each answers in under 10ms on the dev database's
full backfill, and is as of the views' last refresh (`meta.refreshed_at`).

```bash
curl "localhost:8000/v1/market/top-holdings?metric=value&limit=10"
curl "localhost:8000/v1/market/top-buys?period_type=year"
curl "localhost:8000/v1/market/top-sells?period=2026Q1&metric=net_value"
curl localhost:8000/v1/market/new-positions
curl localhost:8000/v1/market/activity
curl "localhost:8000/v1/flows?period_type=year&direction=buy&min_investors=5&sort=buyers"
```

- **`top-holdings`** ranks by `?metric=holders` (the default: held by the
  most filers, ties to the larger holding) or `value` (the most dollars held).
- **`top-buys` and `top-sells` rank by gross or net, and say both.** A year's
  net flow nets a position bought in Q1 and sold in Q3 to about nothing,
  which is right for net flow and wrong for the year's top buys. So every row
  carries `gross_bought_usd`, `gross_sold_usd` and `net_value_usd`, and
  `?metric=value` (the default) ranks by gross dollars traded that way,
  `net_value` by net, `holders` by the filers trading that way. A list only
  holds stocks on its side of zero: a stock merely sold least is not a buy.
  On the dev data, 320 stocks in the latest year had over $100M of gross
  buying and a net under a fifth of it.
- **`?period_type=year` is the four quarters ending at `?period`**: the
  calendar year for a Q4, the trailing twelve months otherwise. Dollars are
  the four quarters' added up, exactly. Counts are distinct filers: one that
  bought in three of the quarters is one buyer of the year.
- **`new-positions`** ranks by filers that opened a position. A filer's first
  period is never counted, as in every flow.
- **`activity`** is the published filings, newest first and paged, each with
  its period's `position_count` and `largest_change`, the period's largest
  trade by dollars. That is null in a filer's first period. A filing withheld
  as suspect is not listed until its period is published.
- **`/v1/flows` is the screener**: every stock traded, held on through or
  exited in the period, filtered by `direction` (`buy` or `sell`, on net),
  `min_investors` (filers holding it at the period end) and `min_value`
  (dollars they hold), sorted by any flow figure, `holders` or `value_held`,
  and paged. `?sector=` is a `422` for now: no source of sectors is loaded,
  and an empty page would say no stock is in it.

### `GET /v1/search`

The ⌘K palette: investors and stocks by name or ticker, in one response,
grouped. Not a `{data, meta, page}` collection, since it is two short lists of
different things, about no period, never paged.

```bash
curl "localhost:8000/v1/search?q=berkshire"
curl "localhost:8000/v1/search?q=microsft&limit=10"
```

```json
{
  "query": "berkshire",
  "investors": [
    { "slug": "berkshire-hathaway", "display_name": "Berkshire Hathaway",
      "manager_name": "Warren Buffett", "category": "value" }
  ],
  "securities": [
    { "cusip": "084670702", "ticker": null, "issuer_name": "BERKSHIRE HATHAWAY INC CL B" }
  ]
}
```

- **The order is the contract.** In each group: an exact ticker first, then
  tickers starting with `q`, then names starting with it (an investor's, its
  manager's, or an issuer's), then names merely like it, by `pg_trgm`. Ties go
  to the most dollars held in the latest period. So `apple` puts APPLE INC
  above Apple Hospitality REIT, and both above APPLIED MATLS INC, which is
  held for 300 times the dollars of Apple Hospitality but only looks like
  `apple`.
- **Misspelt and half-typed both work**, from three characters: `microsft`
  finds Microsoft, `hath` Berkshire Hathaway, `square` Pershing Square,
  `buffett` Berkshire by its manager. Two characters match prefixes only.
- **`q` is trimmed and then at least 2 characters**, or a `422`. `limit` is
  per group, 5 by default, at most 20. `query` echoes the trimmed `q`, for a
  client typing ahead to tell which request a response answers.
- **No tickers are resolved yet**, so for now a stock is found by its name,
  and linked by its CUSIP (`/v1/stocks/{cusip}`).
- **It answers in 4 to 6ms for most searches** on the dev database's full
  backfill, and 28ms for the slowest found, `inc` at `limit=20`. Why the
  matching and the ranking are written the way they are is in
  [`app/api/routers/search.py`](app/api/routers/search.py), and the plans
  are in [docs/query-performance.md](docs/query-performance.md).

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
- **Verify, do not assume.** `app/ingestion/normalisation.py` runs five guards
  over every normalised filing — the implied share price of every `SH` row, the
  row count against the cover page's `tableEntryTotal`, the summed value
  against its `tableValueTotal` within 1%, no negative quantities, and CUSIPs
  that are nine letters and digits — and records what fires in
  `filing.parse_status` and `filing.parse_notes`. The price check is the one
  that matters most: it is the only one that reaches outside the document, so
  it catches a filer who kept using the old convention as well as a parser that
  did. Guards **flag, they do not reject** — a suspect filing still loads,
  because the alternative is a hole that looks exactly like a manager who filed
  nothing. What waits is publishing: `recompute` leaves its period out of
  `position_snapshot`, and `check-data` lists it. Fixtures exist for both
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

Five things worth knowing:

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
- **`EDGAR_CACHE_DIR` is for development only.** When it is set, the EDGAR
  client keeps every body it fetches under that directory and serves it from
  there next time, so re-running a parser does not refetch a filer's whole
  submissions history. Nothing in the cache expires, so a cached submissions
  index misses anything filed since. `Settings` refuses the variable in staging
  and production. Delete the directory to see what EDGAR says today.
- **`RAW_STORE_BACKEND=local` is for development only.** Staging and production
  must use `s3`, with a bucket, so archived documents outlive the container. See
  [The raw archive](#the-raw-archive).

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

Give a customer the id from the header and their whole request is one grep.
Batch work gets the same from [`track_run`](app/jobs/tracking.py), which binds
`job_name` and `run_id` for the run's duration. The `run_id` is the id of the
run's `ingestion_run` row, so one run is one grep, and one `SELECT`:

```python
async with track_run(settings, "backfill_13f", filer=slug) as run:
    ...  # every line logged in here carries run_id and job_name
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
| `run_id` | One execution of a job; every line from that run shares it. The `id` of its `ingestion_run` row |
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
