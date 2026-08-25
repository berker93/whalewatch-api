# WhaleWatch — product spec

## What it is

WhaleWatch is a read-only API over two public SEC disclosure streams:

- **Form 13F-HR** — quarterly holdings reports from institutional investment
  managers with at least $100M in Section 13(f) securities. Berkshire, Bridgewater,
  Renaissance, every pension fund and family office over the threshold.
- **Forms 3/4/5** — ownership reports from corporate insiders: officers,
  directors, and 10%+ beneficial owners, filed within two business days of a
  transaction.

Both are free, both are filed as XML into EDGAR, and both are close to unusable
in their native form. A 13F is a CUSIP list with dollar values and no tickers. A
Form 4 is a transaction-code soup where a routine tax withholding looks exactly
like a director dumping stock. Neither tells you what changed since last time.

The product is the normalisation, the joins, and the deltas. Given a filer, the
API answers "what do they hold, what did they add, what did they exit". Given a
ticker, it answers "who owns it, who is accumulating, who just left". Given an
insider, it answers "what did they actually buy with their own money, as opposed
to what vested".

## Who it is for

The consumer is an application, not a human — a dashboard, a screener, a
research notebook. That is the reason for the shape of nearly everything below:
JSON over HTML, stable ids over display names, explicit `as_of` on every payload
rather than an implied "now".

Three users, in priority order:

1. **The WhaleWatch web client** (not in this repo). Drives the endpoint list.
2. **Notebook / script users** pulling bulk history for backtests. Drives
   pagination, and the decision that everything is filterable by `period`.
3. **Us, debugging ingestion.** Drives the raw-document archive and the fact
   that every derived number is traceable back to an accession number.

## What it answers

| Question | Shape of the answer |
| --- | --- |
| What does Berkshire hold as of 2025-09-30? | Holdings for one `(filer, period)`, valued, with tickers resolved |
| What did they buy and sell that quarter? | Diff of two consecutive periods for one filer |
| Who owns NVDA, and who is building a position? | Holders of one issuer at one period, plus per-holder delta |
| Which insiders bought their own stock last month? | Form 4 transactions filtered to open-market purchases |
| What are institutions crowding into this quarter? | Market-wide aggregate: net share change per issuer across all filers |

## Non-goals

Stated so that "wouldn't it be cool if" has somewhere to bounce off.

- **Not a trading signal, and not backtesting infrastructure.** We serve the
  data with its timestamps intact. What anyone concludes from it is theirs.
- **No real-time anything.** The upstream data is quarterly and lagged (see
  [ingestion spec](ingestion-spec.md)). A websocket over a 45-day-old dataset
  would be theatre.
- **No 13D/G, no S-1, no 8-K, no full-text search of filings.** Different
  parsers, different shapes, and each is its own project. 13G in particular is
  tempting — it is where the >5% stakes live — and is still out.
- **No non-US filers or non-US listings.** 13F covers Section 13(f) securities,
  which is a published list of US-exchange-listed equities and a handful of
  convertibles and options. There is no foreign holdings data to be had here at
  any effort level.
- **No user accounts, no portfolios, no alerts** in this service. If those
  happen they are a separate service with its own database that calls this one.

## Roadmap

The epic numbers are referenced from code comments; they are the unit of work,
not a schedule.

| Epic | Scope | State |
| --- | --- | --- |
| **0 — Foundation** | Settings, logging, app factory, health/readiness, SQLAlchemy, Alembic, this documentation | Done |
| **1 — Schema** | Every table in the [data model](data-model.md), migrated, with the invariants as constraints | Next |
| **2 — Ingestion** | EDGAR client, 13F and Form 4 parsers, loaders, Celery tasks, the CLI. The `ASYNC` ruff rules exist for this epic | |
| **3 — Read API** | The endpoints below, plus `pg_trgm` ticker/name search and the materialised views the aggregates read from | |
| **4 — Enrichment** | OpenFIGI CUSIP→ticker resolution, price enrichment, split adjustment | |
| **CLOUD-1** | Production Dockerfile stage, deploy | |

## API surface

Sketch, not contract — Epic 3 fixes the shapes. Present here so the data model
is designed against known access patterns rather than guessed ones.

Every collection is cursor-paginated. Every response carries the `period` or
`as_of` the data belongs to, at the top level, always — see *Presentation rules*.

```
GET  /health                                  liveness, no I/O
GET  /ready                                   readiness: postgres + redis

GET  /investors                               list/search 13F filers
GET  /investors/{slug}                        one filer, with periods available
GET  /investors/{slug}/holdings?period=        holdings for one period
GET  /investors/{slug}/changes?period=         diff against the prior period
GET  /investors/{slug}/periods                 which periods we hold, and their filing dates

GET  /stocks                                  search issuers by ticker or name (pg_trgm)
GET  /stocks/{ticker}                         one issuer
GET  /stocks/{ticker}/holders?period=          who holds it, ranked
GET  /stocks/{ticker}/holders/changes?period=  who added, trimmed, opened, exited
GET  /stocks/{ticker}/insiders                 Form 4 activity for this issuer

GET  /insiders/{cik}                          one insider
GET  /insiders/{cik}/transactions              their Form 4 transactions

GET  /market/flows?period=                    net institutional share change per issuer
GET  /market/crowded?period=                  most-held and most-added issuers
```

## Presentation rules

These are product decisions with teeth in the schema, so they live here rather
than in a style guide.

**Nothing is ever labelled "current".** A 13F holding is a snapshot of one day —
the last day of a calendar quarter — published up to 45 days later. Every
13F-derived field is returned with its `period`, and no endpoint has a default
of "latest" that hides which period the caller actually got. `period` is
optional in the query string and defaults to the most recent period *we have
ingested*, which the response then states explicitly.

**A "sale" is a decrease in reported shares.** Not a sale. The filer may have
been assigned, distributed in kind, or moved the position to an affiliate that
files separately. The API says `shares_delta: -1200000`; the word "sold" is the
client's problem.

**Unmapped is a value, not an absence.** Roughly a few percent of 13F CUSIPs
never resolve to a ticker — delisted names, obscure converts, OpenFIGI gaps. The
holding is returned with `ticker: null` and its dollar value intact. Dropping it
would silently shrink every portfolio total that contains one.

**Options are never summed with shares.** Positions carrying `put_call` are
reported on separate lines and excluded from share-count aggregates. The 13F
value for an option line is the notional of the underlying, so adding it to a
common-stock position inflates the total by the whole underlying exposure.

## Glossary

| Term | Meaning |
| --- | --- |
| **CIK** | Central Index Key. EDGAR's id for any filer, person or institution. Ten digits, zero-padded, stored as a string — leading zeros are significant and an `int` column loses them |
| **Accession number** | EDGAR's id for one submission, `0001234567-24-000123`. The unit of ingestion and the unit of idempotency |
| **CUSIP** | Nine-character US security identifier. What 13F reports instead of a ticker |
| **Issuer** | The company whose stock is held or traded |
| **Filer** | The institution submitting a 13F |
| **Insider** | A person or entity filing Forms 3/4/5 against an issuer |
| **Period** | The report date of a 13F: the last calendar day of a quarter, `YYYY-MM-DD` |
| **13F-HR** | The holdings report itself |
| **13F-HR/A** | An amendment to one — either a restatement or an addition |
| **Section 13(f) securities** | The SEC's published quarterly list of what must be reported. Exchange-traded equities, some ETFs, converts, and listed options |
