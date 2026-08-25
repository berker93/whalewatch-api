# WhaleWatch — data model

Target state for Epic 1. Nothing here is migrated yet; when it is, this document
and `app/db/models/` move together or the document is wrong.

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
| `filer`, `issuer`, `insider` | `cik` |
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

## Tables

### `filer`

The institution behind a 13F.

```
id            bigint pk
cik           char(10)     unique, not null
name          text         not null      -- as reported on the latest cover page
slug          citext       unique        -- 'berkshire-hathaway', stable, ours
first_period  date                       -- earliest period we hold
last_period   date                       -- latest period we hold
```

`slug` is the public identifier in URLs, and it is generated once and then frozen
even if the filer renames itself. A slug derived live from `name` is a URL that
changes under a client when a fund rebrands.

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
  issuer_id         bigint     fk -> issuer
  ticker            text                    -- null when unresolved; see below
  figi              text
  resolution_source text                    -- 'openfigi' | '13f_column' | 'manual'
  resolved_at       timestamptz
```

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
accession_no      char(20)     unique, not null   -- dashed
cik               char(10)     not null           -- the filer, may be issuer or insider
form_type         text         not null           -- '13F-HR', '13F-HR/A', '4', ...
period            date                            -- report date; null for Form 4
filed_at          timestamptz  not null
raw_document_id   bigint       fk -> raw_document
amends            bigint       fk -> filing       -- self-ref, set on /A forms
report_type       text                            -- 13F cover page: HOLDINGS | NOTICE | COMBINATION
parsed_at         timestamptz                     -- null = fetched but not yet parsed
parse_error       text
```

`filed_at` matters more than it looks: it is what decides whether a 13F's dollar
values are in thousands or whole dollars. See the
[ingestion spec](ingestion-spec.md#the-whole-dollars-cutover).

`report_type` is on the filing, not derived at query time, because a `13F NOTICE`
contains no holdings and a `13F COMBINATION REPORT` contains only the subset the
filer manages directly. Aggregating without it double-counts positions that two
affiliated managers both disclose.

### `holding`

One line of a 13F information table.

```
id                bigint pk
filing_id         bigint    fk -> filing, not null
security_id       bigint    fk -> security, not null
filer_id          bigint    fk -> filer, not null      -- denormalised from filing
period            date      not null                    -- denormalised from filing
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

`filer_id` and `period` are denormalised off `filing` because every read path
filters on them and the alternative is a join on every query in the service.

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
`RANGE (period)` — every read path already filters on `period`, so partition
pruning is free rather than a rewrite.

Written down now so that when the day comes, `period` is already `NOT NULL` and
already in every unique constraint, which is what partitioning requires and what
is expensive to add later.

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

The ones worth a constraint rather than a convention:

- `holding.value_usd >= 0` and `holding.shares >= 0` — 13F is long-only; a
  negative is a parse error, not a short position.
- `holding.put_call IN ('Put','Call')` or null.
- `holding.sshprnamt_type IN ('SH','PRN')`.
- `insider_transaction.acquired_disposed IN ('A','D')`.
- `filing.period` is the last day of a calendar quarter, for 13F form types.
- Exactly one non-amended `filing` per `(cik, period)` with
  `report_type = 'HOLDINGS'`; amendments chain through `amends`.
- Every `holding` row's `period` equals its `filing`'s `period`. A trigger, or a
  composite FK on `(filing_id, period)`, because the denormalisation above is
  otherwise a lie waiting to happen.
