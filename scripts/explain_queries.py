"""The queries behind the API, under ``EXPLAIN (ANALYZE, BUFFERS)``, and how long each takes.

docs/query-performance.md is the write-up. This is how its plans were captured,
and how to capture them again after the data or an index changes::

    make explain                                   # all ten: timing, then plan
    make explain a="--only stock_holders --runs 50"
    make explain a="--filer renaissance --period 2025-06-30"

Only ``GET /filings/{accession_no}`` is built. The other nine are the queries
the endpoints sketched in docs/product-spec.md ("API surface") will issue:
their ``WHERE`` and ``ORDER BY`` are what the indexes are designed against,
and the select lists are a guess. When an endpoint is built, its query belongs
in the app and this list should read it from there.

Each query runs ``--runs`` times on one connection, as the API would run it: a
prepared statement with bound parameters. So after five runs Postgres may
switch it to a generic plan, as it would in production. Then it runs once more
under ``EXPLAIN``, warm, which is the steady state of an API reading the same
few hundred megabytes all day. A cold cache shows up in a plan as ``shared
read`` and in the I/O timings beside it.

The parameters default to the worst case the data has: the period with the most
positions, the filer with the most positions in it, the security with the most
holders in it, and that filer's largest filing.

Needs the dev database, and pg_stat_statements for the server-side mean, which
is reported as unavailable without it. Run it in the api container, which is
where ``POSTGRES_HOST`` resolves.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.db.session import create_engine


@dataclass(frozen=True, slots=True)
class Query:
    """One query an endpoint issues, with ``:name`` parameters."""

    name: str
    endpoint: str
    sql: str

    @property
    def tag(self) -> str:
        """A comment at the head of the statement, which is how its row in
        pg_stat_statements is found again: the view keeps the text as sent."""
        return f"/* api:{self.name} */"

    @property
    def statement(self) -> str:
        return f"{self.tag}\n{self.sql.strip()}"


QUERIES: Final = (
    Query(
        "filing_holdings",
        "GET /filings/{accession_no}",
        # app/api/routers/filings.py, _read_holdings, include_options=true.
        """
        SELECT h.cusip, s.name AS issuer_name, s.ticker, h.value_usd, h.shares,
               h.sshprnamt_type, h.put_call, h.investment_discretion,
               h.voting_sole, h.voting_shared, h.voting_none
        FROM holding h
        JOIN security s ON s.id = h.security_id
        WHERE h.filing_id = :filing_id
        ORDER BY h.value_usd DESC, h.cusip, h.put_call, h.sshprnamt_type
        """,
    ),
    Query(
        "latest_period",
        "?period= omitted, on /stocks/{ticker}/holders",
        # "period is optional and defaults to the most recent period we have
        # ingested" (product spec): one of these in front of every such request.
        """
        SELECT max(period_of_report) AS period_of_report
        FROM position_snapshot
        """,
    ),
    Query(
        "investor_periods",
        "GET /investors/{slug}/periods",
        """
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
        """,
    ),
    Query(
        "investor_holdings",
        "GET /investors/{slug}/holdings?period=",
        # The first page. Keyset pagination continues from the last row's
        # (value_usd, security_id), so the order has to be total.
        """
        SELECT s.cusip, s.name AS issuer_name, s.ticker,
               p.shares, p.value_usd, p.weight_pct
        FROM position_snapshot p
        JOIN security s ON s.id = p.security_id
        WHERE p.filer_id = :filer_id AND p.period_of_report = :period
        ORDER BY p.value_usd DESC, p.security_id DESC
        LIMIT :page_size
        """,
    ),
    Query(
        "investor_changes",
        "GET /investors/{slug}/changes?period=",
        """
        SELECT s.cusip, s.name AS issuer_name, s.ticker, c.action,
               c.prev_period_of_report, c.prev_shares, c.shares, c.shares_delta,
               c.shares_delta_pct, c.prev_value_usd, c.value_usd, c.value_delta,
               c.weight_delta
        FROM position_change c
        JOIN security s ON s.id = c.security_id
        WHERE c.filer_id = :filer_id AND c.period_of_report = :period
        ORDER BY abs(c.value_delta) DESC, c.security_id DESC
        LIMIT :page_size
        """,
    ),
    Query(
        "stock_search",
        "GET /stocks?q=",
        # By name only: no ticker is resolved yet (Epic 4), and the issuer
        # table the data model puts name_trgm on does not exist.
        """
        SELECT s.id, s.cusip, s.name, s.ticker
        FROM security s
        WHERE s.name ILIKE :pattern
        ORDER BY similarity(s.name, :q) DESC, s.id
        LIMIT 20
        """,
    ),
    Query(
        "stock_holders",
        "GET /stocks/{ticker}/holders?period=",
        """
        SELECT f.slug, f.display_name, p.shares, p.value_usd, p.weight_pct
        FROM position_snapshot p
        JOIN filer f ON f.id = p.filer_id
        WHERE p.security_id = :security_id AND p.period_of_report = :period
        ORDER BY p.value_usd DESC, p.filer_id
        LIMIT :page_size
        """,
    ),
    Query(
        "stock_holder_changes",
        "GET /stocks/{ticker}/holders/changes?period=",
        # "Who added, trimmed, opened, exited": every action but hold.
        """
        SELECT f.slug, f.display_name, c.action, c.prev_shares, c.shares,
               c.shares_delta, c.shares_delta_pct, c.prev_value_usd, c.value_usd,
               c.value_delta
        FROM position_change c
        JOIN filer f ON f.id = c.filer_id
        WHERE c.security_id = :security_id AND c.period_of_report = :period
          AND c.action <> 'hold'
        ORDER BY abs(c.value_delta) DESC, c.filer_id
        LIMIT :page_size
        """,
    ),
    Query(
        "market_flows",
        "GET /market/flows?period=",
        """
        SELECT s.cusip, s.name AS issuer_name, s.ticker, q.bought_value_usd,
               q.sold_value_usd, q.net_value_usd, q.net_shares, q.new_positions,
               q.exits, q.buyer_count, q.seller_count, q.suspect
        FROM mv_quarter_flows q
        JOIN security s ON s.id = q.security_id
        WHERE q.period_of_report = :period
        ORDER BY q.net_value_usd DESC, q.security_id
        LIMIT :page_size
        """,
    ),
    Query(
        "market_crowded",
        "GET /market/crowded?period=",
        """
        SELECT s.cusip, s.name AS issuer_name, s.ticker, c.holder_count,
               c.total_value_usd, c.avg_weight_pct, c.median_weight_pct,
               c.value_rank, c.suspect
        FROM mv_consensus_holdings c
        JOIN security s ON s.id = c.security_id
        WHERE c.period_of_report = :period
        ORDER BY c.holder_count DESC, c.value_rank, c.security_id
        LIMIT :page_size
        """,
    ),
)


# --- parameters ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Parameters:
    period: date
    filer_id: int
    filer_slug: str
    security_id: int
    cusip: str
    filing_id: int
    accession_no: str
    q: str
    page_size: int

    def bind(self) -> dict[str, Any]:
        """Every parameter any query takes. Each takes the ones it names."""
        return {
            "period": self.period,
            "filer_id": self.filer_id,
            "security_id": self.security_id,
            "filing_id": self.filing_id,
            "q": self.q,
            "pattern": f"%{self.q}%",
            "page_size": self.page_size,
        }


async def resolve_parameters(connection: AsyncConnection, args: argparse.Namespace) -> Parameters:
    """The worst case, unless the arguments name something else."""
    period: date | None = args.period
    if period is None:
        period = await connection.scalar(
            text(
                "SELECT period_of_report FROM mv_filer_summary GROUP BY period_of_report "
                "ORDER BY sum(position_count) DESC, period_of_report DESC LIMIT 1"
            )
        )
        if period is None:
            raise SystemExit("mv_filer_summary is empty: backfill, then refresh-views")

    if args.filer is None:
        filer = (
            await connection.execute(
                text(
                    "SELECT f.id, f.slug FROM mv_filer_summary m JOIN filer f ON f.id = m.filer_id "
                    "WHERE m.period_of_report = :period ORDER BY m.position_count DESC LIMIT 1"
                ),
                {"period": period},
            )
        ).one()
    else:
        filer = (
            await connection.execute(
                text("SELECT id, slug FROM filer WHERE slug = :slug"), {"slug": args.filer}
            )
        ).one()

    if args.cusip is None:
        security = (
            await connection.execute(
                text(
                    "SELECT s.id, s.cusip FROM mv_consensus_holdings c "
                    "JOIN security s ON s.id = c.security_id WHERE c.period_of_report = :period "
                    "ORDER BY c.holder_count DESC, c.value_rank LIMIT 1"
                ),
                {"period": period},
            )
        ).one()
    else:
        security = (
            await connection.execute(
                text("SELECT id, cusip FROM security WHERE cusip = :cusip"), {"cusip": args.cusip}
            )
        ).one()

    if args.accession is None:
        filing = (
            await connection.execute(
                text(
                    "SELECT f.id, f.accession_no FROM filing f "
                    "JOIN LATERAL (SELECT count(*) AS n FROM holding h WHERE h.filing_id = f.id) h "
                    "ON true WHERE f.filer_id = :filer_id AND f.period_of_report = :period "
                    "ORDER BY h.n DESC LIMIT 1"
                ),
                {"filer_id": filer.id, "period": period},
            )
        ).one()
    else:
        filing = (
            await connection.execute(
                text("SELECT id, accession_no FROM filing WHERE accession_no = :accession_no"),
                {"accession_no": args.accession},
            )
        ).one()

    return Parameters(
        period=period,
        filer_id=filer.id,
        filer_slug=filer.slug,
        security_id=security.id,
        cusip=security.cusip,
        filing_id=filing.id,
        accession_no=filing.accession_no,
        q=args.q,
        page_size=args.page_size,
    )


# --- measuring -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Timing:
    rows: int
    client_ms: list[float]
    """Wall clock per run, rows fetched included: what the API would wait."""
    server_mean_ms: float | None
    """pg_stat_statements' mean over the same runs: the executor's time alone.
    None without the extension."""


async def _statement_totals(connection: AsyncConnection, query: Query) -> tuple[int, float] | None:
    """``(calls, total_exec_time)`` of the query's pg_stat_statements row, summed
    over its plans, or None when the extension cannot be read."""
    try:
        async with connection.begin_nested():
            row = (
                await connection.execute(
                    text(
                        "SELECT coalesce(sum(calls), 0), coalesce(sum(total_exec_time), 0) "
                        "FROM pg_stat_statements WHERE query LIKE :tag "
                        "AND query NOT ILIKE '%EXPLAIN%'"
                    ),
                    {"tag": f"{query.tag}%"},
                )
            ).one()
    except DBAPIError:
        return None
    return int(row[0]), float(row[1])


async def time_query(
    connection: AsyncConnection, query: Query, params: Mapping[str, Any], runs: int
) -> Timing:
    """Run it ``runs`` times as the API would, and read back how long that took."""
    before = await _statement_totals(connection, query)
    client_ms: list[float] = []
    rows = 0
    for _ in range(runs):
        started = time.perf_counter()
        result = await connection.execute(text(query.statement), params)
        rows = len(result.all())
        client_ms.append((time.perf_counter() - started) * 1000)
    after = await _statement_totals(connection, query)

    server_mean_ms = None
    if before is not None and after is not None and after[0] > before[0]:
        server_mean_ms = (after[1] - before[1]) / (after[0] - before[0])
    return Timing(rows=rows, client_ms=client_ms, server_mean_ms=server_mean_ms)


async def explain(connection: AsyncConnection, query: Query, params: Mapping[str, Any]) -> str:
    """The plan, as ``EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)`` prints it."""
    result = await connection.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)\n{query.statement}"), params
    )
    return "\n".join(row[0] for row in result)


def _summary(timing: Timing) -> str:
    client = sorted(timing.client_ms)
    p95 = client[min(len(client) - 1, round(0.95 * (len(client) - 1)))]
    server = (
        f"{timing.server_mean_ms:.2f} ms" if timing.server_mean_ms is not None else "unavailable"
    )
    return (
        f"{timing.rows} rows · client median {statistics.median(client):.2f} ms, "
        f"p95 {p95:.2f} ms, max {client[-1]:.2f} ms over {len(client)} runs · "
        f"server mean {server}"
    )


async def run(args: argparse.Namespace) -> int:
    queries = [q for q in QUERIES if not args.only or q.name in args.only]
    unknown = set(args.only or ()) - {q.name for q in QUERIES}
    if unknown:
        print(f"unknown query: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            params = await resolve_parameters(connection, args)
            print(
                f"period {params.period} · filer {params.filer_slug} ({params.filer_id}) · "
                f"security {params.cusip} ({params.security_id}) · "
                f"filing {params.accession_no} ({params.filing_id}) · q {params.q!r}"
            )
            bound = params.bind()
            slow = []
            for query in queries:
                timing = await time_query(connection, query, bound, args.runs)
                plan = await explain(connection, query, bound)
                # Read-only, but a transaction is open since the first statement.
                # Ending it here keeps one query's snapshot out of the next.
                await connection.rollback()
                print(f"\n== {query.name} · {query.endpoint}\n{_summary(timing)}\n")
                if args.sql:
                    print(query.sql.strip() + "\n")
                print(plan)
                if statistics.median(timing.client_ms) >= args.budget_ms:
                    slow.append(query.name)
    finally:
        await engine.dispose()

    if slow:
        print(f"\nover {args.budget_ms:g} ms: {', '.join(slow)}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--only", action="append", metavar="NAME", help="Only this query.")
    parser.add_argument("--runs", type=int, default=20, help="Timed runs per query.")
    parser.add_argument("--period", type=date.fromisoformat, help="Default: the largest.")
    parser.add_argument("--filer", metavar="SLUG", help="Default: the largest in the period.")
    parser.add_argument("--cusip", help="Default: the most held in the period.")
    parser.add_argument("--accession", help="Default: the filer's largest filing.")
    parser.add_argument("--q", default="apple", help="The /stocks search term.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--sql", action="store_true", help="Print each query's SQL too.")
    parser.add_argument(
        "--budget-ms",
        type=float,
        default=100.0,
        help="Exit 1 if any query's median is over this. The ticket's budget.",
    )
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
