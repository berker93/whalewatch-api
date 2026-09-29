# WhaleWatch — index funds: requirements

Status: **not started**. Written 2026-09-30 for the next iteration. Covers this
repo (API and data) and `whalewatch-web` (frontend and design).

## Why a separate section

The core universe (`data/investors.yaml`) deliberately leaves out index
managers. Their 13F is close to the market portfolio, and their changes follow
flows and index rebalances, not decisions. If they sat alongside Buffett in
"who is buying NVDA", they would outweigh every active manager combined. They
are still worth showing, for a different question: **how much of a company
is owned passively, and is that share growing?**

So index funds get their own section. They are kept out of the active views by
default, and they are never mixed into "smart money" signals without the
reader explicitly asking for it.

## Goals

1. A browsable set of index managers, each with a holdings page like an active
   investor's page.
2. On every stock page, passive ownership shown separately from active
   ownership.
3. A market-level view of passive vs active ownership over time.
4. No change to any existing active aggregate unless the caller opts in.

## Non-goals

- ETF-level or fund-level holdings (N-PORT). A 13F is filed per adviser, not
  per fund, so "what does VOO hold" is out of scope.
- Index composition data (S&P or MSCI constituents). We report what was filed,
  not what the index contains.
- Percent of shares outstanding, until shares-outstanding data exists (see
  *Dependencies*). Until then, percentages are "% of 13F-reported shares".

## Candidate universe

Checked against EDGAR's submissions API on 2026-09-30. As with the core list,
several of these managers file under more than one CIK.

| Slug (proposed) | Manager | CIKs (oldest first) | 13F-HR span | Notes |
| --- | --- | --- | --- | --- |
| `vanguard` | Vanguard | 102909, 2100121, 2100119 | 1999Q1– | Vanguard Group Inc through 2025Q4, then 13F-NT. From 2026Q1, **Vanguard Portfolio Management** (2100121) and **Vanguard Capital Management** (2100119) each file a 13F-HR. These are two advisers with separate books, so they must be **summed** |
| `blackrock` | BlackRock | 1364742, 2012383 | 2006Q1– | The old CIK (now "BlackRock Finance, Inc.") runs through 2024Q2; BlackRock, Inc. from 2024Q3. A clean handoff |
| `state-street` | State Street | 93751 | 1999Q1– | Files 13F-HR/A frequently, so it exercises the amendment logic |
| `geode` | Geode Capital Management | 1214717 | files through 2026Q2 (full span not checked) | Sub-adviser to Fidelity's index funds |
| `northern-trust` | Northern Trust | 73124 | files through 2026Q2 (full span not checked) | |
| `schwab-asset-management` | Charles Schwab Investment Management | 884546 | 1999Q1– | |
| `legal-and-general` | Legal & General | 764068 | 1999Q1– | GB. Mostly index (LGIM) |
| `norges-bank` | Norges Bank Investment Management | 1374170 | files through 2026Q2 (full span not checked) | NO. Sovereign wealth fund, largely index-like. Decide whether it belongs here or in the core list. `country: "NO"` has to be quoted in YAML |

Invesco (914208) files one 13F covering active funds and the QQQ-style ETFs
together, so it can't be split into passive and active. Leave it out unless
someone decides it counts as index.

## Data model and ingestion (this repo)

**R1. A `kind` on filer.** Add `filer.kind` as text with a CHECK constraint,
values `active | index`, `NOT NULL DEFAULT 'active'`.
- Don't add `index` as a seventh `category`. Category describes an investment
  style and applies only to active managers. `kind` decides which views a filer
  appears in, which is a different question. For index filers, `category` is
  null.

**R2. A separate list file with the same schema.** Put the index managers in
`data/index-funds.yaml`. Seed both files with the same `seed-investors`
command.
- The `kind` comes from which file an entry is in, not from a field that could
  be set wrong.
- Validation extends across both files. A slug or CIK that appears in both
  fails, because slugs share one URL namespace and CIKs share one unique
  constraint.

