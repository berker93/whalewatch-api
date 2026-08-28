# WhaleWatch — data model

Target state for Epic 1. The five core tables — `filer`, `filer_cik`, `filing`,
`security`, `holding` — are migrated as of `0002_core_schema`; everything else
below is still a sketch and is marked where it matters. This document and
`app/db/models/` move together or the document is wrong.

## Three layers

Filings arrive immutable, get normalised, and then get aggregated. Keeping those
as three layers rather than one is what makes a parser bug survivable.

```
  raw_document          exactly what EDGAR served, archived, never rewritten
        |
        v
  filing / holding / insider_transaction      normalised, one row per fact
        |
        v
  holding_change / mv_market_flows            derived, recomputable, droppable
```

**The raw layer is the source of truth.** A parser bug found in Epic 3 is
repaired by re-parsing what we already have, not by re-crawling EDGAR at 8
requests per second for a week. EDGAR is also not a stable archive of itself:
documents get re-filed, and a fetch in 2027 need not return the 2026 bytes.

**The derived layer is a cache with a schema.** Anything in it can be dropped
and rebuilt from the layer above by `whalewatch recompute`. Nothing else may
depend on a derived table for correctness, which is what lets `make reset-db`
followed by a re-parse be a complete recovery rather than a data loss event.

## Identity

Every natural key here is EDGAR's, not ours. A surrogate `id` exists on each
table for foreign keys and cursors, but the unique constraint is always on the
natural key, because that is what makes re-ingesting a filing an upsert instead
of a duplicate.

| Entity | Natural key |
| --- | --- |
| `filer` | none of its own — see `filer_cik` |
| `issuer`, `insider` | `cik` |
| `filing` | `accession_no` |
| `holding` | `(filing_id, cusip, put_call, sshprnamt_type)` |
| `insider_transaction` | `(filing_id, line_number)` |
| `security` | `cusip` |
| `price` | `(security_id, trade_date)` |

`cik` is `CHAR(10)`, zero-padded, never an integer. Berkshire is `0001067983`;
stored as `1067983` it stops matching EDGAR's URLs, its own filings, and every
log line written before someone changed the column.

`holding`'s key includes `put_call` and `sshprnamt_type` deliberately. One filer
can report the same CUSIP three times in one filing — common stock, calls, and
puts — and a key of `(filing_id, cusip)` collapses them into one row on
conflict, silently losing two positions.

That key is declared `UNIQUE NULLS NOT DISTINCT`, which is not optional and is
easy to leave off. `put_call` is null on every common-stock row — most of the
table — and under Postgres's default handling two nulls never conflict, so the
constraint would permit unlimited duplicates of exactly the rows it exists to
protect. `ON CONFLICT` would never fire and every re-ingest would add another
full copy of the filing, which surfaces as a fund that appears to hold twice
what it holds. Requires Postgres 15+.

## Tables

### `filer` / `filer_cik`

The institution behind a 13F, and the CIKs it files under. Two tables, because
the thing a user means by "Berkshire" and the thing EDGAR means by a CIK do not
have the same cardinality.

```
filer
  id            bigint pk
  name          text         not null    -- as reported on the latest cover page
  slug          text         unique      -- 'berkshire-hathaway', stable, ours
  first_period  date                     -- earliest period we hold
  last_period   date                     -- latest period we hold

filer_cik
  id        bigint pk
  filer_id  bigint    fk -> filer, on delete cascade
  cik       char(10)  unique, not null
```

**`filer` carries no `cik` column.** One institution files under several CIKs,
routinely and permanently: funds are registered per legal entity, entities get
reorganised, and an acquired manager keeps filing under its own CIK for years.
A unique `cik` on `filer` forces ingestion to choose between inventing a second
filer for what is obviously one institution — the API then showing two
Berkshires with a decade of history each — and discarding every filing made
under the non-canonical CIKs.

The unique constraint moves to `filer_cik.cik`, so the relationship is
many-CIKs-to-one-filer in one direction and a function in the other. That second
half is what makes resolving a filing's CIK to a filer well-defined.

`slug` is the public identifier in URLs, and it is generated once and then frozen
even if the filer renames itself. A slug derived live from `name` is a URL that
changes under a client when a fund rebrands. It is `text` rather than the
`citext` this document originally specified: slugs are minted lowercase by one
function, so case-insensitive comparison has nothing to do, and `citext` is an
extension to install in every database anyone ever creates.

### `issuer` / `security`

Split, because they are different things and merging them is the bug that makes
share counts wrong.

An **issuer** is a company. A **security** is one instrument it has issued, with
one CUSIP. A company with common stock and two classes of convertible has one
issuer row and three security rows. Tickers hang off the security, not the
issuer.

