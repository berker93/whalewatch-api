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
  position_snapshot / position_change /       derived, recomputable, droppable
  implied_price / security_split /
  mv_consensus_holdings / mv_quarter_flows /
  mv_filer_summary
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
| `insider_transaction` | `(filing_id, line_number, transaction_date)` |
| `reporting_owner` | `(filing_id, insider_id)` |
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
  display_name  text                     -- curated, from data/investors.yaml
  manager_name  text                     -- curated
  category      text         check       -- value|activist|quant|macro|growth|multi_strategy
  country       char(2)                  -- ISO 3166-1 alpha-2, curated
  notes         text                     -- curated, for maintainers
  overlap       text         not null    -- successor | sum; default successor
  first_period  date                     -- earliest period we hold
  last_period   date                     -- latest period we hold

filer_cik
  id        bigint pk
  filer_id  bigint    fk -> filer, on delete cascade
  cik       char(10)  unique, not null
  priority  smallint  not null           -- position in the YAML list; highest wins an overlap
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

The curated columns (`0005_filer_curation`) come from
[`data/investors.yaml`](../data/investors.yaml), written by `seed-investors`.
They are nullable: the list is how a filer gets curated, not what makes a row a
filer. `name` stays ingestion's — the seed sets it once on insert as a
placeholder and never touches it again.