**R3. Overlapping CIKs are handled explicitly.** The `effective_filing`
resolver and the `overlap` policy already exist (see *Which filings count* in
the data model). Vanguard's entry needs `overlap: sum` for 2026 onwards, with
its three CIKs listed in the order shown above. BlackRock's handoff needs
nothing. Confirm both with `audit-overlaps` once the quarters are loaded.

**R4. Large filings.** Index 13Fs are an order of magnitude bigger than an
active manager's: thousands of rows per quarter, per adviser.
- Ingest one real Vanguard, BlackRock and State Street filing into the golden
  fixture suite (`make fixtures-fetch`) before building on them.
- Measure parse and load time. The loader replaces all of a filing's holdings
  on every re-ingest, so re-ingest cost scales with filing size.
- Check that the materialised views behind the aggregates still refresh in
  acceptable time with index filers present, even though the default views
  exclude them.

## API (this repo)

The current surface is a sketch (product-spec, *API surface*). These are the
changes that surface needs.

**A1. Investor endpoints serve both kinds.**
- `GET /investors/{slug}` and its sub-resources work for index filers
  unchanged, and every filer payload carries `kind`.
- `GET /investors` gains `?kind=active|index|all`, defaulting to `active`.
  Leaving index filers out by default keeps existing clients' results
  unchanged.

**A2. Holdings must be paginated for index filers.**
- `GET /investors/{slug}/holdings` for an index filer returns thousands of
  rows. Cursor pagination (already planned) is mandatory, sorted by value
  descending by default.
- Add a `summary` block: position count, total value, and top-10 concentration
  (% of total value).

**A3. Changes, framed for index managers.**
- `GET /investors/{slug}/changes` still works. For index filers, most of the
  changes are flows, so the response supports `?sort=value_delta` and
  `?type=opened|exited` (index additions and deletions are the interesting
  part).
- The existing "a decrease is not a sale" rule applies with extra force here.

**A4. Passive ownership on stocks.**
- `GET /stocks/{ticker}/holders` gains `?kind=`, defaulting to `active` as
  today.
- Every response carries an `ownership` block, split by kind:

  ```json
  "ownership": {
    "period": "2026-06-30",
    "basis": "13f_reported_shares",
    "active": { "shares": 0, "filers": 0 },
    "index":  { "shares": 0, "filers": 0 },
    "index_share": 0.0
  }
  ```

  `basis` is spelled out because the percentage is of 13F-reported shares, not
  of shares outstanding, and a client has to be able to label it correctly.

**A5. Market views.**
- `GET /market/flows` and `GET /market/crowded` exclude index filers by
  default and accept `?kind=index|all`.
- New endpoint: `GET /market/passive-share?period=`. It returns, per issuer,
  the index share of 13F-reported shares plus the change from the prior
  period, and it is paginated.
- Optionally, `GET /market/passive-share/history` returns a market-wide time
  series.

**A6. Every payload states its period.** This is the existing presentation
rule, and it matters more here: index holdings are the same quarterly
snapshots, published 45 days late.

## Frontend and design (`whalewatch-web`)

The stack is React 19, Vite, Tailwind 4 and Radix, on the **Nocturne** design
system (`nocturne-readme.md`). Build from its tokens and classes (`.card`,
`.table`, `.tag`, `.seg`), with Phosphor icons, and don't hard-code values.
None of the investor pages exist yet, so this section describes how index
funds should look *within* them.

**F1. Navigation.**
- Add an "Index funds" entry beside "Investors".
- Routes: `/index-funds` for the list and `/index-funds/:slug` for a detail
  page. The detail page calls `GET /investors/{slug}`.
- A slug requested under the wrong section redirects to the right one, so
  `/investors/vanguard` → `/index-funds/vanguard`.

**F2. Visual separation, following Nocturne's own rules.**
- The accent (`#9184d9`, "a line and a glow") is kept for active conviction:
  new positions, large adds.
