# Estimated return since filing

How WhaleWatch estimates what a stock has done since a manager reported holding
it, and what the estimate leaves out. The code is
[`app/db/queries/returns.py`](../app/db/queries/returns.py). It is on the API
since SEC-8: see [On the API](#on-the-api).

## What it is, and what it is not

A 13F says what a manager held at a quarter end and what the holding was worth.
It does not say when they bought it, at what price, or what they did during the
quarter. So WhaleWatch cannot know a return **since purchase**, and never shows
one. It shows a return **since filing**: the stock's price at the end of the
reported quarter against a later price. The figure is the same for every
holder of the stock in that quarter, whether the row is `new`, `add`, `trim`
or `hold`. A `hold` may have been bought in 2016, and its return since filing
says nothing about that.

It is **not**:

- a cost basis, a purchase price or a manager's actual return
- a total return: dividends are not included, since both prices are share
  prices
- adjusted for spin-offs, mergers or special dividends: only splits (below)

## The basis: the implied quarter-end price

The basis is `implied_price` for the row's security and period: the median of
`value / shares` over every holder's 13F line, with a confidence grade
(`high`, `medium`, `low`) and the number of observations behind it. See
[`implied_price` in data-model.md](data-model.md#implied_price). No market data
vendor is involved, so the basis can be published.

The confidence and the observation count go out with the basis. A `low` basis
is one holder, or holders who disagree. The return is still computed from it,
and the client should say how sure it is.

## Two returns, to two later prices

| Field | Later price | Source | In production |
|---|---|---|---|
| `estimated_return_pct` | the latest close in `security_quote` | a quote provider (market data) | **always null**: `QUOTE_PROVIDER=null` |
| `return_to_last_quarter_pct` | the security's latest implied price after the row's period | SEC filings | present |

Both are `later price / basis - 1`, in percent, rounded to two places. The
second (decision D1 in the epic plan) exists so that production still shows a
number: it runs only to the last quarter end with filings, and
`last_quarter_period` says which one that is.

## Across a split

An implied price is per share as the shares were at the time. A later price is
per share as they are now. NVDA's 10-for-1 in June 2024 makes every 2024Q1
basis ten times what it would be in today's shares. On the dev data, NVDA's
2024Q1 implied price is $903.56 and its close on 2026-10-13 was $228.52. Taken
as filed, that is **−74.7%**, a loss nobody had.

So the basis is divided by the product of every split in `security_split` dated
after the row's period and on or before the later price's date:

```
basis_price          = implied_price / (ratio₁ × ratio₂ × …)
estimated_return_pct = (latest_close / basis_price − 1) × 100
```

For NVDA: $903.56 / 10 = $90.356, so **+152.9%**. `basis_split_ratio` and
`last_quarter_split_ratio` show the product that was applied. A reverse split
has a ratio below one and multiplies the basis.

A split is dated by the quarter end by which it had happened, as
[`security_split`](data-model.md#security_split) explains. A split dated the
row's own period had already happened at that period's close, so it is not
applied. The products are computed from the exact numerators and denominators,
the same arithmetic `position_change` uses.

## When it is null

Every field is null rather than zero when an input is missing:

- No `implied_price` row for the security in the period, or one with no price
  because every holder reported $0.
- No quote dated after the period (`estimated_return_pct`). This is always the
  case in production.
- No priced quarter after the period (`return_to_last_quarter_pct`): the
  period is the security's latest.
- **The quote falls inside a split's quarter, before that quarter's end.** The
  split happened at some point in that quarter, and the filings cannot say
  whether the quote was before or after it. This only happens with a quote that
  is older than the split's detection, meaning quote fetching was stopped for a
  quarter.

## Known limitations

- **A split since the last quarter end is invisible.** A split is only
  detected once the next quarter's 13Fs show it, up to 45 days after that
  quarter ends. Until then, `estimated_return_pct` for that stock is off by the
  split's ratio, and a 10-for-1 shows as about −90%. `return_to_last_quarter_pct`
  is not affected. Nothing in the filings can predict this. A corporate-action
  feed would fix it, but that is market data.
- **Detection errors carry through.** If detection picks the wrong ratio (ORLY's
  15-for-1 detected as 10-for-1), the adjustment uses the wrong ratio. A missed
  split (CTAS's 4-for-1) is not adjusted at all.
- **BRK.A has no implied price**, because its periods are held back by the
  implied-price ceiling. It has no return either.
- **Stale quotes.** The latest quote is used however old it is. `quote_date`
  goes out with the figure as `estimated_return_as_of`, and the client should
show it.

## How it is computed

At read time, not stored. The latest quote changes daily, and splits and prices
change on every `recompute`, so a stored return would go stale after any of
them. `estimated_return(security_id, period)` is a `LATERAL` subquery that an
endpoint joins onto its rows. Per row it does these primary-key probes:

- `implied_price` for the basis
- a backward scan of `security_quote` and of `implied_price` for the latest
  later row
- `security_split` for the two products

On a 200-row page it adds about 2 ms (see
[query-performance.md](query-performance.md#built-since-estimated-return)).
Cache invalidation is already handled. `fetch-quotes` flushes the cache when it
writes a close, and `recompute` and `detect-splits` flush it when they publish.

## On the API

Every row that is one security in one period carries the same seven fields,
all optional and nullable, after the row's own: a portfolio position
(`/v1/investors/{slug}/portfolio`), a stock's owner (`/v1/stocks/{ticker}/owners`)
and an activity row (`/v1/investors/{slug}/activity`).

| Field | From |
|---|---|
| `estimated_return_pct` | `estimated_return_pct`, a decimal string to two places |
| `estimated_return_as_of` | `quote_date`, when there is a return to it |
| `return_to_last_quarter_pct` | `return_to_last_quarter_pct` |
| `last_quarter_period` | `last_quarter_period` as a label (`2026Q2`), when there is a return to it |
| `price_estimate` | `{price, period, confidence, observation_count, basis}`: the basis as `implied_price` holds it, in the period's shares, with `basis` always `implied_from_filings`. Null when there is no price |
| `is_split`, `split_ratio` | the row's `position_change`: whether the change is across a split |

`price_estimate.price` is the price as filed, not `basis_price`: a client
showing "estimated from 43 filings" shows the number those filings say.
Option lines carry none of the seven, since an option's price is not the
stock's. An activity row's estimate is at its own period, an exit's included.

Two endpoints serve the series behind it, for a chart:
`/v1/stocks/{ticker}/implied-prices`, a quarter a row with the price in
today's shares beside it (`split_adjusted_price`), and
`/v1/stocks/{ticker}/splits`, for annotating it. `/v1/meta/data-sources`
says what is SEC-derived, what is from a vendor and what is absent, and
reports `latest_close` as absent until a quote is loaded.
