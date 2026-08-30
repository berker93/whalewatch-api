# Golden 13F fixtures

Six real filings, downloaded from EDGAR once and committed. The parsers are the
highest-risk code in this project and their inputs are external and messy — this
is the suite that makes them safe to refactor.

Each directory holds the two documents exactly as EDGAR served them (whitespace
between elements collapsed, content untouched) and `snapshot.json`, the parsers'
output over those bytes. `manifest.json` carries what the documents cannot: the
accession number, the CIK, and the acceptance timestamp that decides the units.

**Nothing here is fetched at test time.** A unit test that downloaded its own
input would be testing EDGAR's uptime, and would fail on a plane.

## The six, and why each one is here

| Fixture | Accession | Why it is worth keeping |
| --- | --- | --- |
| `berkshire-2022q3-thousands` | `0000950123-22-012275` | Filed 2022-11-14, before the 2023-01-03 units cutover, so every `value` is **thousands of dollars**. 179 rows across ~48 securities — Berkshire splits each position across the managers holding it, so the same CUSIP appears several times and the rows must not be collapsed. The `before` half of the cutover pair. |
| `berkshire-2022q4-dollars` | `0000950123-23-002585` | The very next quarter from the same manager, filed 2023-02-14 in **whole dollars**. Same portfolio, six weeks later, other side of the line: $296.1bn against $299.0bn once both are normalised, and 1000x apart if either multiplier is wrong. |
| `berkshire-2023q3-restatement` | `0000950123-23-011029` | `13F-HR/A`, `amendmentType` **RESTATEMENT** — the whole 152-row table again, replacing the original. Also carries `isConfidentialOmitted=true`: this filing is knowingly incomplete, and the positions it withholds arrive in the next fixture's kind of amendment. |
| `berkshire-2023q4-new-holdings` | `0000950123-24-005664` | `13F-HR/A`, `amendmentType` **NEW HOLDINGS** — one row, the Chubb stake released when confidential treatment expired. The cheapest fixture here and the most dangerous filing shape in the pipeline: loading its single row as a restatement deletes the other 150 positions of that quarter. |
| `soros-2026q2-options` | `0000902664-26-003507` | 266 rows from a manager who uses options: six `Put` rows and six `Call` rows against CUSIPs that also appear as common stock, plus 16 `PRN` rows reporting a principal amount rather than a share count. An option's `value` is the notional of the underlying, not the premium, which is what breaks a naive price check. |
| `point72-2025q3-large` | `0000902664-25-005042` | 2,263 rows, 870KB — the size at which streaming the table stops being a style preference. 896 of those rows are puts or calls, so it is also the volume test for the option handling the fixture above tests one row at a time. |

Every one of the six parses clean today: no dropped rows, no warnings, and each
filer's own `tableEntryTotal` and `tableValueTotal` match what the parser
summed. That is the baseline. A warning appearing in a snapshot diff is a
finding, not noise.

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

`make fixtures-fetch` is for adding a seventh fixture. It is not for refreshing
the six that are here: their bytes are the input the snapshots describe, and
re-downloading them is how that stops being true.