```
issuer
  id          bigint pk
  cik         char(10)    unique          -- null until we see it file
  name        text        not null
  name_trgm   -- gin index, gin_trgm_ops, for /stocks search

security
  id                bigint pk
  cusip             char(9)    unique, not null
  issuer_id         bigint     fk -> issuer          -- NOT YET MIGRATED, see below
  name              text                    -- nameOfIssuer as the filing wrote it
  ticker            text                    -- null when unresolved; see below
  figi              char(12)
  resolution_source text                    -- 'openfigi' | '13f_column' | 'manual'
  resolved_at       timestamptz
```

`issuer` and `security.issuer_id` are **not in `0002_core_schema`**. A 13F
information table gives us a CUSIP and a filer-supplied issuer name and nothing
else, so the issuer table arrives with the `/stocks` search that needs it. Until
then `security.name` holds the name as filed — inconsistent, abbreviated
("BERKSHIRE HATHAWAY INC DEL"), and enough to display an unresolved security.

`ticker` is nullable and stays nullable. CUSIP→ticker resolution fails for
delisted names and obscure instruments, and a `NOT NULL` here would force the
loader to either invent a ticker or drop the holding. It drops neither: an
unresolved security still gets a row, still gets held, and still shows up in
dollar totals.

`resolution_source` exists because the newer 13F information table may itself
carry a FIGI column. When it does, that is better than an OpenFIGI lookup and we
want to be able to tell which rows came from where when a mapping turns out wrong.

### `filing`

One row per EDGAR submission, of any form type.

```
id                bigint pk
accession_no      char(20)     unique, not null   -- dashed. THE idempotency key
cik               char(10)     not null, indexed  -- as filed; issuer or insider too
filer_id          bigint       fk -> filer        -- null until resolved; null for Form 4
form_type         text         not null           -- '13F-HR', '13F-HR/A', '4', ...
period_of_report  date                            -- report date; null for Form 4
quarter           text         GENERATED STORED   -- '2024Q1', from period_of_report
filed_at          timestamptz  not null
value_multiplier  smallint     not null           -- 1 or 1000; see below
amends_id         bigint       fk -> filing       -- self-ref, set on /A forms
amendment_kind    amendment_kind                  -- enum; null when not an amendment
report_type       text                            -- 13F cover page: HOLDINGS | NOTICE | COMBINATION
parsed_at         timestamptz                     -- null = fetched but not yet parsed
parse_error       text                            -- set only alongside parse_status = 'failed'
parse_status      text         not null           -- pending | ok | suspect | failed
parse_notes       jsonb                           -- what the guards found; null when nothing did
raw_key           text                            -- object key of the archived document
source_url        text                            -- the EDGAR URL it was fetched from
ingested_at       timestamptz  not null           -- when the loader last wrote this row
raw_document_id   bigint       fk -> raw_document -- NOT MIGRATED; see raw_key below
```

`accession_no` is **the idempotency key** and its `UNIQUE` is the constraint the
whole ingestion pipeline rests on. Every task in `app/jobs` is keyed on an
accession number and Celery delivers at least once, so the collision happens in
normal operation — a retried task, a resumed backfill, an operator re-running a
quarter. Without the constraint that does not raise, it duplicates: two filings,
two sets of holdings, a portfolio reporting twice the positions it holds.

`raw_key` and `source_url` are the interim form of `raw_document_id`. A
`raw_document` table earns its place when a filing has several archived
documents worth describing separately — each with its own size, hash and fetch
time — and until then two nullable text columns hold what the loader is handed.
Both are nullable and both are written with a `coalesce` on re-ingest: a
re-parse runs off bytes already on disk and may not know where they came from,
and blanking the only pointer to a document would turn the next parser fix into
a re-crawl of EDGAR at 10 requests a second.

`ingested_at` is what makes an upsert legible after the fact. The row changes in
place, so without it "which filings did the run I started an hour ago rewrite"
has no answer — and `parsed_at` does not answer it, because a re-parse and a
re-load are different events.

`quarter` is a Postgres generated column, not a value the loader writes, so
there is no code path anywhere that can put `2024Q1` on a June period. The
expression is *not* the obvious `to_char(period_of_report, 'YYYY"Q"Q')`:
`to_char(timestamp, text)` is `STABLE` rather than `IMMUTABLE` — its output
depends on `lc_time` and `DateStyle` — and Postgres rejects a non-immutable
generation expression outright with "generation expression is not immutable".
It is composed from `extract` instead, which is immutable and produces identical
output. Alembic cannot see generated columns at all, so it is written in raw DDL
and any change to it is a hand-written drop-and-re-add.