- Index filers get the **neutral** ramp: a `.tag-neutral` "Index" tag
  wherever an index filer's name appears, neutral-500 lines in charts, and no
  accent marks.
- The rule to remember: *accent = someone decided; neutral = it followed an
  index.*

**F3. Index funds list page (`/index-funds`).**
- One `.card` per manager, showing: name, total 13F value, position count and
  latest period.
- A short intro explains why these managers are separate (one or two
  sentences, reusing *Why a separate section* above).

**F4. Index fund detail page (`/index-funds/:slug`).**
- Header: name, "Index" tag, latest period with its filing date, and the
  `summary` block from A2.
- Holdings: a paginated `.table`, sorted by value. Search within holdings by
  ticker or name. The table must stay usable at 5,000+ rows, so use
  server-side pagination and never load everything.
- Changes tab: two lists, "Added to / removed from" (opened and exited) and
  "Largest changes by value". Each is labelled as reflecting flows and
  rebalancing, not decisions.
- No conviction language anywhere: no "bought", "sold", "bet" or "conviction".

**F5. Stock page, passive ownership.**
- A single horizontal bar splitting reported ownership into active (accent)
  and index (neutral). It shows `index_share` as a number, and the axis label
  reads "of 13F-reported shares".
- Holders table: a `.seg` segmented control with Active (default), Index and
  All.

**F6. Market page.**
- A "Passive share" view: the issuers with the highest index share, and the
  biggest movers against the prior period.
- A market-wide line over time if A5's history endpoint exists.
- Active aggregates keep their current defaults. An "Include index funds"
  toggle is off by default, and the view says so when it's on.

**F7. Required states.**
- **Loading:** skeleton rows in the table.
- **Empty:** "No filings for this period".
- **Partial:** a banner when a filer has CIKs whose holdings are still
  deferred.
- **Period labels:** every number carries its period, and "current" never
  appears.
- **Contrast:** meets the Nocturne contrast note. Neutral text at 300 or
  lighter on the dark ground, and never accent-coloured body text.

**F8. Design deliverables before build.**
- Mockups of F3, F4 (both tabs) and F5, at desktop and 375px widths.
- One chart spec covering F5 and F6: colours from the ramps, and legend and
  tooltip formats.

## Dependencies and order

1. **Overlap resolver** (R3). Done; Vanguard only needs its YAML entry.
2. **`kind` column, second YAML file, seed changes** (R1, R2).
3. **Fixture filings and performance check** (R4).
4. **Read API** for investors, stocks and market (Epic 3). A1 to A5 extend it,
   so they come after or alongside it.
5. **Frontend investor and stock pages.** F1 to F7 are variants of those
   pages.
6. **Shares outstanding** (Epic 4 enrichment). This is required before any "%
   of company" figure; until then the basis is 13F-reported shares.

## Acceptance criteria

- `data/index-funds.yaml` is committed. `seed-investors` seeds both files
  idempotently, and validation fails on a slug or CIK that appears in both
  files.
- `filer.kind` exists. Every existing endpoint's default output is
  byte-identical before and after index filers are seeded.
- Vanguard's 2026Q1 total equals the sum of its two advisers' filings.
  BlackRock has exactly one filing per period across its handoff.
- `/stocks/{ticker}/holders` returns the `ownership` block with `basis`.
- `/market/passive-share` is paginated and states its period.
- The frontend index pages use only Nocturne tokens and classes, pass the
  contrast rules, and contain no conviction wording.

## Open questions

- **Norges Bank:** index section, or the core list under `value`?
- **Geode:** in the index section, or folded into Fidelity? (It files
  separately and is a separate adviser. The recommendation is the index
  section.)
- **Follows:** should users be able to follow an index filer, or would the
  feed noise be useless?
- **Crossover threshold:** should the passive share be surfaced as a stock
  signal, for example "index share crossed 30%", or kept descriptive only?
  The spec's non-goals lean towards descriptive.
