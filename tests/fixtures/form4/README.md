# Form 4 fixtures

Eight real Form 4s, downloaded from EDGAR once and committed byte for byte. Each
one is here for a transaction code or a shape the parser has to get right. The
assertions live in `tests/test_form4_parser.py`. `manifest.json` has each
accession number and source URL.

**Nothing here is fetched at test time.**

| Fixture | Accession | Why it is here |
| --- | --- | --- |
| `purchase-mastercraft-ceo` | `0001193125-26-393047` | **P**, an open-market purchase by a CEO who is also a director. Two lines, both role boxes checked on one owner. |
| `sale-everpure-director` | `0001593803-26-000006` | **S**, a director's sale with no plan. Also has a `nonDerivativeHolding` line, which is not a transaction and takes no line number. |
| `award-state-street-director` | `0000093751-26-000589` | **A**, a director's award filed at a price of `0`, which must parse as no price. |
| `exercise-intuitive-rsu` | `0001274559-26-000005` | **M**, RSUs vesting: a derivative M (disposed) into a non-derivative M (acquired), plus an **F** for tax withholding. |
| `plan-sale-liquidia` | `0001104659-26-115641` | **10b5-1 plan sale**. The checkbox is ticked and the sale's footnote names the plan. The exercise beside it cites no plan footnote and is not flagged. |
| `plan-checkbox-only-robinhood` | `0001783879-26-000149` | The 10b5-1 checkbox **with no footnote** naming the plan, so every line is flagged. Weighted-average prices with ranges. |
| `joint-filing-kinetik` | `0000905148-26-004450` | **Five reporting owners** (a GP, its manager, a holding company, two people) reporting four sales once for all of them. |
| `amendment-nvidia` | `0002152188-26-000007` | A **4/A**, with `dateOfOriginalSubmission` and a remark saying what it corrects. |

Seven of them came from the 280 Form 4s that `crawl-form4` queued on the dev
database for 2026-10-08 to 2026-10-13. Every one of those 280 parsed when the
parser was written. `purchase-mastercraft-ceo` was found through EDGAR full-text
search, because that week's only P filings were an ESPP buy (TSMC) and a bank
trading a closed-end fund. Neither is the conviction purchase the feed exists
for.

To add one, download the XML document listed in the filing's `index.json`.
The filename varies by filing agent (`form4.xml`, `wk-form4_*.xml`,
`edgardoc.xml`...). Send a User-Agent as `app/ingestion/edgar/client.py` does,
then add a row here and in `manifest.json`.