`value_multiplier` records what the information table's `value` column had to be
multiplied by — 1 or 1000, per the
[whole-dollars cutover](ingestion-spec.md#the-whole-dollars-cutover). It is keyed
off `filed_at`, never off the period, and it exists so that a 1000x mis-parse is
diagnosable from the database rather than by re-reading the raw document.
`holding.value_usd` is normalised to whole dollars regardless.

`amendment_kind` is a native enum, `restatement | new_holdings`, normalised from
EDGAR's `<amendmentType>`. It is the most consequential field on an amendment: a
restatement replaces the period's holdings wholesale and a new-holdings
amendment adds to them, so getting it backwards either doubles every position or
discards the ones the original reported. Both outcomes look plausible.

`filed_at` matters more than it looks: it is what decides whether a 13F's dollar
values are in thousands or whole dollars. See the
[ingestion spec](ingestion-spec.md#the-whole-dollars-cutover).

`parse_status` and `parse_notes` are the record of the
[validation guards](ingestion-spec.md#the-whole-dollars-cutover). `pending` is
the default and means fetched but not parsed; `failed` means the document could
not be read at all and `parse_error` says why; `ok` means every guard passed.
The one that earns the column is `suspect`: parsed, **loaded**, and believed with
reservations, because some guard fired — a row count that disagrees with the
cover page, a total that does not sum, or a position implying a share price no
security has. A suspect filing is flagged rather than rejected. Withholding it
leaves a gap that reads, to every query downstream, exactly like a manager who
filed nothing.

`parse_status` is `text` with a `CHECK` rather than a native enum, per the rule
in `app/db/models/enums.py`: the vocabulary is ours and will grow, and
`ALTER TYPE ... ADD VALUE` has no reverse. `parse_notes` is `jsonb` because the
question it exists to answer is asked across a backfill rather than about one
row — `WHERE parse_notes @> '[{"kind": "implied_price"}]'` — and against a text
column that is a grep. Its `Decimal`s are stored as **strings**: a JSON number is
an IEEE 754 double, and a column recording a suspected 1000x error is a poor
place to round the figure a second time.

`report_type` is on the filing, not derived at query time, because a `13F NOTICE`
contains no holdings and a `13F COMBINATION REPORT` contains only the subset the
filer manages directly. Aggregating without it double-counts positions that two
affiliated managers both disclose.

### `holding`

One line of a 13F information table.

```
id                bigint pk
filing_id         bigint    not null   -- FK is composite, with period_of_report
security_id       bigint    fk -> security, not null, on delete restrict
filer_id          bigint    fk -> filer, not null      -- denormalised from filing
period_of_report  date      not null                    -- denormalised from filing
cusip             char(9)   not null                    -- as the filing wrote it
value_usd         numeric(20,2)  not null               -- ALWAYS whole dollars
shares            numeric(20,4)  not null
sshprnamt_type    text      not null                    -- 'SH' | 'PRN'
put_call          text                                  -- null | 'Put' | 'Call'
investment_discretion  text
voting_sole       numeric(20,4)
voting_shared     numeric(20,4)
voting_none       numeric(20,4)
```

`value_usd` is normalised at parse time and is whole dollars for every row
regardless of what the filing said. Storing the filing's own units and
converting at query time means every consumer has to know about the cutover, and
one of them will not.

`filer_id` and `period_of_report` are denormalised off `filing` because every
read path filters on them and the alternative is a join on every query in the
service. They are held to their source by a composite foreign key —
`(filing_id, period_of_report)` references `filing (id, period_of_report)`,
which is what the otherwise-pointless `UNIQUE (id, period_of_report)` on
`filing` exists to satisfy — so the copy cannot drift from the original. That FK
also carries the referential integrity for `filing_id`, which is why that column
has no foreign key of its own. `ON UPDATE CASCADE`, so an amendment correcting a
period carries its holdings with it; `ON DELETE CASCADE`, because re-parsing a
bad filing is a delete and the holdings are its content.

`cusip` is stored here as well as on `security`, and it is not redundant: this is
the filing's own bytes and part of the natural key, whereas `security` is a
mutable interpretation of them. When a resolution turns out to be wrong, this is
the column to compare against.

`shares` is `numeric`, not `bigint`: `sshprnamt_type = 'PRN'` rows report a
principal amount, and fractional share counts appear after some corporate
actions. Rows with `PRN` are never summed with `SH` rows — they are different
units, and a `CHECK` will not save you from that, only the query will.

### `holding_change`

Derived. One row per `(filer, security, period)` describing the move from the
previous period.

```
filer_id, security_id, period
shares_prev, shares_now, shares_delta
value_prev, value_now
change_type    -- 'new' | 'added' | 'trimmed' | 'exited' | 'unchanged'
```

`exited` rows are the reason this table exists rather than being a query: an exit
is the *absence* of a row in the current period, and absence is not something you
can index. Materialising it turns "what did they sell" from an anti-join over two
partitions into a range scan.

Recomputed, never incrementally updated. An amendment landing months later
changes a past period, and an incremental updater would have to find and fix
every downstream row it already wrote.

### `insider` and `insider_transaction`

```
insider
  id      bigint pk
  cik     char(10)  unique, not null
  name    text      not null

insider_transaction
  id                bigint pk
  filing_id         bigint  fk -> filing
  insider_id        bigint  fk -> insider
  issuer_id         bigint  fk -> issuer
  transaction_date  date    not null
  security_title    text
  transaction_code  char(1) not null     -- P, S, A, M, F, G, C, ...
  acquired_disposed char(1) not null     -- 'A' | 'D'
  shares            numeric(20,4)
  price_per_share   numeric(20,4)        -- null for grants and gifts
  shares_owned_after numeric(20,4)
  is_derivative     boolean not null
  is_10b5_1         boolean not null default false
```

`transaction_code` is stored raw and interpreted nowhere but the read layer. `P`
(open-market purchase) and `S` (open-market sale) are the two that carry
information; `M` is an option exercise, `F` is shares withheld for tax, `A` is a
grant, `G` is a gift. Collapsing them into "bought"/"sold" at ingest time is
lossy and is how a vesting event becomes a headline.

`is_10b5_1` comes from the filing's own checkbox. A sale under a pre-arranged
plan was decided months before the date on the form, which is the whole reason
the flag is on the row rather than left to the reader.

### `raw_document`

```
id            bigint pk
accession_no  char(20)  not null
filename      text      not null
content       bytea                 -- or an object-store key
sha256        char(64)  not null
fetched_at    timestamptz not null
unique (accession_no, filename)
```

`sha256` is what makes a re-fetch cheap to verify and what proves, when a number
looks wrong two years from now, whether the bytes changed or our parser did.

## Partitioning

`holding` is the only table with a partitioning plan, and it is deferred until it
hurts. Rough shape: ~4,000 filers × ~200 positions × 4 quarters is low millions of
rows a year, which Postgres does not care about. When it does, it partitions by
`RANGE (period_of_report)` — every read path already filters on it, so partition
pruning is free rather than a rewrite.

Written down now so that when the day comes, `period_of_report` is already
`NOT NULL` on `holding` and already in every unique constraint, which is what
partitioning requires and what is expensive to add later.

## Materialised views

`mv_market_flows` — net share and dollar change per `(security, period)` across
all filers, which is `/market/flows` and is otherwise an aggregate over the whole
`holding_change` table per request.

Refreshed `CONCURRENTLY`, which requires a unique index on the view, by
`whalewatch refresh-views` after each period's ingestion completes — not on a
timer. A timer refreshes mid-backfill and publishes a quarter that is one third
loaded.

Alembic does not model views. They are `op.execute()` in a hand-written migration
with a real `downgrade`, like everything else in
[the migration rules](../README.md#migrations).

## Invariants

The ones worth a constraint rather than a convention. Everything marked
**enforced** is a real constraint in `0002_core_schema` or `0003_parse_status`,
with a test in `tests/integration/test_core_schema.py`; the rest await the tables
they concern.

- **enforced** — `holding.value_usd >= 0` and `holding.shares >= 0`. 13F is
  long-only; a negative is a parse error, not a short position.
- **enforced** — `holding.put_call IN ('Put','Call')` or null.
- **enforced** — `holding.sshprnamt_type IN ('SH','PRN')`.
- **enforced** — `filing.value_multiplier IN (1, 1000)`.
- **enforced** — `filing.parse_status IN ('pending','ok','suspect','failed')`.
- **enforced** — a `suspect` filing has non-null `parse_notes`. A filing we do not
  fully believe has to say why; the status exists to send a person to a specific
  row of a specific document, which a bare flag cannot do.
- **enforced** — a 13F form type has a non-null `period_of_report`. Written as an
  implication on `form_type` rather than `NOT NULL`, because Form 4 shares this
  table and genuinely has no period.
- `insider_transaction.acquired_disposed IN ('A','D')`.
- `filing.period_of_report` is the last day of a calendar quarter, for 13F form
  types. Not enforced: amended and late filings do occasionally carry an
  off-quarter date, and rejecting them at the boundary would lose the filing
  rather than flag it.
- Exactly one non-amended `filing` per `(cik, period_of_report)` with
  `report_type = 'HOLDINGS'`; amendments chain through `amends_id`.
- **enforced** — every `holding` row's `period_of_report` equals its `filing`'s,
  by the composite FK above rather than by a trigger, because the
  denormalisation is otherwise a lie waiting to happen.
