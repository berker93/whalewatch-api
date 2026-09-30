# Golden 13F fixtures

Nine real filings, downloaded from EDGAR once and committed. The parsers are the
highest-risk code in this project and their inputs are external and messy — this
is the suite that makes them safe to refactor.

Each directory holds the two documents exactly as EDGAR served them (whitespace
between elements collapsed, content untouched) and `snapshot.json`, the parsers'
output over those bytes. `manifest.json` carries what the documents cannot: the
accession number, the CIK, and the acceptance timestamp that decides the units.

**Nothing here is fetched at test time.** A unit test that downloaded its own
input would be testing EDGAR's uptime, and would fail on a plane.

## The nine, and why each one is here

| Fixture | Accession | Why it is worth keeping |
| --- | --- | --- |
| `berkshire-2022q3-thousands` | `0000950123-22-012275` | Filed 2022-11-14, before the 2023-01-03 units cutover, so every `value` is **thousands of dollars**. 179 rows across ~48 securities — Berkshire splits each position across the managers holding it, so the same CUSIP appears several times and the rows must not be collapsed. The `before` half of the cutover pair. |
| `berkshire-2022q4-dollars` | `0000950123-23-002585` | The very next quarter from the same manager, filed 2023-02-14 in **whole dollars**. Same portfolio, six weeks later, other side of the line: $296.1bn against $299.0bn once both are normalised, and 1000x apart if either multiplier is wrong. |
| `berkshire-2023q3-original` | `0000950123-23-010898` | The `13F-HR` the next fixture restates: the same 152 rows and the same $313.3bn, with the Other Manager column blank on every row. Loaded alongside its restatement it is the same book twice, to the dollar. |
| `berkshire-2023q3-restatement` | `0000950123-23-011029` | `13F-HR/A` No. 1, `amendmentType` **RESTATEMENT** — the whole 152-row table again, two days later, with the Other Manager column filled in. Replaces the original. Also carries `isConfidentialOmitted=true`: this filing is knowingly incomplete, and the position it withholds arrives in the next fixture. |
| `berkshire-2023q3-new-holdings` | `0000950123-24-005653` | `13F-HR/A` No. 2, `amendmentType` **NEW HOLDINGS** — one row, Chubb, released six months after the restatement. Filed *after* the restatement, so it adds to it; a new-holdings amendment filed before one would be covered by it instead. With the two above, every 13F Berkshire filed for 2023Q3. |
| `berkshire-2023q4-original` | `0000950123-24-002518` | The `13F-HR` the next fixture adds to: 138 rows across 41 positions, filed with `isConfidentialOmitted=true`. |
| `berkshire-2023q4-new-holdings` | `0000950123-24-005664` | `13F-HR/A` No. 1, `amendmentType` **NEW HOLDINGS** — one row, the Chubb stake released when confidential treatment expired. The cheapest fixture here and the most dangerous filing shape in the pipeline: loading its single row as a restatement replaces the other 41 positions of that quarter with it. |
| `soros-2026q2-options` | `0000902664-26-003507` | 266 rows from a manager who uses options: six `Put` rows and six `Call` rows against CUSIPs that also appear as common stock, plus 16 `PRN` rows reporting a principal amount rather than a share count. An option's `value` is the notional of the underlying, not the premium, which is what breaks a naive price check. |
| `point72-2025q3-large` | `0000902664-25-005042` | 2,263 rows, 870KB — the size at which streaming the table stops being a style preference. 896 of those rows are puts or calls, so it is also the volume test for the option handling the fixture above tests one row at a time. |

Every one of the nine parses clean today: no dropped rows, no warnings, and each
filer's own `tableEntryTotal` and `tableValueTotal` match what the parser
summed. That is the baseline. A warning appearing in a snapshot diff is a
finding, not noise.

The five Berkshire 2023 fixtures are two complete periods, not five samples.
`tests/integration/test_amendments.py` loads them and resolves each period, so
they are kept as sets: `RESTATED_PERIOD` and `ADDED_TO_PERIOD` in
`tests/fixtures_13f.py`.

## Working with them

```sh
make fixtures          # rewrite the snapshots from the committed documents
make fixtures-fetch a=0000000000-00-000000 cik=1234567 slug=name note="why"
```

`make fixtures` is the only thing that writes a snapshot. Tests never do — a
suite that repaired its own expectations on failure would turn every regression
into a clean run.

**A failing snapshot test is not fixed by running `make fixtures`.** Read the
diff. `snapshot.json` puts one holding per line for exactly this reason: a
changed position is one changed line, a lost one is one deleted line, and the
row count and value total at the top of the file say which happened before you
scroll. Regenerate once you have decided the new output is the better one.

`make fixtures-fetch` is for adding a tenth fixture. It is not for refreshing
the nine that are here: their bytes are the input the snapshots describe, and
re-downloading them is how that stops being true.
