# WhaleWatch — ingestion spec

How filings get from EDGAR into the tables in the [data model](data-model.md).
Target state for Epic 2; the modules under `app/ingestion/` are stubs until then.

If you read one section of this document, read
[the two traps](#the-two-traps). They are the reason the numbers can be
plausible and wrong at the same time.

## Sources

| Source | What it gives us |
| --- | --- |
| `data.sec.gov/submissions/CIK##########.json` | Every filing a CIK has made, newest first. How we discover 13Fs for a known filer |
| `www.sec.gov/Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx` | Everything filed on one day, by form type. How we discover Form 4s, whose filers we do not know in advance |
| `www.sec.gov/Archives/edgar/data/{cik}/{accession}/…` | The documents themselves |
| `www.sec.gov/divisions/investment/13flists` → `/files/investment/13flist……` | The quarterly Section 13(f) securities list — what is reportable in a given quarter, with the SEC's issuer names |
| OpenFIGI `POST /v3/mapping` | CUSIP → ticker + FIGI |

Everything but OpenFIGI is unauthenticated and free. None of it has a rate limit
you want to discover empirically.

## Talking to EDGAR

**One client, one throttle.** `app/ingestion/edgar/client.py` is the only thing
in this codebase that opens a connection to sec.gov. It owns the `User-Agent`,
the rate limiter and retries.

**User-Agent.** SEC's fair-access policy wants a real, monitored contact address
on every request and blocks traffic that omits or fakes one. It is built from
`settings.sec_user_agent`, which is derived from `SEC_CONTACT_EMAIL` — required,
no default, and the reason the app refuses to boot without it.

**Rate limit.** SEC publishes a ceiling of 10 requests/second across all of
sec.gov and data.sec.gov. `SEC_RATE_LIMIT_PER_SECOND` defaults to 8 and
`Settings` rejects anything above 10, so the ceiling cannot be raised by
accident. The limiter is process-global and, once there is more than one worker,
must be Redis-backed: four Celery workers each politely doing 8/s is 32/s, and
the block is by IP.

**Backoff.** A 403 from EDGAR usually means the User-Agent, not the rate — retrying
it faster makes it permanent. Treat 403 as fatal for the run and log it loudly;
retry 429 and 5xx with exponential backoff and jitter.

Not every 403 is EDGAR. Parts of the archive (`daily-index/`) are served from
S3, which answers a key that does not exist with a 403 and an XML
`AccessDenied` body. The client raises that as `EdgarNotFound`, a 404, rather
than waiting out a block that is not there.

**Archive before parse.** Every fetched document is written to the raw store
(`app/storage`) before a parser sees it, uncompressed and unmodified, under
`raw/13f/{cik}/{accession_no}/{filename}` — cover page, information table and
EDGAR's `index.json` side by side. Writes are once-only: a key that exists is
left alone unless `--force`. The store is local disk under `./data/raw/` in
development and an S3-compatible bucket (R2 included) when deployed. Parsing is then a pure function from
stored bytes to rows, re-runnable at any time without touching the network,
which is what makes a parser bug a `whalewatch recompute` rather than a week of
re-crawling.

## Discovery

**13F** is pull-based, per filer. We keep a list of tracked filer CIKs; for each,
fetch the submissions JSON, take every `13F-HR` and `13F-HR/A`, and enqueue any
accession number not already loaded (`filing.parse_status` `ok` or `suspect`)
into `pending_filing`. That is `discover-filings`
([app/ingestion/discovery.py](../app/ingestion/discovery.py)). `backfill`
drains the queue several filings at a time (see [Orchestration](#orchestration)),
and `ingest-filing` does one accession number.

It is a set difference against what is loaded, never "everything since the
newest filing we hold". The latter forgets: a filing that failed to load three
weeks ago is older than yesterday's, so it is never asked about again. The set
difference finds it on the next run with no special case. It also finds a
deleted row and an amendment filed late for a closed period. The window defaults
to five years of filing dates; `--all` reads a CIK's whole history.

**Form 4** is push-based, per day. There is no useful "all insiders" list, so
`crawl-form4` ([app/ingestion/insider_discovery.py](../app/ingestion/insider_discovery.py))
walks the daily form index (`form.YYYYMMDD.idx`) a day at a time, keeps form
types `4` and `4/A`, and enqueues into the same `pending_filing`, through the
same set difference as 13Fs. `backfill` takes only the 13F rows. Backfilling
insiders means walking every day in the range: one request per business day.

- **Bounded to a universe.** EDGAR has about 1,700 Form 4s on a busy business
  day. `FORM4_UNIVERSE=held` keeps the issuers that were at least
  `FORM4_MIN_WEIGHT_PCT` (default 1) percent of a tracked portfolio in the last
  four quarters. That was ~740 issuers and ~90–125 filings a day in October 2026.
  Held securities are matched to CIKs through SEC's `company_tickers.json`
  (`sec_company_ticker`), which covers 98% of held value. What it misses is
  mostly ETFs, which have no insiders. `sp500` reads CIKs from a CSV.
- **One line per party.** The index lists a Form 4 under its issuer and under
  each reporting owner, so a line matches on the issuer's CIK whoever filed it.
  A tracked company that is a 10% owner of another brings in that company's
  Form 4s too, and the loader decides.
- **Fixed width.** Form types (`SC 13D`) and names (`667, L.P.`) contain spaces.
  The form type and name are cut at the header's column offsets. CIK, date and
  path never contain spaces, so they are the last three tokens. Only the
  header's first line is usable: its second is wrapped.
- **A missing index is a 403, not a 404.** `daily-index/` is served from S3,
  which answers a missing key with `403 AccessDenied`. The EDGAR client
  recognises that body as not-found (`EdgarNotFound`) rather than a block. The
  crawl never asks for a missing file in the first place: each quarter's
  `index.json` listing says which days have one. A weekday not listed is a
  holiday if a later day is listed. If none is, it is not published yet (indexes
  appear at about 10pm New York time), and the walk stops there.
- **A watermark.** `crawl_watermark` holds the last day read in full. It
  advances only to the day after itself, and only after that day's queue rows
  are committed in the same transaction. A failed day stops the walk, and a day
  crawled out of order does not move it. `--catch-up` starts the day after it.

## Parsing

### 13F

Two documents per filing:

- **`primary_doc.xml`** — cover page and summary. Gives `periodOfReport`,
  `reportType` (`13F HOLDINGS REPORT` / `13F NOTICE` / `13F COMBINATION REPORT`),
  the filer's name and CIK, `tableEntryTotal` and `tableValueTotal`.
- **the information table XML** — one `<infoTable>` per position:
  `nameOfIssuer`, `titleOfClass`, `cusip`, `value`, `shrsOrPrnAmt`
  (`sshPrnamt` + `sshPrnamtType`), `putCall`, `investmentDiscretion`,
  `otherManager`, `votingAuthority`.

Parse the cover page first and *check it*: `tableEntryTotal` is the filer's own
count of its rows. If the information table yields a different number, the parse
is wrong or the document is truncated, and the filing should fail rather than
load partially. Same for `tableValueTotal` against the sum of `value` — which is
also a free check on the units question below.

A `13F NOTICE` has no information table at all; it means "everything I hold is
reported by another manager". It is a valid filing and an empty holdings set, not
a failure.

### Form 4

One `ownershipDocument` XML, with `nonDerivativeTransaction` and
`derivativeTransaction` elements. Each carries a `transactionCode`,
`transactionShares`, `transactionPricePerShare`, `transactionAcquiredDisposedCode`
and `sharesOwnedFollowingTransaction`. `reportingOwner` gives the insider's CIK
and their relationship (officer/director/10% owner); `issuer` gives the issuer's
CIK and ticker — the one place in this pipeline a ticker arrives for free.

`transactionPricePerShare` is legitimately absent for grants (`A`) and gifts
(`G`). Null it; do not zero it. A zero price averages into "insiders bought at
$0" the first time someone computes a mean.

`parse_form4` ([app/ingestion/parsers/form4.py](../app/ingestion/parsers/form4.py))
is pure and returns the issuer, every reporting owner in document order, and
every transaction line, numbered the way `insider_transaction.line_number` is.
What it decides:

- **Prices.** `0` and an absent price both become null, and so does a zero
  strike on an RSU. A weighted-average price keeps the range its footnote gives
  ("at prices ranging from $53.66 to $54.58") as `price_low`/`price_high`. A
  price given only as a range (no value, or a value like `10.00-11.00`) has a
  null price and the range.
- **10b5-1.** `aff10b5One` (on the form since April 2023) is one checkbox per
  form, not per line. A line is flagged when a footnote it cites names Rule
  10b5-1 in a sentence that doesn't negate it. If no line cites such a footnote
  and the box is ticked, every line is flagged, since nothing says which one
  the box means. In the 280 Form 4s queued for 2026-10-08 to 2026-10-13, 247
  plan sales were footnoted and 6 had only the checkbox. The exercises filed
  beside footnoted sales are the reason the checkbox alone doesn't flag every
  line.
- **Footnotes.** Each line carries the footnotes it cites anywhere inside it,
  with their text. The document's full set is kept too.
- **Holdings lines are skipped.** `nonDerivativeHolding` and
  `derivativeHolding` report a balance and no trade, and take no line number.
- **A bad line fails the filing.** A missing date, code or A/D flag raises
  `FilingParseError` and names the line. A 13F has a checksum that accounts for
  a dropped row; a Form 4 has nothing, so a dropped line would be a trade that
  silently never happened.
- **Forms 3 and 5 are refused.** They share the `ownershipDocument` schema.

The document's filename is not fixed. It depends on the filing agent
(`form4.xml`, `wk-form4_*.xml`, `edgardoc.xml`, `ownership.xml`...), so
`ingest-form4` finds it in the filing's `index.json` and confirms its root is
`ownershipDocument`. Every filing in that week had exactly one `.xml` file.
The acceptance time comes from the filing's SGML header
(`{accession}.hdr.sgml`, `<ACCEPTANCE-DATETIME>`, New York time), which agrees
with EDGAR's index page. The submissions JSON's `acceptanceDateTime` for the
same NVIDIA 4/A is eight hours later, not four.

The loader ([app/ingestion/loaders/insider.py](../app/ingestion/loaders/insider.py)):

- **Lines are attributed to the first owner**, and every owner gets a
  `reporting_owner` row. `insider` is upserted on CIK, keeping the name the
  latest-filed filing used.
- **Derivative lines keep their own shares and price**: how many options or
  RSUs, and what was paid for each. Not the underlying shares and the strike,
  which would value an exercise at money nobody paid.
- **A 4/A supersedes its original**, found by issuer, filing day and a shared
  owner. Its lines are deleted, whichever loads first.
- **`security_id`** is linked from the issuer CIK via `sec_company_ticker`.
  On the dev week, 272 of 280 filings linked, and every linked line's ticker
  matched the filing's own symbol. The other 8 were Lennar, whose symbol
  is `LEN, LEN.B`.

## The two traps

### The 45-day 13F lag

Managers file within **45 days of the end of each calendar quarter**:

| Period ends | Due |
| --- | --- |
| Mar 31 | May 15 |
| Jun 30 | Aug 14 |
| Sep 30 | Nov 14 |
| Dec 31 | Feb 14 |

Two consequences, both of which have to be visible in the API rather than known
by the developer:

**The freshest 13F data is between 45 and 135 days old.** On May 14 the newest
period available is December 31 — four and a half months of positions you cannot
see. The API therefore never says "current holdings", every payload states its
`period`, and no default silently substitutes the latest period for the one the
caller meant. See *Presentation rules* in the [product spec](product-spec.md).

**A past period can change after you have built it.** Two mechanisms:

- **Amendments.** `13F-HR/A` can restate a whole period or add to it, and can
  arrive years later. The cover page distinguishes a restatement from an
  addition, and getting that backwards either doubles a position or discards one.
- **Confidential treatment.** A manager can ask the SEC to withhold positions —
  typically while still accumulating — and file them later once the request
  expires or is denied. The position then appears in an amendment, dated to the
  original period. This is not rare among the filers anyone actually wants to
  watch.

So ingestion is not write-once. Re-ingesting a `(filer, period)` must be an
upsert keyed on the natural keys, `position_change` for that period and the next
must be recomputable on demand, and nothing downstream may assume a period is
final. `filing.filed_at` is retained so "what did we know, and when" is
answerable — a backtest that uses a position on a date before it was disclosed is
using information nobody had.

### The whole-dollars cutover

**The `value` field in the 13F information table changed units.**

- Filings submitted **before 2023-01-03**: `value` is in **thousands of dollars**.
  A $1.2B position reads `1200000`.
- Filings submitted **on or after 2023-01-03**: `value` is in **whole dollars**.
  The same position reads `1200000000`.

Getting this wrong is a **1000×** error, and 1000× errors do not announce
themselves — every filer in a mis-parsed quarter is wrong by the same factor, so
rankings, percentages and quarter-over-quarter *shapes* all look completely
normal. It surfaces months later as "why is this fund's AUM $40 million".

Three rules:

**1. Key off the filing date, not the period.** The convention follows the
submission, not the quarter it describes. An amendment filed in 2024 for a 2019
period is in **whole dollars**, even though the original filing for that same
period was in thousands. A `period < 2023` test therefore gets amendments exactly
backwards, and amendments are precisely the filings that are hard to notice.

**2. Normalise at parse time.** `holding.value_usd` is whole dollars for every
row in the table, always. The alternative — store what the filing said, convert
on read — requires every consumer to know about the cutover, and one of them will
not.

**3. Verify, do not assume.** The boundary above is what EDGAR's rules say; the
filings are what you actually have. `app/ingestion/normalisation.py` applies the
multiplier and then runs five guards over the result on every ingest, writing
what they find to `filing.parse_status` and `filing.parse_notes`:

- **Implied price.** `value_usd / shares` outside $0.01–$100,000, on `SH` rows.
  The only check that reaches outside the document, and so the only one that
  catches a units error the *filer* made consistently — several managers kept
  filing in thousands after the cutover and had to amend, and every internal
  total in those documents agrees with itself. Not on `PRN` rows, whose ratio
  is a price per dollar of face value and means nothing against a share-price
  range.
- **Entry count.** Parsed rows against the cover page's `tableEntryTotal`,
  exactly. Catches a truncated download and a silently skipped row, both of
  which produce a portfolio that is merely *smaller* than the real one.
- **Value total.** The summed value against `tableValueTotal`, within 1%.
  Filers round; nothing rounds by 1000.
- **Negative quantity.** A value, share count or voting figure below zero. 13F
  is long-only and `holding` refuses one, so the parser drops the row; this
  makes that drop a verdict even on a cover page that declares no count.
- **CUSIP format.** Nine letters and digits. The parser drops a row whose CUSIP
  it cannot read and keeps one written with `*`, `@` or `#` — private-placement
  characters, and private placements are not 13(f) securities. A CUSIP the
  parser left-padded (`37833100`) is not a finding: the repair cannot be wrong.

Each finding in `parse_notes` has a `severity`. Every guard's is `error`, and
one `error` makes the filing `suspect`. A row the parser dropped for any other
reason is a `warning`: evidence for the entry-count finding it almost always
comes with, never a verdict on its own.

Known limit of the price guard: dividing by 1000 in error only pushes an implied
price under a cent for a stock trading below about $10, so an under-scaled
mega-cap ($170 a share read as $0.17) passes it. Closing that gap needs
`shares × price at period end`, which is a dependency on the price feed and
therefore an enrichment-step check rather than a parse-time one. `pytest
tests/test_normalisation.py -k mega_cap` pins the blind spot so nobody assumes it
away.

**Guards flag, they do not reject.** A filing that fails every one still loads,
marked `suspect`. It is the only disclosure that manager made for the quarter,
and dropping it leaves a hole indistinguishable from a manager who filed nothing.

What waits is *publishing*. `whalewatch recompute` builds `position_snapshot`,
the portfolio the read path serves, and leaves out every `(filer, period)` a
suspect filing counts toward unless it is run with `--include-suspect` — which
marks every row it publishes that way. `whalewatch check-data` lists those
periods, alongside the checks that need more than one filing to run: a top
position over 90% of its period, a share count up more than 10,000% on the
quarter before, and a quarter missing between two a filer did file. Those fire
legitimately — a big enough stock split is a 10,000% jump until you look at the
price — and the command exits 1 whenever one does, so that someone looks.

Keep a fixture of one real filing from each side of the boundary, plus one
post-cutover amendment of a pre-cutover period, in `tests/fixtures/`. That third
case is the one that regresses.
`information_table_thousands.xml` and `information_table_dollars.xml` are that
pair — the same four positions, values 1000x apart — and the amendment case is
`test_an_amendment_follows_the_date_it_was_filed_not_the_period_it_describes`.

### And the other traps

Less dramatic, still enough to make a number wrong:

- **Options are notional.** A `putCall` row's `value` is the value of the
  underlying, not the premium. Summing it with common stock inflates a portfolio
  by the full underlying exposure. Separate lines, excluded from share aggregates.
- **`PRN` is not `SH`.** `sshPrnamtType` is `SH` for shares and `PRN` for
  principal amount — convertible bonds report face value. Summing the two adds
  dollars to a share count.
- **Combination reports double-count.** Affiliated managers can each file, or one
  can file on behalf of several. Aggregating across all filers without honouring
  `reportType` and `otherManager` counts the same position twice.
- **Splits.** Share counts are as reported at the time. Comparing 2020-06-30 to
  2020-09-30 across Apple's 4-for-1 split, unadjusted, shows every holder
  quadrupling their position. `position_change` must compare split-adjusted
  shares, which means the price/corporate-action feed is a dependency of the
  delta table, not an optional enrichment. Until that feed exists it compares
  shares as filed, and a split reads as every holder adding.
- **Tickers are reused, CUSIPs change.** A CUSIP maps to different tickers over
  time and tickers get recycled between companies. Resolution is stored with
  `resolved_at`, and the join key inside the database is always `security_id`.
- **13F is long-only US equity.** No shorts, no cash, no bonds outside converts,
  no foreign listings, no direct commodities or FX. "Fund X is 100% in tech" may
  mean their entire non-13F book is invisible. This one is not fixable, only
  disclosable.

## Enrichment

**CUSIP → ticker (OpenFIGI).** 13F gives CUSIPs; nearly every consumer wants
tickers. Batched (the API takes many jobs per request), cached permanently in
`security`, and retried on a schedule for the ones that miss — a newly listed
name can resolve next month. Unresolved is a normal state, not a failure.

`OpenFIGIClient.map_cusips` in `app/ingestion/enrichment/figi.py` is the only
thing that talks to OpenFIGI. It batches to the per-request job cap and paces
requests under the per-minute quota: 25 requests/minute and 10 CUSIPs per
request without `OPENFIGI_API_KEY`, 25 per 6 seconds and 100 per request with
one (as published, checked 2026-10-12). Every job's response is written to
`figi_response_log` as it arrives, and a later call reads that log first, so
re-running costs nothing. It returns every match OpenFIGI gave, without
choosing one: an unfiltered CUSIP lookup typically answers with one listing per
exchange, and picking the right one belongs to resolution, not the transport.

A CINS (a CUSIP for a non-North-American issuer, with a letter first) is
asked for as `ID_CINS`. Asked as `ID_CUSIP`, OpenFIGI answers every one "No
identifier found", Linde, Chubb, Accenture and ASML included.

**Resolution (`resolve-cusips`).** Takes the securities with no ticker in
descending order of aggregate holdings value (`SUM(holding.value_usd)`), since
on the dev backfill 1,681 of 20,766 securities hold 95% of it. It tries OpenFIGI,
then the 13(f) list for a name only, then a trigram match on that name that is
queued in `manual_review` and never applied. `security.resolution_source`
records which source won. OpenFIGI wins only with an equity listing on a US
venue: the `US` composite, or one ticker across US venues. A CUSIP with listings
only abroad falls through. That includes Exxon's own, which OpenFIGI has only
on foreign venues and which needs `resolve-cusip`. `cusip-coverage` reports the
resolved share by row and by value. Value is the gate, at 95%: below it, the
aggregate rankings are quietly incomplete. A first run on the dev backfill
reached 95.22% by value and 58.93% by row. See
[the README](../README.md#resolve-cusips---limit-n---force).

Newer information tables may carry a FIGI column of their own. Prefer it, and
record which source a mapping came from, so a bad OpenFIGI match can be found and
corrected without re-resolving everything.

**Issuer names (the SEC's 13(f) list).** OpenFIGI has no match for some
CUSIPs. The SEC's Official List of Section 13(f) Securities still names most of
them, quarterly, free, and authoritatively, though without tickers.
`import-13f-list --quarter 2026Q1` reads the Division of Investment
Management's page for that quarter's link (the files have moved and been renamed
too often to build the URL), archives the text file under
`raw/13f-list/{quarter}/`, and replaces that quarter's rows in
`sec_13f_security`. Text only: from 2020Q1 every quarter has one, and before
that the lists are PDF only. A list is refused if its additions were already on
the previous quarter's list. That is how the SEC's 2025Q3 file, a copy of its
2025Q1 file, is caught. `issuer_names` in
`app/ingestion/enrichment/issuer_names.py` takes OpenFIGI's name and falls back
to the latest list's. `resolve-cusips` uses it.

**Prices.** Needed to value positions at a period end, to split-adjust share
counts, and for the sanity check above. Fetched per `(security, trade_date)` for
period-end dates only — this is not a full daily price history and should not
become one by accident.

## Orchestration

Celery, with Redis as the broker and result backend
([app/jobs/celery_app.py](../app/jobs/celery_app.py)). Two queues, each with its
own worker: `ingestion` for long EDGAR work, one task at a time while the EDGAR
limiter is per process, and `compute` for short Postgres work, so a backfill
never queues a refresh behind it. Late acks, a prefetch of one and
`task_reject_on_worker_lost`: a task killed mid-flight is redelivered, which the
idempotency below makes safe. Time limits 30 minutes soft, 35 hard.

Beat schedule in `app/jobs/schedule.py`, in code, crontabs in New York time
(see [the README](../README.md#the-schedule)):

- **13F poll** — discover → ingest → recompute → refresh. Roughly 60% of a
  year's 13Fs arrive in the days before each 45-day deadline, so every 30
  minutes inside the filing windows (1–20 February, May, August, November;
  `in_filing_window` in `app/core/periods.py`) and once a day, at 06:00,
  outside them. One half-hourly entry whose task decides
  (`should_poll_now`), not two entries.
- **Form 4** — weekdays 22:00: `crawl-form4 --catch-up`, every day since the
  watermark, then `ingest-form4` to load what it queued. A day whose index is
  not published yet is picked up on the next run.
- **Enrichment** — `fetch-quotes` weekdays 21:30, `resolve-cusips` daily 03:00.
- **Views** — `refresh-views` nightly at 04:00, and after every 13F ingest
  that published, as the last link of its chain.

Everything is also a CLI verb (`app/cli.py`: `ingest-filing`, `backfill`,
`recompute`, `refresh-views`) so that a bad quarter can be re-run by hand without
a broker in the loop. A verb and its task call one `@tracked_task` job
([app/jobs/tracking.py](../app/jobs/tracking.py)), which holds a Postgres
advisory lock named after the job for the run, so two runs of one job never
overlap: the second does nothing, records nothing, and succeeds. Tasks retry
transient failures only ([app/jobs/retry.py](../app/jobs/retry.py)), and the
13F tasks chain ingest → recompute → refresh
([app/jobs/tasks.py](../app/jobs/tasks.py)).

**Idempotency.** Every task is keyed on `accession_no` and safe to run twice.
Re-running a filing re-parses stored bytes and upserts; it does not duplicate and
does not re-fetch unless asked.

**Backfill.** `backfill` ([app/ingestion/backfill.py](../app/ingestion/backfill.py))
plans from the database, never from EDGAR. The plan covers every queued filing
and every loaded 13F, and it skips the loaded ones before any worker starts. So
resuming a run that died is running it again, at no network cost. Workers share
one `asyncio.Semaphore` (default 5) and the process-wide EDGAR limiter. Each
filing is archived, then parsed, then loaded. One filing's failure is caught,
logged, recorded on its queue row and reported, and the run carries on. A
rate-limit block is the exception, and it stops the run. `--force` reparses
loaded filings from the raw store, with `filed_at` taken from their `filing`
row. The first SIGINT lets in-flight filings finish and starts no new ones.

**Logging.** Every job binds `job_name` and `run_id`, and every filing binds
`accession_no` and `cik`, per the vocabulary in
[the README](../README.md#logging). A backfill of 2,000 filings that dies on
number 1,347 is only debuggable if one grep returns every line that touched that
filing.

**Run records.** Every job runs inside
[`track_run`](../app/jobs/tracking.py), CLI verb and Celery task alike, and
leaves one `ingestion_run` row: its parameters, its counters, and `success`,
`partial` or `failed` with what went wrong. The row's `id` is the `run_id` in
the log. So whether last night's backfill finished is
`whalewatch runs --job backfill_13f`, or the `SELECT` behind it, and the answer
does not depend on whoever started it still having the terminal open. See
[`ingestion_run`](data-model.md#ingestion_run).

**When a backfill goes wrong**, the recovery is
[`make reset-db`](../README.md#everyday-commands) plus a re-parse from
`raw_document` — which is why the raw layer exists and why nothing downstream of
it is load-bearing. Not yet fully offline: `backfill --force` takes each
filing's `filed_at` from its `filing` row, which a reset deletes, so until the
acceptance timestamp is archived with the documents a post-reset reload goes
back to EDGAR.
