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
| `www.sec.gov/divisions/investment/13f/13flist……` | The quarterly Section 13(f) securities list — what is reportable in a given quarter |
| OpenFIGI `POST /v3/mapping` | CUSIP → ticker + FIGI |

Everything but OpenFIGI is unauthenticated and free. None of it has a rate limit
you want to discover empirically.

## Talking to EDGAR

**One client, one throttle.** `app/ingestion/edgar/client.py` is the only thing
in this codebase that opens a connection to sec.gov. It owns the `User-Agent`,
the rate limiter, retries, and the write into `raw_document`.

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

**Archive before parse.** Every fetched document is written to `raw_document`
with its sha256 before a parser sees it. Parsing is then a pure function from
stored bytes to rows, re-runnable at any time without touching the network,
which is what makes a parser bug a `whalewatch recompute` rather than a week of
re-crawling.

## Discovery

**13F** is pull-based, per filer. We keep a list of tracked filer CIKs; for each,
fetch the submissions JSON, take every `13F-HR` and `13F-HR/A`, and enqueue any
accession number not already in `filing`.

**Form 4** is push-based, per day. There is no useful "all insiders" list, so we
walk the daily index for each business day, filter to form type `4`, and enqueue.
Weekends and market holidays have no index file — a 404 there is expected and is
not an error. Backfilling insiders means walking every day in the range.

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
upsert keyed on the natural keys, `holding_change` for that period and the next
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
filings are what you actually have. Check both:

- Against the filing's own `tableValueTotal`, whose units match the table's.
- Against the arithmetic: `value` should be within a factor of ~2 of
  `shares × price at period end` for common stock. A ratio clustering near 1000
  or 1/1000 across a filing means the convention was misread. Run this as an
  assertion during backfill, not as a dashboard nobody opens.

Keep a fixture of one real filing from each side of the boundary, plus one
post-cutover amendment of a pre-cutover period, in `tests/fixtures/`. That third
case is the one that regresses.

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
  quadrupling their position. `holding_change` must compare split-adjusted
  shares, which means the price/corporate-action feed is a dependency of the
  delta table, not an optional enrichment.
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

Newer information tables may carry a FIGI column of their own. Prefer it, and
record which source a mapping came from, so a bad OpenFIGI match can be found and
corrected without re-resolving everything.

**Prices.** Needed to value positions at a period end, to split-adjust share
counts, and for the sanity check above. Fetched per `(security, trade_date)` for
period-end dates only — this is not a full daily price history and should not
become one by accident.

## Orchestration

Celery, with Redis as the broker. Beat schedule in `app/jobs/schedule.py`:

- **Daily** — walk yesterday's daily index for Form 4s.
- **Daily during filing season** — poll tracked filers' submissions JSON for new
  13Fs. Outside the ~three weeks after each due date this finds nothing, so it
  drops to weekly.
- **After a period completes** — recompute `holding_change`, then
  `refresh-views`. Triggered by ingestion finishing, not by a clock.

Everything is also a CLI verb (`app/cli.py`: `ingest-filing`, `backfill`,
`recompute`, `refresh-views`) so that a bad quarter can be re-run by hand without
a broker in the loop.

**Idempotency.** Every task is keyed on `accession_no` and safe to run twice.
Re-running a filing re-parses stored bytes and upserts; it does not duplicate and
does not re-fetch unless asked.

**Logging.** Every backfill binds `job_name` and `run_id`, and every filing binds
`accession_no` and `cik`, per the vocabulary in
[the README](../README.md#logging). A backfill of 2,000 filings that dies on
number 1,347 is only debuggable if one grep returns every line that touched that
filing.

**When a backfill goes wrong**, the recovery is
[`make reset-db`](../README.md#everyday-commands) plus a re-parse from
`raw_document` — which is why the raw layer exists and why nothing downstream of
it is load-bearing.