**A filer's CIKs can overlap in time.** Most multi-CIK filers hand over cleanly
from one entity to the next, but some have quarters in which two of their CIKs
each filed an original 13F-HR — during a reorganisation (Elliott 2020, Pershing
Square 2025–26) or permanently (Two Sigma's two advisers). Summing a filer's
filings for such a period double counts it in the first case and is correct in
the second. So `filer` carries an `overlap` policy (`successor`, the default,
or `sum`) and `filer_cik` a `priority` (the CIK's position in the YAML list);
see *Which filings count* below.

### Which filings count: `effective_filing`

Every filing is loaded as filed. Two views (`0006_effective_filing`) decide
which of them a per-filer read adds up, and **every read that sums holdings per
filer joins through `effective_filing`** rather than grouping on
`holding.filer_id` directly.

```
effective_filing_by_cik   (filing_id, filer_id, cik, period_of_report)
effective_filing          (filing_id, filer_id, cik, period_of_report)
```

`effective_filing_by_cik` resolves amendments within one CIK's filings for a
period: the latest-filed whole-period filing counts (the original `13F-HR`, or
a `restatement` amendment, which replaces it), plus every `new_holdings`
amendment filed after it. Only filings with loaded holdings count (`ok`,
`suspect`); an amendment with no `<amendmentType>` is left out rather than
guessed at.

`effective_filing` then applies the filer's policy across its CIKs. Under
`successor` only the highest-priority CIK with anything for the period counts —
the one listed last, which is the successor entity during a reorganisation. Under
`sum` all of them do. `audit-overlaps` compares the holdings of overlapping CIKs
and reports whether each policy matches what the filings show.

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
  name              text                    -- best name we have; written by resolve-cusips
  filed_name        text                    -- nameOfIssuer as the first filing wrote it
  ticker            text                    -- null when unresolved; see below
  figi              char(12)
  resolution_source text                    -- 'openfigi' | '13f_list' | 'manual' | '13f_column'
  resolved_at       timestamptz             -- when resolution_source was last set
  resolution_note   text                    -- why, for a manual resolution
  delisted_by       date                    -- first quarter end off the 13(f) list
  check (ticker IS NULL OR resolution_source IS NOT NULL)
```

`issuer` and `security.issuer_id` are **not in `0002_core_schema`**. A 13F
information table gives us a CUSIP and a filer-supplied issuer name and nothing
else, so the issuer table arrives with the `/stocks` search that needs it. Until
then `security.name` holds the name as filed — inconsistent, abbreviated
("BERKSHIRE HATHAWAY INC DEL"), and enough to display an unresolved security.
It is also what `/stocks` searches for now, so the trigram index is on it
(`ix_security_name_trgm`, `0017_query_indexes`) until `issuer.name` exists to
take it over.

`ticker` is nullable and stays nullable. CUSIP→ticker resolution fails for
delisted names and obscure instruments, and a `NOT NULL` here would force the
loader to either invent a ticker or drop the holding. It drops neither: an
unresolved security still gets a row, still gets held, and still shows up in
dollar totals.

`resolution_source` records which source won when `resolve-cusips` ran
(`0023_cusip_resolution`). `openfigi` is a US equity listing's ticker, FIGI and
name. `13f_list` is the SEC's name and nothing else, so that security is still
unresolved. `manual` is `resolve-cusip`, which nothing automatic overwrites.
Null means nothing named it beyond its filing. `13f_column` is reserved, because
the newer 13F information table may carry a FIGI column of its own. Recording
the source lets you find the rows that came from the same place when a mapping
turns out wrong. A ticker must name its source; a source need not come with a
ticker.

`name` is what the API shows, and resolution overwrites it: OpenFIGI's name,
else the 13(f) list's, else `filed_name`. The loader writes the filing's
`nameOfIssuer` to both columns on insert and never touches either again.
`filed_name` keeps the filer's spelling, which is sometimes stale
("FACEBOOK INC") and sometimes a note to self ("Full Truck Alliance - converted
at IPO"). A re-run that no longer finds a better name goes back to it.
Both are trigram-indexed: `name` for search, `filed_name` for the fuzzy match
below.

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
amendment_no      smallint                        -- cover page's amendmentNo; null on an original
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

`amendment_no` is the filer's own sequence number, stored as filed. It does not
decide the order amendments apply in — `filed_at` does, in `effective_filing` —
because a number typed by a filing agent is a claim about the order and the
acceptance timestamp is the order. It is kept because it is how everyone else
names an amendment, and because a gap in it is how a missing amendment shows up
(`audit-amendments` flags one).

`filed_at` matters more than it looks: it is what decides whether a 13F's dollar
values are in thousands or whole dollars. See the
[ingestion spec](ingestion-spec.md#the-whole-dollars-cutover).

`parse_status` and `parse_notes` are the record of the
[validation guards](ingestion-spec.md#the-whole-dollars-cutover). `pending` is
the default and means fetched but not parsed; `failed` means the document could
not be read at all and `parse_error` says why; `ok` means every guard passed.
The one that earns the column is `suspect`: parsed, **loaded**, and believed with
reservations, because some guard fired — a row count that disagrees with the
cover page, a total that does not sum, a position implying a share price no
security has, a negative quantity, or a CUSIP that is not nine letters and
digits. A suspect filing is flagged rather than rejected. Deleting it
leaves a gap that reads, to every query downstream, exactly like a manager who
filed nothing. What waits instead is publication: `position_snapshot`, below,
leaves out the periods a suspect filing counts toward.

`parse_status` is `text` with a `CHECK` rather than a native enum, per the rule
in `app/db/models/enums.py`: the vocabulary is ours and will grow, and
`ALTER TYPE ... ADD VALUE` has no reverse. `parse_notes` is `jsonb` because the
question it exists to answer is asked across a backfill rather than about one
row — `WHERE parse_notes @> '[{"kind": "implied_price"}]'` — and against a text
column that is a grep. Its `Decimal`s are stored as **strings**: a JSON number is
an IEEE 754 double, and a column recording a suspected 1000x error is a poor
place to round the figure a second time.

It is an array of findings, one object each:

```json
[
  {"kind": "entry_count", "severity": "error", "detail": "parsed 40 rows, cover page declares 41",
   "observed": "40", "expected": "41"},
  {"kind": "dropped_row", "severity": "warning", "row": 17, "cusip": "037833100",
   "detail": "value: expected a number (got 'N/A'); row not loaded"}
]
```

`kind` is the guard, and `severity` is what the finding means for the filing: any
`error` makes it `suspect`, and a `warning` — today only a row the parser dropped
for a reason no guard names — is evidence kept beside it.
`WHERE parse_notes @> '[{"severity": "error"}]'` is every filing a guard failed
on. Notes written before `severity` existed lack it until `backfill --force`
re-parses them from the archive.

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

### `position_snapshot`

Derived (`0009_position_snapshot`, reshaped by
`0011_position_snapshot_by_security`). The published portfolio: one row per
security per `(filer, period)`, summed over the filings `effective_filing` says
count, and what the per-filer read path serves. `holding` is what was filed;
this is what is true, once amendments are resolved.

```
filer_id          bigint    fk -> filer, on delete cascade
period_of_report  date      not null
security_id       bigint    fk -> security, on delete restrict
shares            numeric(20,4)  not null
value_usd         numeric(20,2)  not null
weight_pct        numeric(9,6)              -- percent of the period's value
source_filing_id  bigint    fk -> filing, on delete cascade, not null
suspect           boolean   not null        -- a suspect filing counts toward the period
computed_at       timestamptz  not null default now()
primary key (filer_id, period_of_report, security_id)
index (period_of_report, security_id)       -- the latest period; a stock's holders (0017)
```

Rebuilt by `whalewatch recompute`, a set of `(filer, period)` pairs at a time:
every pair, a filer's, a period's, or one. Each pair's rows are deleted and
inserted again inside one transaction, never upserted, since a rebuild can
remove rows a restatement no longer lists. `ingest-filing` and `backfill`
rebuild the pair each filing is filed under in the transaction that loads it,
so a loaded filing is a published one, unless the period is withheld (below).

**Common stock only.** Lines with a `put_call` and `PRN` principal amounts are
not rows here. An option's value is the notional of its underlying and a
principal amount is not a share count, so neither can be added to shares or be
a share of the portfolio. Leaving them out is also what makes the security a
key: one filing can report a CUSIP as stock, calls, puts and principal, but only
once as stock. The key is the natural one, with no surrogate `id` beside it.
Nothing refers to a snapshot row, and an `id` that every rebuild renumbers is no
use as a cursor.

The query that builds it is three CTEs. `winning_filings` reads which filings
count from `effective_filing`, and whether any of them is suspect. `agg` sums
their common stock per security. The final select adds `weight_pct` with
`SUM(value_usd) OVER (PARTITION BY filer_id, period_of_report)`. A period's
weights sum to 100, within rounding to six places, and are null only for a
period whose positions are all worth nothing.

`source_filing_id` is the filing a position was read from. When more than one
of the period's filings holds the security, as with an original and the
new-holdings amendment that added to it, or two CIKs under `sum`, it is the
latest-filed of them, the one that last changed the number.

**A period a suspect filing counts toward is withheld**, the whole period and
not just that filing. The rest of it is not a smaller correct answer: an
original without the addition that followed it is the portfolio before
confidential treatment expired, and one CIK of two under `sum` is half the book.
Either reads, a quarter later, as a manager who bought. Nor does a suspect
restatement fall back to the original it replaced — that was restated because
it was wrong. `recompute --include-suspect` publishes such periods anyway, with
`suspect` true on every row, so what went out unchecked is never
indistinguishable from what did not. A suspect filing that a later restatement
replaced counts toward nothing and withholds nothing.

### `position_change`

Derived (`0012_position_change`, exits added by `0013_position_change_exits`).
What each filer did to each position since its previous period: one row per row
of `position_snapshot`, plus an `exit` for each position the previous period had
and this one does not. Rebuilt from the snapshot by `whalewatch recompute` in
the same transaction. The snapshot is what a manager holds, and this is what
they did. The activity view and the `/changes` endpoints read it.

```
filer_id               bigint    fk -> filer, on delete cascade
period_of_report       date      not null
security_id            bigint    fk -> security, on delete restrict
action                 text      not null, check   -- new | add | trim | hold | exit
shares                 numeric(20,4)  not null     -- this period's, as the snapshot has them; 0 on an exit
value_usd              numeric(20,2)  not null
weight_pct             numeric(9,6)
prev_period_of_report  date                        -- the filer's previous published period
prev_shares            numeric(20,4)               -- null exactly when action = 'new'
prev_value_usd         numeric(20,2)
prev_weight_pct        numeric(9,6)
shares_delta           numeric(20,4)  not null     -- from zero when new
shares_delta_pct       numeric(28,6)               -- null when new
value_delta            numeric(20,2)  not null
weight_delta           numeric(9,6)                -- percentage points
is_split               boolean   not null default false  -- taken across a split (0026)
split_ratio            numeric(20,10)              -- that split's ratio; null exactly when not
suspect                boolean   not null          -- either end of the change is
computed_at            timestamptz  not null default now()
primary key (filer_id, period_of_report, security_id)
index (period_of_report, security_id) where action <> 'hold'   -- who traded a stock (0017)
```

The previous shares, value, weight and period come from `LAG` over one window,
`PARTITION BY filer_id, security_id ORDER BY period_of_report`. **Previous means
the filer's previous published period**, which is not what `LAG` alone returns.
`LAG` returns the last period in which the filer held the security. A stock sold
in Q2 and bought back in Q3 lags to Q1, which would make Q3 an add to a position
the filer did not have. So a lagged row counts only when it is from the filer's
previous period, and the position is `new` otherwise. Across a quarter with
nothing loaded, or one withheld for a suspect filing, the previous period is the
last one before the gap, and `prev_period_of_report` says which on every row.
Counting back one calendar quarter instead would make every position after a
withheld quarter `new`, which reads as the manager who bought everything. In
the filer's first period everything is `new` and `prev_period_of_report` is
null: it is the first period we have, not a quarter in which the filer bought
everything.

**`hold` is a band**: a change in shares within ±0.01% of the previous count.
Managers' reported share counts drift by a handful of shares between quarters,
from dividend reinvestment and rounding. Without the band, half of every
portfolio reads as an add or a trim. The band decides the action and nothing
else. A hold's `shares_delta` is the drift, not zero, and the action is judged
on shares alone, so a stock that doubled is still a hold, with a `value_delta`
to show for it.

**A `new` row's deltas count from zero, and an `exit`'s down to it.** So a
filer's `shares_delta` summed over every period is what it holds, and the
market-wide flow into a stock is a `SUM` that includes those who bought in
fresh and those who sold out. `shares_delta_pct` is null there
instead, since growth from nothing has no percentage. It is `numeric(28,6)`
because growth has no ceiling. Twenty-two integer digits hold any change
between two `numeric(20,4)` share counts, so no position, however strange, can
overflow the column and fail the rebuild for every filer.

`suspect` is true when either end of the change is: a change into or out of a
period published with `--include-suspect`. That includes a `new`, which is a
claim that the suspect filing did not list the security.

**An `exit` is a position of nothing.** `shares`, `value_usd` and `weight_pct`
are zero, and a `CHECK` holds them to it. The previous figures and
`prev_period_of_report` are from the period the security was last held in. The
deltas are that position, negative, with a `shares_delta_pct` of -100. Exits
are the reason this is a table rather than a query. An exit is the *absence* of
a row in the current period, and absence is not something you can index or
`LAG` from. Materialising it turns "what did they sell" from an anti-join over
two periods into a range scan.

So the query finds exits from the period before. For each snapshot row it takes
the filer's next period, by `LEAD` over the [`filer_period`](#filer_period)
view. It left joins the snapshot at that period for the same security, and
keeps the rows where the join misses. **A missing filing is not an exit.**
`LEAD` is null in a filer's latest period, and those rows are dropped before
the join. Otherwise everything a filer holds in its latest quarter would be an
exit until it filed the next one. A quarter with nothing filed, or one
withheld, is stepped over as it is for the previous period: the exit is dated
the next period published, and `prev_period_of_report` says when the position
was last seen.

`LEAD` shares the window that the previous period's `LAG` already sorts, so the
next period costs nothing more. A self-join, taking the smallest later period,
gives the same rows but pairs every period with every later one. `CROSS JOIN
LATERAL` with `LIMIT 1` probes once per period, but through a grouped view each
probe aggregates the whole of the next period. The anti-join is the real cost.
Exits roughly double the rebuild.

**Across a split, the previous shares are times the ratio** (`0026`). Compared
as filed, a 10-for-1 makes every holder of the stock a +900% `add`. A change
whose previous period is before a split in [`security_split`](#security_split)
and whose own period is on or after it is taken against `prev_shares × ratio`,
rounded to four places: a holder who held through it is a `hold`, one who also
bought is an `add` of what they bought, and `shares_delta` and
`shares_delta_pct` are in today's shares. `is_split` and `split_ratio` say so.
Across a quarter the filer skipped, every split in it counts, their ratios
multiplied. `prev_shares` stays as filed, so it is still the previous period's
snapshot row, and so do `value_delta` and `weight_delta`, which no split
changes. The cost is that a filer's `shares_delta` no longer sums to its
position across a split. Only an `add`, `trim` or `hold` is adjusted, a `CHECK`
says so: a `new` position has nothing to adjust, and an `exit` is the position
as filed, gone, which is what `mv_quarter_flows` prices it at. The split's
ratio comes from a `LATERAL` probe of `security_split`'s primary key per row.

Recomputed, never patched. An amendment landing months later changes a past
period and the comparison of the period after it, and nothing else: a period's
changes depend on its own snapshot and the filer's previous published one. So
a rebuild of some `(filer, period)` pairs rebuilds their changes and the
changes of each filer's next published period after them, which is next across
a gap or a withheld quarter, not the next calendar quarter. Only the changes
walk forward, because the next period's snapshot does not depend on this one.

### `implied_price`

Derived (`0025_implied_price`). Each security's quarter-end price in each
period, implied by the filings of the managers who held it, with no market data
vendor involved. Rebuilt from `position_snapshot` by `whalewatch recompute`, in
the same transaction as the snapshot and the changes.

```
security_id        bigint    fk -> security, on delete cascade
period_of_report   date      not null
price              numeric(20,8)          -- > 0; null when no observation is priced
observation_count  integer   not null     -- snapshot rows with shares: one per holder
unpriced_count     integer   not null     -- of those, worth $0; left out of the median
agreeing_count     integer   not null     -- priced, and within 2% of the median
confidence         text                   -- high | medium | low; null exactly when price is
suspect            boolean   not null     -- an observation is from a period published suspect
computed_at        timestamptz  not null default now()
primary key (security_id, period_of_report)
```

**Every snapshot row with shares is one observation**, of `value_usd / shares`.
A 13F value is the position's market value at the quarter end, so forty
managers holding a stock are forty observations of its closing price. The
snapshot is common stock only, so option notionals and bond principal never
reach it. A row with no shares observes nothing.

**The price is the median observation**, by `percentile_disc(0.5)`: always one
of the observations, the lower of the middle two when there is an even number,
in exact `numeric`. Holders who disagree with the rest are not noise around
the price. They are mistakes: a filer still reporting in thousands after 2023,
a share count from before a split against a value from after it, a strategic
stake carried at book value. On the five-year backfill, 6% of
`(security, period)`s held by two or more filers spread more than 2%, and about
400 by more than a factor of ten. A value-weighted price, `SUM(value) /
SUM(shares)`, follows the largest of those wherever it goes: Coinbase at $78 at
the end of 2022Q2 instead of $47.02, Magna at $66 instead of $47. The median
ignores any minority of them, however large.

**Confidence is agreement.** An observation agrees when it is within 2% of the
median, which lets through the rounding of a value reported in thousands
before 2023 and nothing like a split or a scale error. `high` is three or more
agreeing and a majority of the priced observations, `medium` is two and a
majority, `low` is everything else: one holder, or no majority. On the backfill
that is 55%, 14% and 31%. Most of the `low` prices are of securities only one
filer holds.

**$0 is not a price.** A position reported at nothing is a filer that did not
value it, so it is counted in `unpriced_count` and left out of the median. A
security every holder reported at $0, 726 `(security, period)`s on the
backfill, has a row with a null price and null confidence. Never a price of
zero, and a `CHECK` holds `price > 0`. Eight places, because a dollar value
over a large share count is small: the cheapest observation on the backfill is
$0.009.

**Rebuilt by security, not by filer.** The snapshot and the changes belong to a
`(filer, period)`, and a price belongs to a `(security, period)` across every
filer. A rebuild of some pairs reads which securities they held, before the
rebuild and after it, and rebuilds the price of each in its period. Before, so
that a security a restatement drops is repriced without it. The keys go to
Postgres as two arrays, unnested back into pairs, because one filer's every
period is tens of thousands of keys and asyncpg takes at most 32,767 bind
parameters. Prices do not walk forward with the changes: the next period's
snapshot is unchanged, so its prices are too. `recompute --all` rebuilds every
row, in under a second.

### `security_split`

Derived (`0026_security_split`). The stock splits in the data, detected from
the holders' filings: no corporate-action feed. Rebuilt by `whalewatch
recompute` after `implied_price`, which it reads, and before `position_change`,
which reads it. `whalewatch detect-splits` rebuilds it on its own.

```
security_id        bigint    fk -> security, on delete cascade
period_of_report   date      not null       -- the first quarter end after the split
numerator          integer   not null       -- new shares: 20 in 20-for-1, 1 in 1-for-10
denominator        integer   not null       -- old shares: 1 in 20-for-1, 10 in 1-for-10
ratio              numeric(20,10)  generated always as (numerator::numeric / denominator) stored
confidence         text      not null       -- high | medium | low
holders            integer   not null       -- filers holding it this quarter and the one before
holders_agreeing   integer   not null       -- whose shares moved by the ratio, within 25%
holders_exact      integer   not null       -- whose shares are the ratio's, to the share
shares_ratio       numeric(28,6)  not null  -- the median holder's shares now / before
price_ratio        numeric(28,10) not null  -- the implied price now / before
detected_at        timestamptz  not null default now()
primary key (security_id, period_of_report)
```

**A split is the holders agreeing and the price corroborating.** Each filer
that held the security in this quarter and the one before has a shares ratio,
now over before. The median of them, snapped to the nearest common fraction
within 25% (3:2, 2:1, 3:1, 4:1, 5:1, 10:1, 20:1, 25:1, 50:1, and the reverse
of each), is the candidate. Holders agree when their own ratio is within 25% of
it, not on it: these are hedge funds, which trade every quarter, and of AMZN's
35 holders across its 20-for-1, two moved by exactly 20 and nineteen by 16 to
25. At least two must agree, and a third of all of them. Then
`ratio × price_ratio`, which is one plus the stock's return over the quarter,
has to be between 0.4 and 1.6, and nearer one than the ratio is.

**The smaller the ratio, the more it takes.** A manager keeping a stock at a
fixed weight trims a quarter of it when the price rises a third, which is a
2-for-3's shares ratio with a price to match. On the backfill, the rule above
found over 900 splits of 3:2, 2:1 and their reverses, nearly none real. So
those need a holder **exact to the share**, within one share of the previous
count times the ratio: a holder that did not trade, which rebalancing lands on
by chance but not to the share. A 3:1 or 1:3 needs that or a majority of at
least three holders agreeing. 4:1 and beyond need neither. `confidence` is
`high` for an exact holder and three agreeing, `medium` for either, `low` for
two agreeing and no exact one.

**The ratio is the fraction, stored exactly.** The arithmetic on shares uses
`numerator` and `denominator`, so a 3-for-2 of 1,000 shares is 1,500, not
1,499.99999. `ratio` is generated from them, for reading.

**Rebuilt on the prices' keys and the quarter after.** A split belongs to a
`(security, period)` and every holder decides it, as a price does. A rebuild
detects at the securities and periods it reprices, and at the quarter after
each, since that quarter's detection compares its shares and price with this
one's: splits walk forward one quarter. When a split appears, goes or changes
its ratio, the change of every filer across it is rebuilt with the rest, found
in `position_change` by its previous period being before the split and its own
on or after.

What it does not detect: a split of any other ratio (ORLY's 15-for-1 is
detected as 10-for-1, and RYAAY's ADR ratio change as 3-for-1), and a split
the holders traded through too heavily to agree on (CTAS's 4-for-1, one holder
of five). Spin-offs, mergers and stock dividends are not splits at all. Where
the median holder's shares and the price moved apart by 2.5 times or more and
no split was detected, `detect-splits` lists the quarter as an outlier for a
person to look at. On the five-year backfill: 43 splits detected, among them
all 14 that showed as a +80% `add` for five or more holders before, and 71
outliers.

### `filer_period`

View (`0013_position_change_exits`). Every `(filer, period)` published in
`position_snapshot`, once each. Exits read it for "the filer's next period".

```
filer_id          bigint
period_of_report  date
suspect           boolean   -- published with a suspect filing: only after recompute --include-suspect
```

A period here is one the filer *published*, which is narrower than one it filed
for. A period withheld for a suspect filing is not one, and nor is a quarter
with nothing loaded, so both are stepped over rather than read as a quarter in
which everything was sold. Neither is a period whose filings hold no common
stock, only options or principal amounts: it has no snapshot rows, so a manager
who sold every share has no period to exit into until it files stock again. A
plain view, for `effective_filing`'s reasons: a `GROUP BY` over the snapshot's
primary key is cheap, and a view cannot go stale.

### `insider`, `reporting_owner` and `insider_transaction`

The Form 4 tables (`0028_insider_transaction`). Event-shaped where `holding` is
periodic: a holding is what a filer had at a quarter end, a transaction is what
an insider did on a day.

```
insider
  id      bigint pk
  cik     char(10)  unique, not null      -- rptOwnerCik; the key, never the name
  name    text      not null              -- as the latest filing spelled it

reporting_owner                           -- one insider on one Form 4
  filing_id             bigint  fk -> filing, on delete cascade
  insider_id            bigint  fk -> insider, on delete restrict
  is_director           boolean not null default false
  is_officer            boolean not null default false
  is_ten_percent_owner  boolean not null default false
  is_other              boolean not null default false
  officer_title         text
  other_text            text
  pk (filing_id, insider_id)

insider_transaction
  id                 bigserial
  transaction_date   date          not null
  filing_id          bigint        not null  -- fk (filing_id, insider_id) -> reporting_owner
  insider_id         bigint        not null
  line_number        smallint      not null  -- 1-based, in document order
  issuer_cik         char(10)      not null
  issuer_name        text          not null
  issuer_ticker      text                    -- issuerTradingSymbol as filed
  security_id        bigint        fk -> security, on delete set null
  security_title     text
  is_derivative      boolean       not null
  transaction_code   char(1)       not null  -- P, S, A, M, F, G, C, ...
  acquired_disposed  char(1)       not null  -- 'A' | 'D'
  shares             numeric(20,4)
  price_per_share    numeric(20,8)           -- null, never zero, for grants and gifts
  value_usd          numeric(20,2) GENERATED STORED  -- round(shares * price_per_share, 2)
  shares_owned_after numeric(20,4)
  is_10b5_1          boolean       not null default false
  footnotes          jsonb                   -- {"F1": "...", ...}: the ones this line cites
  pk (id, transaction_date)
  unique (filing_id, line_number, transaction_date)
  index (transaction_date DESC) WHERE transaction_code IN ('P','S')
```

**Roles live on `reporting_owner`, not on `insider`.** They are a relationship
with one issuer at one time: Buffett is a director of Berkshire and a 10% owner
elsewhere, and a CFO who joins the board is a director from that filing on.
Booleans rather than an enum, because a CEO on the board is both.

**A joint filing's trades are recorded once.** A Form 4 can name several
reporting owners, typically a fund, its general partner and the person behind
both, and reports the transactions once for all of them. Each
`insider_transaction` row is attributed to the first owner named, and every
owner is in `reporting_owner`. Fanning the row out to each owner would count
one purchase three times in every sum, and as three insiders in a cluster. The
composite FK holds a row's insider to an owner of its own filing, and carries
the integrity of `filing_id` and `insider_id` through `reporting_owner`'s FKs.

**The natural key is the line, not the values.** A Form 4 line has no identity
of its own, and one filing can report the same sale twice (500 shares at $41.20,
two tranches on one day) with every value equal. `line_number`, the position in
the document counting the non-derivative table then the derivative table, is
what tells them apart; a re-ingest of the same filing repeats it, so
`ON CONFLICT DO NOTHING` drops the copy. A `4/A` is a new `filing_id`: it
supersedes the original by deleting the original's rows, which cascade from the
filing.

**`transaction_date` is in both keys for partitioning.** It adds nothing to
either today, since a line has one date. Postgres requires the partition key in
every unique constraint on a partitioned table, and this table is the one that
will be partitioned on it (see Partitioning), so the keys are already the ones
that partitioning needs. `id` is a sequence rather than an `IDENTITY` column for
the same reason: Postgres 16 refuses identity on a partitioned table.

`transaction_code` is stored raw and interpreted nowhere but the read layer. `P`
(open-market purchase) and `S` (open-market sale) are the two that carry
information; `M` is an option exercise, `F` is shares withheld for tax, `A` is a
grant, `G` is a gift. Collapsing them into "bought"/"sold" at ingest time is
lossy and is how a vesting event becomes a headline. No check on it: the set is
the SEC's, not ours. The partial index is on the two that carry information,
which is the feed's default.

`price_per_share` is null, never zero, when the filing gives none, and the check
refuses a zero. A $0 purchase averages into "insiders bought at $0" the first
time anything computes a mean. `value_usd` is generated from it, so it is null
for every grant and gift and cannot disagree with the shares beside it. Eight
decimal places, like `implied_price`, for weighted-average prices.

`is_10b5_1` comes from a footnote the line cites, or from the form's checkbox
(since April 2023) when no line cites a plan footnote. The checkbox is one per
form, so it can't say which line it means when the footnotes don't. Before
April 2023 there is only the footnote. A sale under a pre-arranged plan was
decided months before the date on the form, which is the whole reason the flag
is on the row rather than left to the reader. See the
[parser](../app/ingestion/parsers/form4.py) for the rule.

`security_id` links the issuer to a `security` where the loader can tell which
one, from `issuer_cik`, `issuer_ticker` and `security_title`, and is null
otherwise. Null blocks nothing: the row is loaded and found by `issuer_cik`. A
CIK cannot always decide it alone (Alphabet is one CIK and two listed classes),
and there is no `issuer` table to hang it on (see `issuer` / `security`).
`ON DELETE SET NULL`, because the link is our interpretation, not the filing's.

### `pending_filing`

```
accession_no   char(20)     pk
cik            char(10)     not null            -- whose archive holds it
form_type      text         not null            -- 13F-HR | 13F-HR/A | 4 | 4/A
filing_date    date         not null
report_date    date                             -- null for a Form 4
discovered_at  timestamptz  not null default now()
status         text         not null default 'pending'  -- pending | failed | done
attempts       integer      not null default 0
last_error     text
```

The ingestion work queue. `discover-filings` writes every 13F it finds that is
not loaded; `ingest-filing` reads the CIK from here and writes back each attempt
(`done` in the loader's transaction, or `attempts + 1` and the error). Separate
from `filing` because a discovered filing has no `filed_at` or
`value_multiplier` yet, and inventing either is the 1000x error. No foreign key
to `filing` for the same reason. Rows are kept after `done`, so a filing that
took three tries keeps its history.

Form 4s share it. `crawl-form4` queues `4` and `4/A` rows, with the CIK of the
daily index line that matched (usually the issuer's), and `backfill` reads only
the `13F` rows.

### `crawl_watermark`

```
job         text         pk                       -- 'crawl-form4'
last_date   date         not null                 -- every day up to here read in full
updated_at  timestamptz  not null default now()
```

How far an incremental crawl has got (`0029_form4_crawl`). It moves only to
the day after itself, in the same transaction as that day's queue rows. A
failed day stops it, and a day crawled out of order leaves it alone. So
nothing at or before it was skipped, and `crawl-form4 --catch-up` starts the
day after. Kept as a table because "no Form 4s were queued for 25 December"
and "25 December was never read" leave the same queue behind.

### `sec_company_ticker`

```
ticker   text         pk                          -- as SEC spells it: BRK-B
cik      char(10)     not null
name     text         not null                    -- SEC's title
seen_at  timestamptz  not null                    -- latest import that listed it
index (cik)
```

SEC's `company_tickers.json` (`0029_form4_crawl`): about 10,000 listed
companies, a row per ticker with its company's CIK. It is the only place the
schema joins a security to a CIK. `crawl-form4` refreshes it on each `held` run,
and joins `security.ticker` to it with the share-class dot turned into SEC's
dash, to bound the crawl to held issuers. The insider loader can use it to link
an issuer CIK back to a security. A ticker missing from a later file is kept
with its old `seen_at`, because SEC lists only today's companies. A ticker given
to a new company moves to the new CIK. Not foreign-keyed to `security`, for
`sec_13f_security`'s reason.


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

### `ingestion_run`

```
id             uuid         pk default gen_random_uuid()   -- the run_id in the logs
job_name       text         not null                       -- the CLI verb, or backfill_13f
status         text         not null default 'running'     -- running | success | partial | failed
started_at     timestamptz  not null default now()
finished_at    timestamptz                                  -- null exactly while running
items_seen     integer      not null default 0
items_written  integer      not null default 0
error          text                                         -- one line per thing that went wrong
context        jsonb        not null default '{}'           -- the run's parameters
metrics        jsonb        not null default '{}'           -- what it measured, written at the end
index (job_name, started_at desc)
```

One row per execution of a job, so "did the backfill finish?" is a `SELECT`.
[`track_run`](../app/jobs/tracking.py) writes it as the job starts and fills in
the outcome as it ends, through sessions of its own. A job that fails
mid-transaction rolls back its own writes, not the record of why it failed.
`id` is generated before the row is written and bound to every log line as
`run_id`, so the row and the log lead to each other. A UUID rather than a
sequence because a UUID is one grep and `42` is every line with a 42 in it.

`context` is what the run was asked to do, and `metrics` is what it measured
doing it, beyond the two counters (`0015_matview_refresh`). A `refresh-views`
run records each view's duration and row count there.

`partial` is a run that finished with something left undone: failed filings,
an unreadable CIK, a backfill stopped short. `error` lists each, the failure
first when the run raised. A `running` row with no process behind it is a
`SIGKILL` or an OOM. There is no heartbeat, so its age is the only tell.

Not in any layer above. It describes the pipeline, not the filings, and nothing
reads it for correctness. Unlike `pending_filing` it only grows, hence the one
index, for its one question: the latest runs of a job.

### `figi_response_log`

```
id            bigint       pk
cusip         char(9)      not null                -- the idValue asked about
outcome       text         not null                -- found | not_found | error
response      jsonb        not null                -- the job's response, verbatim
requested_at  timestamptz  not null default now()  -- one request, one timestamp
index (cusip, requested_at desc)
```

Every answer OpenFIGI's mapping API has given, one row per CUSIP asked about
(`0021_figi_response_log`). [`OpenFIGIClient`](../app/ingestion/enrichment/figi.py)
commits each request's rows before sending the next, and reads the latest row
per CUSIP before asking again, so an interrupted or repeated resolution run
spends no quota on what it already has. `error` rows are kept but never reused.
They describe the request, not the CUSIP.

Append-only. Not foreign-keyed to `security`: it records what an external API
said about nine characters. Like `ingestion_run`, it is in no layer above, and
nothing reads it for correctness. `security.ticker` and `security.figi` are
the resolved values; this table is the evidence they were resolved from.

`delisted_by` (`0024_ticker_history`) flags a security that is no longer
listed: the first quarter end by which it had left the SEC's 13(f) list, which
is every exchange-traded equity a 13F must report. It is a bound rather than
the day, since the list is quarterly. It is null while the security is listed,
and for one no list has ever had, whose listing we cannot tell. `ticker` keeps
the last ticker a delisted security had.

### `security_alias`

```
id           bigint       pk
security_id  bigint       fk -> security, cascade
ticker       text         not null                -- as security.ticker spells it
valid_from   date                                 -- null: as far back as we know
valid_to     date                                 -- the first day it no longer was; null: still is
source       text                                 -- openfigi | manual | 13f_column
note         text
created_at   timestamptz  not null default now()
exclude using gist (security_id with =, daterange(valid_from, valid_to) with &&)
index (upper(ticker) text_pattern_ops)
```

Every ticker each security has traded under, over half-open date ranges
(`0018`, made a history in `0024_ticker_history`). The CUSIP is the stable key
and the ticker a label. Facebook's CUSIP is `FB` until 2022-06-09 and `META`
from then, and a delisted company's ticker can name another security later.
The ranges are what let `/v1/stocks/{ticker}` find the security a ticker meant
in the period being viewed, and let every row in a period show the ticker as
it was then. Nothing inside the database joins on a ticker.

One security has one ticker at a time, which the exclusion constraint
enforces. Two securities may hold the same ticker at once. That is a collision
that `cusip-coverage` reports, since either may be the stale one.

Written by `resolve-cusips` and `resolve-cusip`: a first ticker opens a range
with no start, and a different one ends the old range today and starts the
new one. Delisting ends the open range at `delisted_by`, and hands the ticker
to any other security holding it from an unknown start. Former tickers come
from `ticker-alias`, which trims the ranges it overlaps.

### `manual_review`

```
id                     bigint       pk
security_id            bigint       fk -> security, cascade   -- unresolved
suggested_security_id  bigint       fk -> security, cascade   -- resolved, name alike
suggested_ticker       text         not null                  -- its ticker at the time
matched_name           text         not null
suggested_name         text         not null
similarity             float8       not null                  -- pg_trgm, best of four
status                 text         not null default 'pending' -- pending | accepted | superseded
created_at             timestamptz  not null default now()
closed_at              timestamptz                            -- null while pending
unique (security_id, suggested_security_id) where status = 'pending'
```

The queue of fuzzy matches (`0023_cusip_resolution`). When neither OpenFIGI
nor the 13(f) list gives a security a ticker, `resolve-cusips` looks for
securities that have one under a similar name. It compares current and filed
names on both sides, and keeps up to three matches at `pg_trgm` similarity 0.8
or more. They are written here and never applied: a similar name is how one
share class comes to look like another. `resolve-cusip` closes a security's
pending rows, `accepted` where it agrees with the suggestion and `superseded`
where it does not. A later OpenFIGI resolution closes them `superseded` too.
Closed rows are kept.

### `sec_13f_security`

```
id                  bigint   pk
period_of_report    date     not null                 -- the quarter end the list is for
quarter             text     GENERATED STORED         -- '2026Q1', as on filing
cusip               char(9)  not null                 -- as printed; not check-digit validated
issuer_name         text     not null                 -- upper case, cut to 28 characters
issuer_description  text                              -- 'COM', 'CL A', 'CALL'; null if blank
status              text     not null                 -- listed | added | deleted
has_listed_options  boolean  not null                 -- the list's '*'
unique (period_of_report, cusip)
index (cusip, period_of_report desc)
```

The SEC's Official List of Section 13(f) Securities, one row per CUSIP per
quarter (`0022_sec_13f_security`), written by `import-13f-list`. Every quarter
is kept, because the list changes as securities are added and deleted. An
import replaces one quarter's rows and leaves the rest alone. `deleted` rows are
the SEC's `*D*`: on the previous list, not on this one, and still printed with
their names.

What it is for: an issuer name the SEC wrote, for a CUSIP OpenFIGI cannot match
([`issuer_names`](../app/ingestion/enrichment/issuer_names.py), which takes the
latest list that has the CUSIP). `resolve-cusips` writes that name to
`security.name` as `13f_list`. It has no tickers. Across 2020Q1 to 2026Q3 the
lists name 92.6% of the dev database's securities.

The published file repeats about a thousand CUSIPs a quarter from 2026Q1. These
are option rows whose synthetic CUSIPs collide across a trust's funds. The
parser collapses them before they reach the unique constraint; see
[its module](../app/ingestion/parsers/thirteen_f_list.py) for how each field is
chosen. Not foreign-keyed to `security`, for `figi_response_log`'s reason, and
most of its CUSIPs are held by no one we track. In no layer above.

## Partitioning

`insider_transaction` will be partitioned by `RANGE (transaction_date)`, once
the insider feed's date-ranged query exists to `EXPLAIN` (SEC-6). It is
unpartitioned as of `0028`, with `transaction_date` already `NOT NULL` and in its
primary key and its unique constraint, so the partitioning is a data move.

`holding` has a partitioning plan too, and it is deferred until it
hurts. Rough shape: ~4,000 filers × ~200 positions × 4 quarters is low millions of
rows a year, which Postgres does not care about. When it does, it partitions by
`RANGE (period_of_report)` — every read path already filters on it, so partition
pruning is free rather than a rewrite.

Written down now so that when the day comes, `period_of_report` is already
`NOT NULL` on `holding` and already in every unique constraint, which is what
partitioning requires and what is expensive to add later.

## Materialised views

Three aggregates of the derived tables (`0014_materialised_views`), for the
reads that would otherwise aggregate hundreds of thousands of rows per request
over inputs that change four times a year. Each has a live query in
[`app/derived/views.py`](../app/derived/views.py) that computes the same rows
from the tables as they are now. The `check_*` tests hold each view to its live
query, row for row.

```
mv_consensus_holdings                  unique (period_of_report, security_id)
  period_of_report, security_id
  holder_count        bigint            -- filers holding it
  total_value_usd     numeric
  total_shares        numeric
  avg_weight_pct      numeric           -- over its holders, 6 places
  median_weight_pct   numeric           -- over its holders, 6 places
  value_rank          bigint            -- 1 = most dollars held in the period; ties share
  suspect             boolean           -- a holder's period was published unchecked

mv_quarter_flows                       unique (period_of_report, security_id)
  period_of_report, security_id
  bought_value_usd    numeric           -- new + add, traded dollars (below)
  sold_value_usd      numeric           -- trim + exit, positive
  net_value_usd       numeric           -- bought - sold
  net_shares          numeric           -- sum(shares_delta), holds included
  new_positions, exits, buyer_count, seller_count     bigint
  suspect             boolean

mv_filer_summary                       unique (filer_id, period_of_report)
  filer_id, period_of_report
  portfolio_value_usd numeric           -- common stock, as position_snapshot holds it
  position_count      bigint
  top10_weight_pct    numeric           -- the ten largest positions' share of the book
  turnover_pct        numeric           -- null in the filer's first period
  suspect             boolean           -- this period or the one it is compared with

mv_year_flows                          unique (period_of_report, security_id)
  period_of_report                      -- the last of the year's four quarters
  security_id
  (mv_quarter_flows' columns)           -- dollars: the four quarters' summed
                                        -- counts: distinct filers over the four

mv_filing_feed                         unique (filer_id, period_of_report, filing_id)
  filing_id, filer_id, period_of_report, filed_at
  position_count      bigint            -- the period's, as published
  largest_security_id, largest_action, largest_shares_delta,
  largest_traded_value_usd              -- the period's largest trade; null in a first period
```

**A year keeps gross and net apart.** Bought in Q1 and sold in Q3, a position
nets to about nothing over the year: right for net flow, wrong for the year's
top buys. So `mv_year_flows` keeps `bought_value_usd` (every quarter's buying,
gross) beside `net_value_usd`, as the quarter does. Its dollars are the sums of
the four quarters' rows, already rounded to cents, so a year adds up to its
quarters exactly. Its counts are counted again from `position_change`,
distinct per filer: one that bought in two of the quarters is one buyer. The
year is the four quarters ending at the period, and one ends at every period
with flows.

**Flows and turnover count traded dollars, not `value_delta`.** `value_delta`
includes the price move on every share held throughout. Summed as flows, a
stock that doubled would read as bought by every holder who did nothing, and a
trim during a rally as negative selling. So each change is valued as the shares
it bought or sold, times the period-end price. An exit has no price in its own
period, so it is valued at the price it was last held at, which makes it its
whole previous value. A new position is its whole value. A `hold` trades
nothing, drift included. Period-end prices are the only ones a 13F has, so this
is an estimate: a position bought at $50 and worth $80 at quarter end counts as
$80 of buying.

**Turnover is `sum(traded) / 2 / previous portfolio value`**, in percent. There
is no single standard. This one is half of bought plus sold, so a manager who
sold half the book and bought the same again turned over half of it. The
previous portfolio value is the sum of the period's `prev_value_usd`, since
every position of the previous period is a change row, held on or exited.
Across a gap the previous period is the one before the gap, so turnover covers
every quarter since. The formula is also the column's comment in the database,
so `\d+ mv_filer_summary` in psql says which turnover this is.

**A filer's first period is not a flow.** Every position in it is `new`, and
`prev_period_of_report` is null. `mv_quarter_flows` leaves those rows out. If it
counted them, every filer added to the universe would be a market-wide buying
spree in its first quarter.

**Average and median weight both** are over the stock's holders, because they
tell different stories. A stock held at 20% by one manager and 0.5% by thirty
others averages 1.13% and has a median of 0.5%. The median is
`PERCENTILE_CONT(0.5)`, which is double precision only. Converted back to
`numeric`, it is the exact midpoint: a weight has at most nine significant
digits, and the conversion keeps fifteen.

**Refreshed by `whalewatch refresh-views`, after whatever publishes, not on a
timer.** `recompute` runs it after every rebuild, `ingest-filing` after a load
that publishes, and `backfill` once at the end, never after each filing. A
refresh partway through a backfill would publish a quarter with a third of its
filers in it. Each of them is its own `refresh-views` run, recorded after the
publishing run has committed, with that run's id as `after_run_id`. Until a
refresh, each view is as of the last one.

The refresh is `CONCURRENTLY`, so reads go on through it: Postgres builds the
new rows beside the old ones and applies the difference, and readers see the
old rows until the commit. That needs a unique index on plain columns, with no
`WHERE`, on every view. It is more work than a plain refresh, which locks the
view against every read until the commit, and the extra cost is worth paying
to never block the API. `--no-concurrent` takes the plain path for a database
nobody is reading. The refresh holds the recompute lock, so no rebuild commits
between one view and the next. A view that was never populated is refreshed
plainly once. The migration creates them `WITH DATA`, so this happens only
after a `REFRESH ... WITH NO DATA`.

**A view that reads another is refreshed after it**, in an order read from the
catalog (`pg_depend`, followed through plain views), not declared anywhere.
`refresh-views --view` refreshes one view and every view that reads it. None of
the three reads another yet.

```
matview_refresh                        pk (view_name)
  view_name      text                  -- pg_matviews.matviewname
  refreshed_at   timestamptz not null  -- clock_timestamp() as the refresh finished
  run_id         uuid                  -- the refresh-views run; null only from tests
```

**When each view was last refreshed** is in `matview_refresh`
(`0015_matview_refresh`), one row per view, replaced in the transaction that
refreshes it. The row and the view's rows commit together, so a reader never
sees a refresh time newer than the data. This is what lets a response built
from a view say honestly when its aggregates were computed, which makes it a
product feature, not housekeeping. `clock_timestamp()`, not `now()`: in a
refresh of several views, `now()` would give each one the time the first
began. Created empty: `0014` filled the views without recording when, so a
view with no row has a last refresh that is unknown, and is reported as
unknown. `run_id` is not a foreign key, because nothing depends on
`ingestion_run` for correctness and this row is what the API serves.

Alembic does not model views. They are `op.execute()` in a hand-written migration
with a real `downgrade`, like everything else in
[the migration rules](../README.md#migrations). Their handles in
`app/derived/views.py` are `table()` constructs, kept off `Base.metadata` so that
autogenerate does not draft a `CREATE TABLE` for them.

## Invariants

The ones worth a constraint rather than a convention. Everything marked
**enforced** is a real constraint in the migration named beside it, or in
`0002_core_schema` or `0003_parse_status` where none is named, with a test in
`tests/integration/test_core_schema.py`; the rest await the tables they concern.

- **enforced** — `holding.value_usd >= 0` and `holding.shares >= 0`. 13F is
  long-only; a negative is a parse error, not a short position.
- **enforced** — `holding.put_call IN ('Put','Call')` or null.
- **enforced** — `holding.sshprnamt_type IN ('SH','PRN')`.
- **enforced** — `filing.value_multiplier IN (1, 1000)`.
- **enforced** — `filing.parse_status IN ('pending','ok','suspect','failed')`.
- **enforced** — `pending_filing.status IN ('pending','failed','done')` and
  `attempts >= 0` (`0007_pending_filing`).
- **enforced** — `ingestion_run.status IN ('running','success','partial','failed')`,
  `finished_at` is null exactly when the status is `running`, a `partial` or
  `failed` run has a non-null `error`, and both counters are `>= 0`
  (`0010_ingestion_run`).
- **enforced** — a `suspect` filing has non-null `parse_notes`. A filing we do not
  fully believe has to say why; the status exists to send a person to a specific
  row of a specific document, which a bare flag cannot do.
- **enforced** — a 13F form type has a non-null `period_of_report`. Written as an
  implication on `form_type` rather than `NOT NULL`, because Form 4 shares this
  table and genuinely has no period.
- **enforced** — `insider_transaction.acquired_disposed IN ('A','D')`, its
  `price_per_share` is positive or null, its quantities are not negative,
  `line_number > 0`, `footnotes` is a JSON object, and its insider is a
  reporting owner on its own filing (`0028_insider_transaction`).
- `filing.period_of_report` is the last day of a calendar quarter, for 13F form
  types. Not enforced: amended and late filings do occasionally carry an
  off-quarter date, and rejecting them at the boundary would lose the filing
  rather than flag it.
- Exactly one non-amended `filing` per `(cik, period_of_report)` with
  `report_type = 'HOLDINGS'`; amendments chain through `amends_id`.
- **enforced** — every `holding` row's `period_of_report` equals its `filing`'s,
  by the composite FK above rather than by a trigger, because the
  denormalisation is otherwise a lie waiting to happen.
- **enforced** — `position_snapshot` has one row per `(filer_id,
  period_of_report, security_id)`, and `weight_pct` is between 0 and 100
  (`0011_position_snapshot_by_security`).
- **enforced** — `position_change.action IN ('new','add','trim','hold','exit')`,
  and the action is `new` exactly when `prev_shares` is null
  (`0012_position_change`). An `exit` has zero shares, value and weight
  (`0013_position_change_exits`). A row whose action and figures disagree has
  every reader believing one of the two.
- **enforced** — `implied_price.price > 0`, its `confidence IN ('high',
  'medium','low')` and null exactly when the price is, and its counts nest:
  agreeing within priced within observed (`0025_implied_price`).
- **enforced** — `security_split`'s ratio is a split: both sides positive and
  different. Its `confidence IN ('high','medium','low')`, and its holders nest:
  exact within agreeing within all (`0026_security_split`).
- **enforced** — `position_change.is_split` exactly when `split_ratio` is not
  null, and only on an `add`, `trim` or `hold` (`0026_security_split`).
- **enforced** — every materialised view has a unique index on plain columns,
  with no `WHERE`, which is what `REFRESH ... CONCURRENTLY` needs
  (`0014_materialised_views`). `test_materialised_views.py` checks every
  materialised view in the database, so one added without an index fails there
  rather than on its first refresh.
