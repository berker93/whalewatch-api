# WhaleWatch — query performance

The ten queries behind the API, what Postgres does with each of them on the
full dataset, and what changed because of it. Migration `0017_query_indexes`
added three indexes and dropped one. Every plan below was captured by
`make explain` ([`scripts/explain_queries.py`](../scripts/explain_queries.py)),
which runs each query twenty times the way the API would, as a prepared
statement with bound parameters, then once more under
`EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)`. Run it again after changing an
index, a derived table or one of these queries, and compare.

Only `GET /filings/{accession_no}` is built. The other nine are the queries
the endpoints in the [product spec](product-spec.md#api-surface) will issue.
Their select lists are a guess. Their `WHERE` and `ORDER BY` clauses are what
the indexes were designed against, so when an endpoint is built, check its
query against the one here.

## Summary

| # | Query | Endpoint | Before | After | What changed |
| --- | --- | --- | --- | --- | --- |
| 1 | `filing_holdings` | `GET /filings/{accession_no}` | 11.90 ms | 11.83 ms | — |
| 2 | `latest_period` | `?period=` omitted | 21.17 ms | 0.01 ms | seq scan → index-only scan |
| 3 | `investor_periods` | `GET /investors/{slug}/periods` | 1.07 ms | 1.07 ms | — |
| 4 | `investor_holdings` | `GET /investors/{slug}/holdings` | 2.22 ms | 2.19 ms | — |
| 5 | `investor_changes` | `GET /investors/{slug}/changes` | 4.37 ms | 4.88 ms | — |
| 6 | `stock_search` | `GET /stocks?q=` | 5.07 ms | 0.02 ms | seq scan → trigram index |
| 7 | `stock_holders` | `GET /stocks/{ticker}/holders` | 0.11 ms | 0.06 ms | one index probe instead of 100 |
| 8 | `stock_holder_changes` | `GET /stocks/{ticker}/holders/changes` | 0.15 ms | 0.09 ms | partial index |
| 9 | `market_flows` | `GET /market/flows` | 4.36 ms | 4.31 ms | — |
| 10 | `market_crowded` | `GET /market/crowded` | 3.27 ms | 3.30 ms | — |

Each time is the server-side mean over 20 runs with a warm cache, from
`pg_stat_statements`. Every query was under the 100 ms budget before any index
changed, and is after. The slowest end to end is `filing_holdings`: a median
of 26 ms at the client, of which 12 ms is Postgres and the rest is turning
13,450 rows into Python objects. The budget was never at risk on this dataset.
What changed is that the two queries that read a whole table to return a
handful of rows no longer do.

Index changes, all in `0017`:

| Index | Size | Why |
| --- | --- | --- |
| `+ position_snapshot (period_of_report, security_id)` | 15 MB | `latest_period`, `stock_holders` |
| `+ security USING gin (name gin_trgm_ops)` | 1.5 MB | `stock_search` |
| `+ position_change (period_of_report, security_id) WHERE action <> 'hold'` | 16 MB | `stock_holder_changes` |
| `− holding (filer_id, period_of_report)` | −14 MB | proven unnecessary |

## The dataset

A full backfill of the 100 curated filers, five years back: 2,216 filings,
1,856,625 `holding` rows and 20,766 securities. Rebuilt with
`recompute --all --include-suspect`, that is 1,405,974 `position_snapshot`
rows and 1,639,772 `position_change` rows over 2,010 filer-periods. The views
hold 189,179 (`mv_consensus_holdings`), 198,947 (`mv_quarter_flows`) and 2,010
(`mv_filer_summary`) rows.

**Why `--include-suspect`.** 718 of the 2,216 filings are `suspect`, and a
default rebuild withholds every period they count toward. That leaves 471,032
positions, a third of the data. Half of those filings, 359, are suspect for
one reason only. They hold Berkshire class A (`084670108`) at about $411,000 a
share, above `MAX_IMPLIED_PRICE` in
[`normalisation.py`](../app/ingestion/normalisation.py). That constant is
$100,000, and its own comment argues for a ceiling above Berkshire. So the
measurements here use every period, at the size the dataset will have once
that ceiling is fixed. Afterwards the dev database was rebuilt the default
way.

**The parameters** are the worst case the data has, which is what the script
picks by default:

- **period:** 2022-03-31, the period with the most positions
- **filer:** Citadel, the largest filer in it, with 6,134 positions
- **security:** Microsoft (`594918104`), the most widely held
- **filing:** Citadel's 13F for that period, `0000950123-22-006403`. It is the
  largest at 13,450 lines, 7,191 of them options

**The server** is Postgres 16.15 in Docker on stock settings: `shared_buffers`
128 MB, `work_mem` 4 MB, `random_page_cost` 4, `effective_cache_size` 4 GB, and
two parallel workers per query. JIT is on above a cost of 100,000, which none
of these queries reaches. `position_snapshot` is 181 MB, larger than
`shared_buffers`, so a full scan of it can never stay cached in Postgres and
reads from the OS cache every time. That is the `shared read` in
`latest_period`'s plan below.

## Reading the plans

A plan is a tree. The most indented node runs first and feeds its parent, so
read it bottom-up and inside-out. On each node, `cost=a..b` is the planner's
estimate in its own units, the startup cost and then the total. `actual time=a..b`
is milliseconds to the first row and to the last, `rows` is the rows produced,
and both are **per loop**. Multiply by `loops` for the node's total.

Three things to look at first, each with an example from below:

1. **Estimated against actual rows.** A large gap means the planner chose the
   plan for a table that does not exist. There are three causes in this
   document, and they have three different fixes. Statistics went stale after
   a bulk rewrite (the fix is `ANALYZE`). Two columns that are correlated were
   estimated as independent: 707 rows against 13,450. A filter on an
   expression has no statistics at all: 12 rows against 2,216. The last two
   need `CREATE STATISTICS`. See [Statistics](#statistics).
2. **Buffers.** `shared hit` is a page found in Postgres's cache and `shared
   read` is one fetched from outside it, the OS cache or disk, with its time
   under `I/O Timings` (`track_io_timing` is on). `latest_period` read 10,976
   pages per run. The buffer count is also the clearest measure of how much
   work a plan does. Dropping one index turned a 212-buffer lookup into a
   1,452-buffer one, for a third of a millisecond more.
3. **Node types.** A `Seq Scan` on a large table with a selective filter is
   the classic finding: `stock_search` removed 20,758 rows to keep 8. A
   `Nested Loop` with a high loop count is the second. `stock_holders`' loop
   ran 100 times and was fine, because each loop cost three buffers. A loop is
   only a problem when the work inside it is.

## Finding the slow ones: pg_stat_statements

Enabled in two halves. `docker-compose.yml` loads the library at server start
(`shared_preload_libraries=pg_stat_statements`, plus
`pg_stat_statements.track=all` and `track_io_timing=on`). Migration `0016`
creates the view that reads it. One row per normalised statement, constants
replaced by `$n`:

```sql
SELECT calls,
       round(total_exec_time::numeric / 1000, 1) AS total_s,
       round(mean_exec_time::numeric, 1)         AS mean_ms,
       shared_blks_read                          AS read,
       left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 8;
```

After the backfill, two full rebuilds and the API runs:

```
  calls  | total_s | mean_ms |  read  |  query
---------+---------+---------+--------+-------------------------------------------------------------
    4435 |   114.0 |    25.7 |      0 | SELECT pg_advisory_xact_lock($1::BIGINT) ...
       2 |    44.7 | 22338.2 | 143351 | WITH winning_filings AS (SELECT effective_filing.filing_id ...
       2 |    43.1 | 21537.9 | 457172 | WITH held AS (SELECT position_snapshot.filer_id ...
 1437263 |    38.8 |     0.0 |    437 | INSERT INTO holding (filing_id, security_id, filer_id, ...
 9198972 |    22.6 |     0.0 |     14 | SELECT $2 FROM ONLY "public"."filer" x WHERE "id" ... FOR KEY SHARE
 9196644 |    21.6 |     0.0 |    983 | SELECT $2 FROM ONLY "public"."security" x WHERE "id" ... FOR KEY SHARE
```

Order by total time, not by mean. A query that takes 4 ms and runs a million
times costs more than one that takes a second and runs once. This list says
three things the API never would:

- **The top statement is a wait, not a query.** `pg_advisory_xact_lock` is the
  recompute lock. The backfill's five concurrent loads took turns on it, 114 s
  in all, and a statement waiting on a lock counts as executing.
- **Foreign-key checks** are the two `FOR KEY SHARE` rows: nine million each,
  one per row a rebuild inserts. They are 44 s together, as long as the two
  full rebuilds' snapshot query took in all.
- **The ten API queries are not on it.** Across about 550 calls they total
  about 2.3 s.

The script finds its own rows by a comment at the head of each statement
(`/* api:stock_holders */`), since the view keeps the text it saw first.
`SELECT pg_stat_statements_reset()` starts the counts again.

## The ten queries

Each section has the query, then the plan from before `0017`, then the plan
after where it changed. Plans are warm runs on vacuumed tables.

### 1. `filing_holdings` — `GET /filings/{accession_no}`

The second of the endpoint's two queries, with `include_options` left at its
default of `true`. Unchanged at 11.9 ms.

```sql
SELECT h.cusip, s.name AS issuer_name, s.ticker, h.value_usd, h.shares,
       h.sshprnamt_type, h.put_call, h.investment_discretion,
       h.voting_sole, h.voting_shared, h.voting_none
FROM holding h
JOIN security s ON s.id = h.security_id
WHERE h.filing_id = :filing_id
ORDER BY h.value_usd DESC, h.cusip, h.put_call, h.sshprnamt_type
```

```
Sort  (cost=2108.23..2141.96 rows=13491 width=98) (actual time=8.426..8.774 rows=13450 loops=1)
  Sort Key: h.value_usd DESC, h.cusip, h.put_call, h.sshprnamt_type
  Sort Method: quicksort  Memory: 1731kB
  Buffers: shared hit=397
  ->  Hash Join  (cost=652.66..1182.77 rows=13491 width=98) (actual time=2.165..4.581 rows=13450 loops=1)
        Hash Cond: (h.security_id = s.id)
        Buffers: shared hit=397
        ->  Index Scan using ix_holding_filing_id on holding h  (cost=0.43..495.11 rows=13491 width=53) (actual time=0.008..0.724 rows=13450 loops=1)
              Index Cond: (filing_id = '294'::bigint)
              Buffers: shared hit=212
        ->  Hash  (cost=392.66..392.66 rows=20766 width=61) (actual time=2.119..2.119 rows=20766 loops=1)
              Buckets: 32768  Batches: 1  Memory Usage: 1573kB
              Buffers: shared hit=185
              ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=61) (actual time=0.002..0.902 rows=20766 loops=1)
                    Buffers: shared hit=185
Planning:
  Buffers: shared hit=14
Planning Time: 0.112 ms
Execution Time: 9.121 ms
```

The 13,450 lines come from `ix_holding_filing_id` in 212 buffers. Postgres
then hashes all 20,766 securities (185 buffers) rather than probing the
security index 13,450 times, and sorts in memory: 1.7 MB, inside the 4 MB
`work_mem`. A sort bigger than `work_mem` says `external merge  Disk:` and
spills to disk. The `include_options=false` variant is in
[the partial index experiments](#partial-indexes).

### 2. `latest_period` — the default for `?period=`

"Period is optional and defaults to the most recent period we have ingested"
(product spec). So any request that leaves it out asks this first.

```sql
SELECT max(period_of_report) AS period_of_report
FROM position_snapshot
```

Before, 21.17 ms on average. The run under `EXPLAIN` took 37.5 ms: how much
of the table it finds in `shared_buffers` changes from run to run, and
`ANALYZE` adds the cost of timing every row.

```
Finalize Aggregate  (cost=31467.99..31468.00 rows=1 width=4) (actual time=36.069..37.520 rows=1 loops=1)
  Buffers: shared hit=12169 read=10976
  I/O Timings: shared read=11.189
  ->  Gather  (cost=31467.78..31467.99 rows=2 width=4) (actual time=36.014..37.517 rows=3 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=12169 read=10976
        I/O Timings: shared read=11.189
        ->  Partial Aggregate  (cost=30467.78..30467.79 rows=1 width=4) (actual time=34.990..34.990 rows=1 loops=3)
              Buffers: shared hit=12169 read=10976
              I/O Timings: shared read=11.189
              ->  Parallel Seq Scan on position_snapshot  (cost=0.00..29003.22 rows=585822 width=4) (actual time=1.128..20.718 rows=468658 loops=3)
                    Buffers: shared hit=12169 read=10976
                    I/O Timings: shared read=11.189
Planning Time: 0.048 ms
Execution Time: 37.535 ms
```

A parallel sequential scan of all 1.4 million rows, by three processes, to
find one value. No index leads with `period_of_report`. The primary key leads
with `filer_id`, and Postgres 16 cannot skip through the leading column of a
B-tree. About half the pages come from outside `shared_buffers`
(`read=10976`), because the table is bigger than it.

After, 0.01 ms:

```
Result  (cost=0.45..0.46 rows=1 width=4) (actual time=0.010..0.010 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1 (returns $0)
    ->  Limit  (cost=0.43..0.45 rows=1 width=4) (actual time=0.010..0.010 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan Backward using ix_position_snapshot_period_of_report_security_id on position_snapshot  (cost=0.43..32196.96 rows=1405974 width=4) (actual time=0.010..0.010 rows=1 loops=1)
                Index Cond: (period_of_report IS NOT NULL)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.046 ms
Execution Time: 0.018 ms
```

`max(x)` with an index on `x` is planned as
`ORDER BY x DESC LIMIT 1`. That is the `InitPlan` with its `Limit`, and the
`IS NOT NULL` condition is how `max` ignores nulls. The scan reads one index
entry from four buffers and never touches the table, because `VACUUM` has
marked every page all-visible (`Heap Fetches: 0`).

Three shapes were tried:

| Index | Size | `latest_period` | `stock_holders` |
| --- | --- | --- | --- |
| none (before) | — | 37.5 ms | 0.14 ms, 100 primary-key probes |
| `(period_of_report)` | 9.5 MB | 0.011 ms | unchanged |
| `(period_of_report, security_id)` | 15 MB | 0.015 ms | 0.110 ms, one probe |
| the same, `INCLUDE (filer_id, shares, value_usd, weight_pct)` | 91 MB | 0.013 ms | 0.085 ms, index-only |

The one-column index is smaller than its row count suggests. There are only
26 distinct periods, and B-tree deduplication (Postgres 13+) stores each value
once with a list of row pointers. The two-column index costs 5 MB
more and also serves `stock_holders`, so that is the one in `0017`. Period
comes first because `max(period_of_report)` needs it first. Both columns are
equalities in `stock_holders`, so either order would serve that query. The
covering variant is [below](#covering-index-include). Times from separate
runs differ by about 10 µs.

### 3. `investor_periods` — `GET /investors/{slug}/periods`

Periods held, with their summary from `mv_filer_summary` and the date of the
latest filing that counts toward each, through `effective_filing`. Unchanged
at 1.07 ms.

```sql
SELECT m.period_of_report, m.portfolio_value_usd, m.position_count,
       m.top10_weight_pct, m.turnover_pct, m.suspect, filed.filed_at
FROM mv_filer_summary m
LEFT JOIN (
    SELECT e.period_of_report, max(f.filed_at) AS filed_at
    FROM effective_filing e
    JOIN filing f ON f.id = e.filing_id
    WHERE e.filer_id = :filer_id
    GROUP BY e.period_of_report
) filed ON filed.period_of_report = m.period_of_report
WHERE m.filer_id = :filer_id
ORDER BY m.period_of_report DESC
```

```
Merge Left Join  (cost=222.25..222.37 rows=20 width=45) (actual time=1.351..1.356 rows=20 loops=1)
  Merge Cond: (m.period_of_report = filed.period_of_report)
  Buffers: shared hit=281
  ->  Sort  (cost=27.61..27.66 rows=20 width=37) (actual time=0.018..0.019 rows=20 loops=1)
        Sort Key: m.period_of_report DESC
        Sort Method: quicksort  Memory: 26kB
        Buffers: shared hit=5
        ->  Bitmap Heap Scan on mv_filer_summary m  (cost=4.43..27.18 rows=20 width=37) (actual time=0.010..0.013 rows=20 loops=1)
              Recheck Cond: (filer_id = '92'::bigint)
              Heap Blocks: exact=3
              Buffers: shared hit=5
              ->  Bitmap Index Scan on uq_mv_filer_summary_filer_id  (cost=0.00..4.43 rows=20 width=0) (actual time=0.007..0.008 rows=20 loops=1)
                    Index Cond: (filer_id = '92'::bigint)
                    Buffers: shared hit=2
  ->  Sort  (cost=194.64..194.64 rows=1 width=12) (actual time=1.332..1.333 rows=20 loops=1)
        Sort Key: filed.period_of_report DESC
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=276
        ->  Subquery Scan on filed  (cost=186.53..194.63 rows=1 width=12) (actual time=1.231..1.329 rows=20 loops=1)
              Buffers: shared hit=276
              ->  GroupAggregate  (cost=186.53..194.62 rows=1 width=12) (actual time=1.231..1.328 rows=20 loops=1)
                    Group Key: ranked.period_of_report
                    Buffers: shared hit=276
                    ->  Nested Loop  (cost=186.53..194.60 rows=1 width=12) (actual time=1.223..1.324 rows=21 loops=1)
                          Buffers: shared hit=276
                          ->  Subquery Scan on ranked  (cost=186.25..186.30 rows=1 width=12) (actual time=1.218..1.298 rows=21 loops=1)
                                Filter: ((ranked.overlap = 'sum'::text) OR (ranked.cik_rank = 1))
                                Buffers: shared hit=213
                                ->  WindowAgg  (cost=186.25..186.28 rows=1 width=85) (actual time=1.217..1.296 rows=21 loops=1)
                                      Buffers: shared hit=213
                                      ->  Sort  (cost=186.25..186.26 rows=1 width=77) (actual time=1.214..1.215 rows=21 loops=1)
                                            Sort Key: c.period_of_report, (COALESCE((fc.priority)::integer, '-1'::integer)) DESC, c.cik DESC
                                            Sort Method: quicksort  Memory: 26kB
                                            Buffers: shared hit=213
                                            ->  Nested Loop Left Join  (cost=179.23..186.24 rows=1 width=77) (actual time=0.998..1.210 rows=21 loops=1)
                                                  Join Filter: (fc.cik = c.cik)
                                                  Buffers: shared hit=213
                                                  ->  Nested Loop  (cost=179.23..183.80 rows=1 width=73) (actual time=0.995..1.160 rows=21 loops=1)
                                                        Buffers: shared hit=192
                                                        ->  Nested Loop Left Join  (cost=179.23..179.54 rows=1 width=64) (actual time=0.990..1.101 rows=21 loops=1)
                                                              Join Filter: ((candidate.cik = c.cik) AND (candidate.period_of_report = c.period_of_report))
                                                              Rows Removed by Join Filter: 418
                                                              Filter: ((candidate.accession_no IS NULL) OR (c.accession_no = candidate.accession_no) OR ((NOT c.replaces_period) AND (ROW(c.filed_at, c.accession_no) > ROW(candidate.filed_at, candidate.accession_no))))
                                                              Rows Removed by Filter: 1
                                                              Buffers: shared hit=129
                                                              CTE candidate
                                                                ->  Seq Scan on filing  (cost=0.00..178.95 rows=12 width=61) (actual time=0.003..0.701 rows=2216 loops=1)
                                                                      Filter: ((filer_id IS NOT NULL) AND (parse_status = ANY ('{ok,suspect}'::text[])) AND ((upper(form_type) = '13F-HR'::text) OR ((upper(form_type) = '13F-HR/A'::text) AND (amendment_kind IS NOT NULL))))
                                                                      Buffers: shared hit=129
                                                              ->  CTE Scan on candidate c  (cost=0.00..0.27 rows=1 width=157) (actual time=0.039..0.088 rows=22 loops=1)
                                                                    Filter: (filer_id = '92'::bigint)
                                                                    Rows Removed by Filter: 2194
                                                                    Buffers: shared hit=5
                                                              ->  Unique  (cost=0.28..0.29 rows=1 width=148) (actual time=0.043..0.045 rows=20 loops=22)
                                                                    Buffers: shared hit=124
                                                                    ->  Sort  (cost=0.28..0.29 rows=1 width=148) (actual time=0.043..0.044 rows=21 loops=22)
                                                                          Sort Key: candidate.cik, candidate.period_of_report, candidate.filed_at DESC, candidate.accession_no DESC
                                                                          Sort Method: quicksort  Memory: 26kB
                                                                          Buffers: shared hit=124
                                                                          ->  CTE Scan on candidate  (cost=0.00..0.27 rows=1 width=148) (actual time=0.003..0.941 rows=21 loops=1)
                                                                                Filter: (replaces_period AND (filer_id = '92'::bigint))
                                                                                Rows Removed by Filter: 2195
                                                                                Buffers: shared hit=124
                                                        ->  Seq Scan on filer f_1  (cost=0.00..4.25 rows=1 width=17) (actual time=0.002..0.003 rows=1 loops=21)
                                                              Filter: (id = '92'::bigint)
                                                              Rows Removed by Filter: 99
                                                              Buffers: shared hit=63
                                                  ->  Seq Scan on filer_cik fc  (cost=0.00..2.42 rows=1 width=21) (actual time=0.002..0.002 rows=1 loops=21)
                                                        Filter: (filer_id = '92'::bigint)
                                                        Rows Removed by Filter: 102
                                                        Buffers: shared hit=21
                          ->  Index Scan using uq_filing_id_period_of_report on filing f  (cost=0.28..8.30 rows=1 width=16) (actual time=0.001..0.001 rows=1 loops=21)
                                Index Cond: (id = ranked.filing_id)
                                Buffers: shared hit=63
Planning Time: 0.232 ms
Execution Time: 1.421 ms
```

Fast, and a good plan to practise reading. Two things in it:

- **An estimate gap ANALYZE cannot fix.** `Seq Scan on filing ... rows=12`
  produces 2,216. The view filters on `upper(form_type)`, Postgres keeps no
  statistics for an expression, and it falls back to a default selectivity.
  Every estimate above it inherits the error, down to `rows=1` on nodes that
  produce 21. See [Statistics](#statistics).
- **The CTE is read whole.** `candidate` is referenced twice inside
  `effective_filing_by_cik`, so Postgres materialises it rather than inlining
  it. The `filer_id = 92` filter is then applied to its output instead of
  being pushed down into the scan, so every request reads all of `filing` to
  keep 22 rows. That costs nothing at 2,216 filings and would cost more at
  two hundred thousand.

### 4. `investor_holdings` — `GET /investors/{slug}/holdings`

The first page of 100. Unchanged at 2.22 ms.

```sql
SELECT s.cusip, s.name AS issuer_name, s.ticker,
       p.shares, p.value_usd, p.weight_pct
FROM position_snapshot p
JOIN security s ON s.id = p.security_id
WHERE p.filer_id = :filer_id AND p.period_of_report = :period
ORDER BY p.value_usd DESC, p.security_id DESC
LIMIT :page_size
```

```
Limit  (cost=10052.09..10052.34 rows=100 width=89) (actual time=3.337..3.344 rows=100 loops=1)
  Buffers: shared hit=374
  ->  Sort  (cost=10052.09..10066.44 rows=5739 width=89) (actual time=3.337..3.340 rows=100 loops=1)
        Sort Key: p.value_usd DESC, p.security_id DESC
        Sort Method: top-N heapsort  Memory: 45kB
        Buffers: shared hit=374
        ->  Hash Join  (cost=9385.57..9832.75 rows=5739 width=89) (actual time=0.893..2.716 rows=6134 loops=1)
              Hash Cond: (s.id = p.security_id)
              Buffers: shared hit=374
              ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=71) (actual time=0.002..0.598 rows=20766 loops=1)
                    Buffers: shared hit=185
              ->  Hash  (cost=9313.83..9313.83 rows=5739 width=26) (actual time=0.879..0.880 rows=6134 loops=1)
                    Buckets: 8192  Batches: 1  Memory Usage: 448kB
                    Buffers: shared hit=189
                    ->  Index Scan Backward using pk_position_snapshot on position_snapshot p  (cost=0.43..9313.83 rows=5739 width=26) (actual time=0.009..0.462 rows=6134 loops=1)
                          Index Cond: ((filer_id = '92'::bigint) AND (period_of_report = '2022-03-31'::date))
                          Buffers: shared hit=189
Planning:
  Buffers: shared hit=12
Planning Time: 0.115 ms
Execution Time: 3.382 ms
```

The primary key finds the 6,134 positions in 189 buffers. The join hashes
those and reads all of `security` past them, then a top-N heapsort keeps 100.
That sort only ever holds 100 rows, which is why it needs 45 kB.

An index on `(filer_id, period_of_report, value_usd DESC, security_id DESC)`
supplies the order. The plan becomes `Limit → Nested Loop → Index Scan`,
which stops after 100 rows and probes `security` 100 times: 0.16 ms. It costs
67 MB, maintained on every rebuild, to save 2 ms on a 2 ms query, so it is
not in `0017`. It is the first thing to try if this endpoint ever matters,
and it would also make every page of a keyset-paginated walk cost the same
as the first.

### 5. `investor_changes` — `GET /investors/{slug}/changes`

Unchanged, 4.37 ms before and 4.88 ms after. That difference is noise between
two runs.

```sql
SELECT s.cusip, s.name AS issuer_name, s.ticker, c.action,
       c.prev_period_of_report, c.prev_shares, c.shares, c.shares_delta,
       c.shares_delta_pct, c.prev_value_usd, c.value_usd, c.value_delta,
       c.weight_delta
FROM position_change c
JOIN security s ON s.id = c.security_id
WHERE c.filer_id = :filer_id AND c.period_of_report = :period
ORDER BY abs(c.value_delta) DESC, c.security_id DESC
LIMIT :page_size
```

```
Limit  (cost=19189.55..19189.80 rows=100 width=160) (actual time=6.969..6.975 rows=100 loops=1)
  Buffers: shared hit=3454
  ->  Sort  (cost=19189.55..19209.40 rows=7939 width=160) (actual time=6.968..6.971 rows=100 loops=1)
        Sort Key: (abs(c.value_delta)) DESC, c.security_id DESC
        Sort Method: top-N heapsort  Memory: 58kB
        Buffers: shared hit=3454
        ->  Hash Join  (cost=652.66..18886.13 rows=7939 width=160) (actual time=2.486..5.615 rows=7352 loops=1)
              Hash Cond: (c.security_id = s.id)
              Buffers: shared hit=3454
              ->  Index Scan Backward using pk_position_change on position_change c  (cost=0.43..18193.20 rows=7939 width=65) (actual time=0.013..1.896 rows=7352 loops=1)
                    Index Cond: ((filer_id = '92'::bigint) AND (period_of_report = '2022-03-31'::date))
                    Buffers: shared hit=3269
              ->  Hash  (cost=392.66..392.66 rows=20766 width=71) (actual time=2.429..2.429 rows=20766 loops=1)
                    Buckets: 32768  Batches: 1  Memory Usage: 1769kB
                    Buffers: shared hit=185
                    ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=71) (actual time=0.003..0.958 rows=20766 loops=1)
                          Buffers: shared hit=185
Planning:
  Buffers: shared hit=12
Planning Time: 0.126 ms
Execution Time: 7.092 ms
```

The same shape as the holdings. The order is `abs(value_delta)`, an
expression, so no plain index can supply it. Captured before the vacuum, the
same plan showed how a rebuild leaves its mark:

```
Bitmap Index Scan on pk_position_change  (cost=0.00..263.55 rows=7512 width=0) (actual time=0.428..0.428 rows=14996 loops=1)
  Index Cond: ((filer_id = '92'::bigint) AND (period_of_report = '2022-03-31'::date))
  Buffers: shared hit=73
```

That is 14,996 index entries for 7,352 live rows. `recompute` deletes and
re-inserts each period, and until vacuum runs, the index still points at the
dead copies. The heap visit discards them.

### 6. `stock_search` — `GET /stocks?q=`

By name only. No ticker is resolved yet, since enrichment is Epic 4, and the
`issuer` table the data model puts this index on does not exist. So the index
is on `security.name` for now.

```sql
SELECT s.id, s.cusip, s.name, s.ticker
FROM security s
WHERE s.name ILIKE :pattern
ORDER BY similarity(s.name, :q) DESC, s.id
LIMIT 20
```

Before, 5.07 ms:

```
Limit  (cost=444.59..444.59 rows=2 width=75) (actual time=5.210..5.211 rows=8 loops=1)
  Buffers: shared hit=185
  ->  Sort  (cost=444.59..444.59 rows=2 width=75) (actual time=5.209..5.210 rows=8 loops=1)
        Sort Key: (similarity(name, 'apple'::text)) DESC, id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=185
        ->  Seq Scan on security s  (cost=0.00..444.58 rows=2 width=75) (actual time=0.007..5.204 rows=8 loops=1)
              Filter: (name ~~* '%apple%'::text)
              Rows Removed by Filter: 20758
              Buffers: shared hit=185
Planning Time: 0.086 ms
Execution Time: 5.220 ms
```

The classic finding: every row read, 20,758 thrown away, 8 kept. A B-tree
cannot help, because it can only find a pattern anchored at the start.
`'%apple%'` is not.

After, 0.02 ms:

```
Limit  (cost=37.63..37.63 rows=2 width=75) (actual time=0.034..0.034 rows=8 loops=1)
  Buffers: shared hit=14
  ->  Sort  (cost=37.63..37.63 rows=2 width=75) (actual time=0.033..0.034 rows=8 loops=1)
        Sort Key: (similarity(name, 'apple'::text)) DESC, id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=14
        ->  Bitmap Heap Scan on security s  (cost=30.21..37.62 rows=2 width=75) (actual time=0.016..0.029 rows=8 loops=1)
              Recheck Cond: (name ~~* '%apple%'::text)
              Heap Blocks: exact=7
              Buffers: shared hit=14
              ->  Bitmap Index Scan on ix_security_name_trgm  (cost=0.00..30.21 rows=2 width=0) (actual time=0.013..0.013 rows=8 loops=1)
                    Index Cond: (name ~~* '%apple%'::text)
                    Buffers: shared hit=7
Planning:
  Buffers: shared hit=1
Planning Time: 0.084 ms
Execution Time: 0.049 ms
```

`pg_trgm` indexes every three-character sequence of every name, and the
pattern's own trigrams narrow the search to names containing all of them. The
`Recheck Cond` confirms each candidate against the real pattern, because
sharing trigrams is necessary but not sufficient. GIN took 0.039 ms and 1.5 MB.
GiST, the other trigram index type, took 0.68 ms and 2.5 MB. GiST can order
by similarity (`<->`), which GIN cannot, so it would be the one to revisit if
the search ever ranks by distance across the whole table rather than sorting
a handful of matches.

A pattern shorter than three characters has no trigrams, and the index scans
everything. The endpoint should require three characters.

### 7. `stock_holders` — `GET /stocks/{ticker}/holders`

```sql
SELECT f.slug, f.display_name, p.shares, p.value_usd, p.weight_pct
FROM position_snapshot p
JOIN filer f ON f.id = p.filer_id
WHERE p.security_id = :security_id AND p.period_of_report = :period
ORDER BY p.value_usd DESC, p.filer_id
LIMIT :page_size
```

Before, 0.11 ms:

```
Limit  (cost=843.60..843.79 rows=77 width=58) (actual time=0.191..0.195 rows=60 loops=1)
  Buffers: shared hit=363
  ->  Sort  (cost=843.60..843.79 rows=77 width=58) (actual time=0.191..0.192 rows=60 loops=1)
        Sort Key: p.value_usd DESC, p.filer_id
        Sort Method: quicksort  Memory: 32kB
        Buffers: shared hit=363
        ->  Nested Loop  (cost=0.43..841.18 rows=77 width=58) (actual time=0.010..0.175 rows=60 loops=1)
              Buffers: shared hit=363
              ->  Seq Scan on filer f  (cost=0.00..4.00 rows=100 width=40) (actual time=0.002..0.007 rows=100 loops=1)
                    Buffers: shared hit=3
              ->  Index Scan using pk_position_snapshot on position_snapshot p  (cost=0.43..8.45 rows=1 width=26) (actual time=0.002..0.002 rows=1 loops=100)
                    Index Cond: ((filer_id = f.id) AND (period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint))
                    Buffers: shared hit=360
Planning:
  Buffers: shared hit=12
Planning Time: 0.108 ms
Execution Time: 0.208 ms
```

The plan worth studying in this document. No index starts with
`security_id`, yet there is no sequential scan. The planner noticed that
`filer` has 100 rows. It loops over them and probes the primary key
`(filer_id, period_of_report, security_id)` with all three values each time:
`loops=100`, three or four buffers per loop. That is a skip scan by hand. It
scales with the number of filers, not the size of the table. At the 4,000
filers the data model once sketched, it would be 4,000 probes.

After, 0.06 ms, through `latest_period`'s index:

```
Limit  (cost=247.80..247.96 rows=61 width=58) (actual time=0.092..0.096 rows=60 loops=1)
  Buffers: shared hit=66
  ->  Sort  (cost=247.80..247.96 rows=61 width=58) (actual time=0.092..0.093 rows=60 loops=1)
        Sort Key: p.value_usd DESC, p.filer_id
        Sort Method: quicksort  Memory: 32kB
        Buffers: shared hit=66
        ->  Hash Join  (cost=10.30..245.99 rows=61 width=58) (actual time=0.031..0.076 rows=60 loops=1)
              Hash Cond: (p.filer_id = f.id)
              Buffers: shared hit=66
              ->  Bitmap Heap Scan on position_snapshot p  (cost=5.05..240.57 rows=61 width=26) (actual time=0.012..0.052 rows=60 loops=1)
                    Recheck Cond: ((period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint))
                    Heap Blocks: exact=60
                    Buffers: shared hit=63
                    ->  Bitmap Index Scan on ix_position_snapshot_period_of_report_security_id  (cost=0.00..5.04 rows=61 width=0) (actual time=0.008..0.008 rows=60 loops=1)
                          Index Cond: ((period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint))
                          Buffers: shared hit=3
              ->  Hash  (cost=4.00..4.00 rows=100 width=40) (actual time=0.015..0.015 rows=100 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 16kB
                    Buffers: shared hit=3
                    ->  Seq Scan on filer f  (cost=0.00..4.00 rows=100 width=40) (actual time=0.002..0.007 rows=100 loops=1)
                          Buffers: shared hit=3
Planning:
  Buffers: shared hit=12
Planning Time: 0.097 ms
Execution Time: 0.117 ms
```

One probe finds the 60 entries, then 60 heap pages are visited, because a
stock's holders are on different pages.

### 8. `stock_holder_changes` — `GET /stocks/{ticker}/holders/changes`

"Who added, trimmed, opened, exited" (product spec): every action but `hold`.

```sql
SELECT f.slug, f.display_name, c.action, c.prev_shares, c.shares,
       c.shares_delta, c.shares_delta_pct, c.prev_value_usd, c.value_usd,
       c.value_delta
FROM position_change c
JOIN filer f ON f.id = c.filer_id
WHERE c.security_id = :security_id AND c.period_of_report = :period
  AND c.action <> 'hold'
ORDER BY abs(c.value_delta) DESC, c.filer_id
LIMIT :page_size
```

Before, 0.15 ms: the same 100-probe loop, with the hold filter applied to
each row found.

```
Limit  (cost=842.98..843.10 rows=47 width=119) (actual time=0.233..0.237 rows=58 loops=1)
  Buffers: shared hit=364
  ->  Sort  (cost=842.98..843.10 rows=47 width=119) (actual time=0.233..0.234 rows=58 loops=1)
        Sort Key: (abs(c.value_delta)) DESC, c.filer_id
        Sort Method: quicksort  Memory: 33kB
        Buffers: shared hit=364
        ->  Nested Loop  (cost=0.43..841.68 rows=47 width=119) (actual time=0.012..0.210 rows=58 loops=1)
              Buffers: shared hit=364
              ->  Seq Scan on filer f  (cost=0.00..4.00 rows=100 width=40) (actual time=0.002..0.010 rows=100 loops=1)
                    Buffers: shared hit=3
              ->  Index Scan using pk_position_change on position_change c  (cost=0.43..8.45 rows=1 width=55) (actual time=0.002..0.002 rows=1 loops=100)
                    Index Cond: ((filer_id = f.id) AND (period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint))
                    Filter: (action <> 'hold'::text)
                    Rows Removed by Filter: 0
                    Buffers: shared hit=361
Planning:
  Buffers: shared hit=12
Planning Time: 0.101 ms
Execution Time: 0.253 ms
```

After, 0.09 ms, through the partial index:

```
Limit  (cost=316.01..316.21 rows=78 width=119) (actual time=0.105..0.108 rows=58 loops=1)
  Buffers: shared hit=64
  ->  Sort  (cost=316.01..316.21 rows=78 width=119) (actual time=0.104..0.106 rows=58 loops=1)
        Sort Key: (abs(c.value_delta)) DESC, c.filer_id
        Sort Method: quicksort  Memory: 33kB
        Buffers: shared hit=64
        ->  Hash Join  (cost=10.48..313.56 rows=78 width=119) (actual time=0.036..0.084 rows=58 loops=1)
              Hash Cond: (c.filer_id = f.id)
              Buffers: shared hit=64
              ->  Bitmap Heap Scan on position_change c  (cost=5.23..307.90 rows=78 width=55) (actual time=0.014..0.054 rows=58 loops=1)
                    Recheck Cond: ((period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint) AND (action <> 'hold'::text))
                    Heap Blocks: exact=58
                    Buffers: shared hit=61
                    ->  Bitmap Index Scan on ix_position_change_period_of_report_security_id_not_hold  (cost=0.00..5.21 rows=78 width=0) (actual time=0.009..0.009 rows=58 loops=1)
                          Index Cond: ((period_of_report = '2022-03-31'::date) AND (security_id = '47'::bigint))
                          Buffers: shared hit=3
              ->  Hash  (cost=4.00..4.00 rows=100 width=40) (actual time=0.018..0.018 rows=100 loops=1)
                    Buckets: 1024  Batches: 1  Memory Usage: 16kB
                    Buffers: shared hit=3
                    ->  Seq Scan on filer f  (cost=0.00..4.00 rows=100 width=40) (actual time=0.003..0.009 rows=100 loops=1)
                          Buffers: shared hit=3
Planning:
  Buffers: shared hit=4
Planning Time: 0.113 ms
Execution Time: 0.137 ms
```

The index's `WHERE` is in the `Recheck Cond` and absent from the
`Index Cond`. The planner proved that the query's `action <> 'hold'` implies
the index's, which is the condition for using a partial index at all. What it
saves is in [Partial indexes](#partial-indexes).

### 9. `market_flows` — `GET /market/flows`

Unchanged at 4.36 ms.

```sql
SELECT s.cusip, s.name AS issuer_name, s.ticker, q.bought_value_usd,
       q.sold_value_usd, q.net_value_usd, q.net_shares, q.new_positions,
       q.exits, q.buyer_count, q.seller_count, q.suspect
FROM mv_quarter_flows q
JOIN security s ON s.id = q.security_id
WHERE q.period_of_report = :period
ORDER BY q.net_value_usd DESC, q.security_id
LIMIT :page_size
```

```
Limit  (cost=5953.34..5953.59 rows=100 width=132) (actual time=6.121..6.129 rows=100 loops=1)
  Buffers: shared hit=493
  ->  Sort  (cost=5953.34..5980.03 rows=10677 width=132) (actual time=6.121..6.124 rows=100 loops=1)
        Sort Key: q.net_value_usd DESC, q.security_id
        Sort Method: top-N heapsort  Memory: 67kB
        Buffers: shared hit=493
        ->  Hash Join  (cost=5098.09..5545.27 rows=10677 width=132) (actual time=2.305..4.677 rows=10964 loops=1)
              Hash Cond: (s.id = q.security_id)
              Buffers: shared hit=493
              ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=71) (actual time=0.001..0.638 rows=20766 loops=1)
                    Buffers: shared hit=185
              ->  Hash  (cost=4964.63..4964.63 rows=10677 width=69) (actual time=2.286..2.287 rows=10964 loops=1)
                    Buckets: 16384  Batches: 1  Memory Usage: 1332kB
                    Buffers: shared hit=308
                    ->  Bitmap Heap Scan on mv_quarter_flows q  (cost=387.17..4964.63 rows=10677 width=69) (actual time=0.222..1.092 rows=10964 loops=1)
                          Recheck Cond: (period_of_report = '2022-03-31'::date)
                          Heap Blocks: exact=228
                          Buffers: shared hit=308
                          ->  Bitmap Index Scan on uq_mv_quarter_flows_period_of_report  (cost=0.00..384.50 rows=10677 width=0) (actual time=0.214..0.214 rows=10964 loops=1)
                                Index Cond: (period_of_report = '2022-03-31'::date)
                                Buffers: shared hit=80
Planning:
  Buffers: shared hit=6
Planning Time: 0.087 ms
Execution Time: 6.216 ms
```

The view's unique index finds the period's rows. Three processes each sort
their share, and `Gather Merge` interleaves the three sorted streams. A
`Nested Loop` then fetches 100 securities. `Memoize` caches each lookup in
case a key repeats, which none does here (`Hits: 0`). An index on
`(period_of_report, net_value_usd DESC)` would make this a 100-row index
scan. At 4 ms that is not worth slowing every refresh for.

### 10. `market_crowded` — `GET /market/crowded`

Unchanged at 3.27 ms, the same shape as `market_flows`.

```sql
SELECT s.cusip, s.name AS issuer_name, s.ticker, c.holder_count,
       c.total_value_usd, c.avg_weight_pct, c.median_weight_pct,
       c.value_rank, c.suspect
FROM mv_consensus_holdings c
JOIN security s ON s.id = c.security_id
WHERE c.period_of_report = :period
ORDER BY c.holder_count DESC, c.value_rank, c.security_id
LIMIT :page_size
```

```
Limit  (cost=5160.62..5160.87 rows=100 width=107) (actual time=4.937..4.943 rows=100 loops=1)
  Buffers: shared hit=394
  ->  Sort  (cost=5160.62..5186.16 rows=10216 width=107) (actual time=4.936..4.939 rows=100 loops=1)
        Sort Key: c.holder_count DESC, c.value_rank, c.security_id
        Sort Method: top-N heapsort  Memory: 45kB
        Buffers: shared hit=394
        ->  Hash Join  (cost=4322.99..4770.18 rows=10216 width=107) (actual time=1.790..4.003 rows=10051 loops=1)
              Hash Cond: (s.id = c.security_id)
              Buffers: shared hit=394
              ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=71) (actual time=0.002..0.635 rows=20766 loops=1)
                    Buffers: shared hit=185
              ->  Hash  (cost=4195.29..4195.29 rows=10216 width=44) (actual time=1.768..1.769 rows=10051 loops=1)
                    Buckets: 16384  Batches: 1  Memory Usage: 988kB
                    Buffers: shared hit=209
                    ->  Bitmap Heap Scan on mv_consensus_holdings c  (cost=371.59..4195.29 rows=10216 width=44) (actual time=0.198..0.895 rows=10051 loops=1)
                          Recheck Cond: (period_of_report = '2022-03-31'::date)
                          Heap Blocks: exact=128
                          Buffers: shared hit=209
                          ->  Bitmap Index Scan on uq_mv_consensus_holdings_period_of_report  (cost=0.00..369.04 rows=10216 width=0) (actual time=0.193..0.193 rows=10051 loops=1)
                                Index Cond: (period_of_report = '2022-03-31'::date)
                                Buffers: shared hit=81
Planning:
  Buffers: shared hit=6
Planning Time: 0.075 ms
Execution Time: 5.009 ms
```

## Experiments

Each was run on the full data and then undone. `DROP INDEX` and
`ALTER TABLE ... DROP CONSTRAINT` are transactional in Postgres, so a drop
inside `BEGIN ... ROLLBACK` shows the plan without the index and then puts it
back. Note that it holds an exclusive lock on the table until the rollback.
An index created only to be measured is named `exp_*` in these plans, and was
dropped afterwards.

### Dropping an index you are sure is needed

`pk_position_snapshot`, under `investor_holdings`:

```sql
BEGIN;
ALTER TABLE position_snapshot DROP CONSTRAINT pk_position_snapshot;
EXPLAIN (ANALYZE, BUFFERS) ...;  -- investor_holdings
ROLLBACK;
```

```
Limit  (cost=33066.83..33116.30 rows=100 width=89) (actual time=20.423..21.597 rows=100 loops=1)
  Buffers: shared hit=1636 read=21895
  I/O Timings: shared read=21.300
  ->  Nested Loop  (cost=33066.83..35905.63 rows=5739 width=89) (actual time=20.422..21.592 rows=100 loops=1)
        Buffers: shared hit=1636 read=21895
        I/O Timings: shared read=21.300
        ->  Gather Merge  (cost=33066.54..33734.94 rows=5739 width=26) (actual time=20.403..21.470 rows=100 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=1336 read=21895
              I/O Timings: shared read=21.300
              ->  Sort  (cost=32066.51..32072.49 rows=2391 width=26) (actual time=19.218..19.237 rows=786 loops=3)
                    Sort Key: p.value_usd DESC, p.security_id DESC
                    Sort Method: quicksort  Memory: 232kB
                    Buffers: shared hit=1336 read=21895
                    I/O Timings: shared read=21.300
                    Worker 0:  Sort Method: quicksort  Memory: 225kB
                    Worker 1:  Sort Method: quicksort  Memory: 119kB
                    ->  Parallel Seq Scan on position_snapshot p  (cost=0.00..31932.34 rows=2391 width=26) (actual time=14.282..18.796 rows=2045 loops=3)
                          Filter: ((filer_id = 92) AND (period_of_report = '2022-03-31'::date))
                          Rows Removed by Filter: 466613
                          Buffers: shared hit=1250 read=21895
                          I/O Timings: shared read=21.300
        ->  Memoize  (cost=0.30..0.49 rows=1 width=71) (actual time=0.001..0.001 rows=1 loops=100)
              Cache Key: p.security_id
              Cache Mode: logical
              Hits: 0  Misses: 100  Evictions: 0  Overflows: 0  Memory Usage: 15kB
              Buffers: shared hit=300
              ->  Index Scan using pk_security on security s  (cost=0.29..0.48 rows=1 width=71) (actual time=0.001..0.001 rows=1 loops=100)
                    Index Cond: (id = p.security_id)
                    Buffers: shared hit=300
Planning:
  Buffers: shared hit=12
Planning Time: 0.145 ms
Execution Time: 21.664 ms
```

That is 2.2 ms becoming 21.7 ms, and 23,000 buffers instead of 374. Each of
the three processes threw away 466,613 rows. The join changed too. With the
rows now arriving sorted from the `Gather Merge`, the planner probes
`security` for the first 100 instead of hashing the whole table.

### Adding one you are sure is useless

`CREATE INDEX ON holding (put_call)`: three values, one of them null on 82%
of rows. On a query shaped like the API's, one filing's lines filtered by
`put_call`, the planner ignored it and kept the filing index:

```
Aggregate  (cost=511.11..511.12 rows=1 width=8) (actual time=0.950..0.950 rows=1 loops=1)
  Buffers: shared hit=212
  ->  Index Scan using ix_holding_filing_id on holding  (cost=0.43..508.04 rows=1229 width=0) (actual time=0.010..0.851 rows=3820 loops=1)
        Index Cond: (filing_id = 294)
        Filter: (put_call = 'Call'::text)
        Rows Removed by Filter: 9630
        Buffers: shared hit=212
Planning Time: 0.024 ms
Execution Time: 0.957 ms
```

But it was not useless to everything. `SELECT count(*) FROM holding WHERE
put_call IS NULL` used it, for 82% of the table:

```
Finalize Aggregate  (cost=25559.55..25559.56 rows=1 width=8) (actual time=32.491..33.361 rows=1 loops=1)
  Buffers: shared hit=1290
  ->  Gather  (cost=25559.33..25559.54 rows=2 width=8) (actual time=32.375..33.359 rows=3 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=1290
        ->  Partial Aggregate  (cost=24559.33..24559.34 rows=1 width=8) (actual time=31.315..31.315 rows=1 loops=3)
              Buffers: shared hit=1290
              ->  Parallel Index Only Scan using exp_h on holding  (cost=0.43..22970.24 rows=635636 width=0) (actual time=0.012..18.602 rows=508752 loops=3)
                    Index Cond: (put_call IS NULL)
                    Heap Fetches: 0
                    Buffers: shared hit=1290
Planning Time: 0.045 ms
Execution Time: 33.380 ms
```

A count needs no columns, and reading a 12 MB index instead of a 216 MB table
is cheaper whatever the selectivity. Deduplication again: 1.9 million entries
in 12 MB. `WHERE put_call = 'Put'`, 8% of rows, used it too, in 8.8 ms. So a
"useless" index gets used. What makes it useless is that nothing anyone runs
benefits.

### Proving an index unnecessary

**`ix_holding_filer_id_period_of_report`, dropped.** Three kinds of evidence:

- **Usage.** `pg_stat_user_indexes.idx_scan` was 0 after the full backfill,
  with 2,215 loads each rebuilding its period, and stayed 0 through
  `recompute --all`, `check-data`, `audit-overlaps`, `audit-amendments` and
  every API query. Those reads hit `ix_holding_filing_id` 3,232 times.
- **Design.** No correct query can use it.
  [`effective.py`](../app/db/queries/effective.py) requires every per-filer
  read of holdings to go through `effective_filing` by `filing_id`, because
  grouping on `holding.filer_id` counts a restatement twice. And the per-filer
  read path is `position_snapshot`.
- **Cost.** 14 MB, and a write on every one of 1.9 million inserts.

What it could still have served is the foreign-key check when a `filer` row
is deleted. Nothing deletes filers, and if someone does by hand, that check
becomes a scan of `holding`.

**`ix_holding_filing_id`, kept, though it looks redundant.** It repeats the
leading column of `uq_holding_filing_id_cusip_put_call_sshprnamt_type`, which
can serve the same lookups. Dropped in a transaction:

```
Index Scan using uq_holding_filing_id_cusip_put_call_sshprnamt_type on holding h  (cost=0.43..19842.47 rows=13491 width=53) (actual time=0.009..1.094 rows=13450 loops=1)
  Index Cond: (filing_id = 294)
  Buffers: shared hit=1452
```

against, with it (from `filing_holdings` above):

```
Index Scan using ix_holding_filing_id on holding h  (cost=0.43..495.11 rows=13491 width=53) (actual time=0.008..0.724 rows=13450 loops=1)
  Index Cond: (filing_id = '294'::bigint)
  Buffers: shared hit=212
```

The same rows cost 1,452 buffers instead of 212, and the planner's estimate
is 40 times higher. The unique index is 135 MB of wide entries in CUSIP order,
so it visits the filing's rows in CUSIP order, jumping between heap pages. The
small index is 14 MB of deduplicated entries whose row pointers are in
physical order, so each heap page is read once. The planner's cost model
assumes as much: it discounts how closely a multi-column index follows the
table's order. "Its columns are a prefix of another index" is a reason to
look, not a proof.

**Not dropped, worth a look:** `ix_holding_cusip`, 17 MB with 0 scans. Its
comment keeps it for ad-hoc debugging ("the way a position is found when a
security row is suspected wrong"), and whether that is worth 17 MB is a
judgement call rather than a measurement. `ix_filing_cik` and
`ix_filing_filed_at` are unused too, but `filing` has 2,216 rows and they
cost nothing.

### Partial indexes

A partial index stores only the rows matching its `WHERE`, and only a query
whose own `WHERE` implies it can use it. What it saves is proportional to the
rows it leaves out.

**`position_change ... WHERE action <> 'hold'`, kept.** Holds are 6.5% of
changes: the ±0.01% hold band is narrow, and most positions drift by more.
So it saves little:

| | Size | `stock_holder_changes` | Note |
| --- | --- | --- | --- |
| full `(period_of_report, security_id)` | 17 MB | 0.110 ms | `Rows Removed by Filter: 3` |
| partial `WHERE action <> 'hold'` | 16 MB | 0.118 ms | no filter |

The two times are the same within noise, and the partial index is 1 MB
smaller. A query that includes holds cannot use it: drop `AND action <> 'hold'`
and the plan falls back to the 100-probe loop. It is in `0017` because it is
the right shape for the only query that wants this index. It would earn more
on a predicate that leaves out most of a table.

**`holding ... WHERE put_call IS NULL`, not kept.** This was for
`filing_holdings` with `include_options=false`: an index on
`(filing_id, value_usd DESC, cusip, put_call, sshprnamt_type)`, so the rows
come out of the index already in the response's order. The planner never
used it. It kept `ix_holding_filing_id`, filtered out the 7,191 options and
sorted:

```
Sort  (cost=1920.31..1948.00 rows=11074 width=98) (actual time=4.840..5.000 rows=6259 loops=1)
  Sort Key: h.value_usd DESC, h.cusip, h.put_call, h.sshprnamt_type
  Sort Method: quicksort  Memory: 808kB
  Buffers: shared hit=397
  ->  Hash Join  (cost=652.66..1176.42 rows=11074 width=98) (actual time=1.957..3.424 rows=6259 loops=1)
        Hash Cond: (h.security_id = s.id)
        Buffers: shared hit=397
        ->  Index Scan using ix_holding_filing_id on holding h  (cost=0.43..495.11 rows=11074 width=53) (actual time=0.006..0.846 rows=6259 loops=1)
              Index Cond: (filing_id = 294)
              Filter: (put_call IS NULL)
              Rows Removed by Filter: 7191
              Buffers: shared hit=212
        ->  Hash  (cost=392.66..392.66 rows=20766 width=61) (actual time=1.924..1.924 rows=20766 loops=1)
              Buckets: 32768  Batches: 1  Memory Usage: 1573kB
              Buffers: shared hit=185
              ->  Seq Scan on security s  (cost=0.00..392.66 rows=20766 width=61) (actual time=0.002..0.796 rows=20766 loops=1)
                    Buffers: shared hit=185
Planning:
  Buffers: shared hit=14
Planning Time: 0.089 ms
Execution Time: 5.186 ms
```

The index supplies an order, and the join above it does not keep one. A hash
join's output is in no order the planner relies on, and the alternative,
6,259 nested-loop probes into `security`, costs more than the sort it saves.
That was 86 MB for nothing. The `put_call IS NULL` filter also leaves out
only 18% of `holding`. And `position_snapshot` is already that slice of it:
common stock only, aggregated and materialised. So every common-stock read in
the API goes there and never comes to `holding` at all.

### Covering index (INCLUDE)

`INCLUDE` adds columns to an index's leaf entries without making them part
of the key, so a query that needs only those columns can be answered from the
index alone (`Index Only Scan`). Tried on `stock_holders` with
`(period_of_report, security_id) INCLUDE (filer_id, shares, value_usd, weight_pct)`:

| | Size | Buffers | Time |
| --- | --- | --- | --- |
| plain, the index in `0017` | 15 MB | 66 | 0.117 ms |
| covering, freshly vacuumed | 91 MB | 8 | 0.079 ms |
| covering, after the rows are rewritten | 91 MB | 68 | 0.075 ms |
| covering, vacuumed again | 91 MB | 8 | 0.072 ms |

**It helped, by about 40 µs, for six times the size. Not kept.** An
index-only scan can skip the table only for pages the visibility map marks
all-visible, and any write to a page clears its mark until vacuum runs again.
Rewriting the 60 rows was enough:

```
Index Only Scan using exp_c on position_snapshot p  (cost=0.43..5.97 rows=77 width=26) (actual time=0.005..0.018 rows=60 loops=1)
  Index Cond: ((period_of_report = '2022-03-31'::date) AND (security_id = 47))
  Heap Fetches: 60
  Buffers: shared hit=65
```

`recompute` deletes and re-inserts whole periods, and every refresh of a
materialised view writes the rows that changed. So in this schema,
`Heap Fetches` is high after every publish until autovacuum gets there, and
the covering index buys less than these numbers suggest. It pays where reads are hot, the table is
large, the table is rarely written, and the extra columns are narrow. Here
the heap pages were in cache anyway.

### Statistics

**Stale.** Right after `recompute --all`, `pg_class.reltuples` for
`position_snapshot` still said 461,897 rows. The last autoanalyze ran
mid-backfill, and the table now held 1,405,974 live rows and 471,003 dead
ones. The estimates mostly survived it, because the planner scales
`reltuples` by the table's current size in pages. The dead rows did not:
`investor_changes` visited twice the index entries it returned.
`VACUUM (ANALYZE)` fixed that. It did not make `latest_period` read fewer
pages, because VACUUM marks dead space for reuse rather than giving it back,
and the scan reads every page either way. After a bulk rebuild, run it rather
than wait for autovacuum.

**Correlated columns.** `holding WHERE filing_id = 294 AND period_of_report =
'2022-03-31'` is the lookup an `ON DELETE CASCADE` from `filing` runs. It was
estimated at 672 rows and returned 13,450. The planner multiplies the two
columns' selectivities as if they were independent, but a filing's period is
a function of the filing. Extended statistics teach it that:

```sql
CREATE STATISTICS ... (dependencies) ON filing_id, period_of_report FROM holding;
ANALYZE holding;
-- rows=14729, against 13450 actual
```

**An expression.** `effective_filing` filters on `upper(form_type)`, which has
no statistics, so the estimate was 12 rows against 2,216.
`CREATE STATISTICS ... ON (upper(form_type)) FROM filing` (Postgres 14+)
brought it to 2,023.

Neither is in `0017`. Both fixed the estimate and changed no plan of the
ten, which were already right. They are worth trying against `recompute`.
Its two rebuild queries go through `effective_filing` over every filing, take
22 s each, and could be planned from estimates that are wrong by 200 times.

## What it costs

- **Space.** +15 MB, +16 MB and +1.5 MB, −14 MB.
- **Writes.** `recompute --all --include-suspect` took 50.6 s before `0017`
  and 53.9 s after, including the view refresh. That is one run each, so read
  it as about 3 s, or 6%, for maintaining two more indexes on the derived
  tables.
- **Migration.** `0017` built its indexes on the full data in under two
  seconds. Not `CONCURRENTLY`, which cannot run inside the migration's
  transaction. Each table is locked against writes while its index builds.

## Changed alongside

- **`docker-compose.yml`** loads pg_stat_statements and turns on
  `track_io_timing`. It also gives the `db` container `shm_size: 256mb`. A
  manual `VACUUM` of the full dataset vacuums its indexes with parallel
  workers, which share memory through `/dev/shm`, and Docker's 64 MB default
  failed it: "could not resize shared memory segment ... No space left on
  device". Recreate the container to pick these up: `docker compose up -d db`.
- **`make explain`** runs the script. `--only NAME`, `--runs N`, `--filer`,
  `--period`, `--cusip`, `--accession` and `--q` change what it measures, and
  `--sql` prints each query. It exits 1 if any median is over `--budget-ms`
  (100).

## Not done

- **Generic plans.** The API runs these as prepared statements, and after
  five executions Postgres may switch to a generic plan that ignores the
  parameter values. The timings include whatever it chose. The plans shown
  are custom ones, with the values in them. A skewed parameter, such as a
  filer with 40 positions against one with 6,000, is where a generic plan
  could go wrong. Nothing here showed it, but this is where to look if a
  query is fast under `EXPLAIN` and slow in the API.
- **`/stocks/{ticker}`.** Once Epic 4 resolves tickers, `security.ticker`
  needs an index, or every lookup is a sequential scan of `security`.
- **The issuer table** takes over `ix_security_name_trgm`'s job when it
  arrives, as the data model plans.
