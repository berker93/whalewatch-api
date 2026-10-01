"""materialised views

Three aggregates over the derived tables, which the market-wide and per-filer
read paths serve instead of computing them per request:

- ``mv_consensus_holdings``: per ``(period, security)``, who holds it and how
  much, from ``position_snapshot``.
- ``mv_quarter_flows``: per ``(period, security)``, what was bought and sold,
  from ``position_change``.
- ``mv_filer_summary``: per ``(filer, period)``, the portfolio's size,
  concentration and turnover, from both.

Each has a unique index, which ``REFRESH MATERIALIZED VIEW CONCURRENTLY``
requires. ``whalewatch refresh-views`` refreshes them. See
:mod:`app.derived.views`, whose live queries are what these must agree with.

Created ``WITH DATA``, unlike 0009's table, which was created empty. Filling
these publishes nothing new: they aggregate what is already published. An
unpopulated materialised view also refuses every read and every concurrent
refresh, and there is no reason to start there.

The SQL is written out here rather than built from the live queries, for 0006's
reason: it is history. When a live query changes, a migration redefines its
view, and the ``check_*`` tests fail until one does.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01 23:45:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The dollars a position_change row traded: the shares bought or sold, at the
# period-end price. That is this period's price, or, for an exit, which has
# none, the price the position was last held at. So a new position trades its
# whole value, and an exit trades the whole value it had. Multiplied before
# dividing, so neither of those has a rounding error.
#
# Not value_delta. That includes the price move on every share held throughout,
# so a stock that doubled reads as bought by every holder who did nothing, and a
# trim during a rally reads as negative selling.
TRADED_USD = """
    CASE
        WHEN shares > 0 THEN abs(shares_delta) * value_usd / shares
        WHEN prev_shares > 0 THEN abs(shares_delta) * prev_value_usd / prev_shares
        ELSE 0
    END
"""

CONSENSUS_HOLDINGS = """
CREATE MATERIALIZED VIEW mv_consensus_holdings AS
SELECT
    period_of_report,
    security_id,
    count(*) AS holder_count,
    sum(value_usd) AS total_value_usd,
    sum(shares) AS total_shares,
    -- Over the holders, not over every filer: the weight of the stock in the
    -- portfolios that hold it.
    round(avg(weight_pct), 6) AS avg_weight_pct,
    -- percentile_cont is double precision only. A weight has at most nine
    -- significant digits and the midpoint of two has ten, so converting back
    -- at fifteen gives the exact midpoint, which rounds as numeric does.
    round(
        (percentile_cont(0.5) WITHIN GROUP (ORDER BY weight_pct))::numeric, 6
    ) AS median_weight_pct,
    -- 1 is the period's largest by dollars held. Ties share a rank.
    rank() OVER (PARTITION BY period_of_report ORDER BY sum(value_usd) DESC) AS value_rank,
    bool_or(suspect) AS suspect
FROM position_snapshot
GROUP BY period_of_report, security_id
WITH DATA
"""

QUARTER_FLOWS = f"""
CREATE MATERIALIZED VIEW mv_quarter_flows AS
WITH traded AS (
    SELECT
        period_of_report,
        security_id,
        round(coalesce(sum({TRADED_USD}) FILTER (WHERE action IN ('new', 'add')), 0), 2)
            AS bought_value_usd,
        round(coalesce(sum({TRADED_USD}) FILTER (WHERE action IN ('trim', 'exit')), 0), 2)
            AS sold_value_usd,
        -- Every change, holds included, so it is how far the shares these
        -- filers hold moved, drift and all.
        sum(shares_delta) AS net_shares,
        count(*) FILTER (WHERE action = 'new') AS new_positions,
        count(*) FILTER (WHERE action = 'exit') AS exits,
        -- One row per filer per security per period, so a count of rows is a
        -- count of filers.
        count(*) FILTER (WHERE action IN ('new', 'add')) AS buyer_count,
        count(*) FILTER (WHERE action IN ('trim', 'exit')) AS seller_count,
        bool_or(suspect) AS suspect
    FROM position_change
    -- A filer's first period is the first we have, not a quarter in which it
    -- bought everything. Counted, every filer added to the universe would be a
    -- market-wide buying spree.
    WHERE prev_period_of_report IS NOT NULL
    GROUP BY period_of_report, security_id
)
SELECT
    period_of_report,
    security_id,
    bought_value_usd,
    sold_value_usd,
    bought_value_usd - sold_value_usd AS net_value_usd,
    net_shares,
    new_positions,
    exits,
    buyer_count,
    seller_count,
    suspect
FROM traded
WITH DATA
"""

FILER_SUMMARY = f"""
CREATE MATERIALIZED VIEW mv_filer_summary AS
WITH ranked AS (
    SELECT
        filer_id,
        period_of_report,
        value_usd,
        suspect,
        -- A tie at tenth place is between equal values, so which of them is
        -- in the ten does not change the sum.
        row_number() OVER (
            PARTITION BY filer_id, period_of_report ORDER BY value_usd DESC, security_id
        ) AS place
    FROM position_snapshot
),
held AS (
    SELECT
        filer_id,
        period_of_report,
        sum(value_usd) AS portfolio_value_usd,
        count(*) AS position_count,
        sum(value_usd) FILTER (WHERE place <= 10) AS top10_value_usd,
        bool_or(suspect) AS suspect
    FROM ranked
    GROUP BY filer_id, period_of_report
),
traded AS (
    SELECT
        filer_id,
        period_of_report,
        -- A hold traded nothing: its few shares of drift are not a trade.
        coalesce(sum({TRADED_USD}) FILTER (WHERE action <> 'hold'), 0) AS traded_usd,
        -- Every position of the previous period is a row here, held on or
        -- exited, so this is the previous period's value. Null in the filer's
        -- first period, which has no previous one.
        sum(prev_value_usd) AS prev_portfolio_value_usd,
        bool_or(suspect) AS suspect
    FROM position_change
    GROUP BY filer_id, period_of_report
)
SELECT
    held.filer_id,
    held.period_of_report,
    held.portfolio_value_usd,
    held.position_count,
    round(held.top10_value_usd * 100 / nullif(held.portfolio_value_usd, 0), 6)
        AS top10_weight_pct,
    -- Turnover, in percent: sum(traded) / 2 / previous portfolio value. Half of
    -- bought plus sold, so a manager who sold half the book and bought the same
    -- again turned over half of it, not all of it. Trades valued as TRADED_USD
    -- says, so a quarter in which prices moved and nobody traded turned over
    -- nothing. Across a gap it covers every quarter since the previous period.
    round(traded.traded_usd * 100 / 2 / nullif(traded.prev_portfolio_value_usd, 0), 6)
        AS turnover_pct,
    -- Turnover is only as good as the previous period, too.
    held.suspect OR coalesce(traded.suspect, false) AS suspect
FROM held
LEFT JOIN traded USING (filer_id, period_of_report)
WITH DATA
"""

#: Each view, and the columns of its unique index: its key, which is also what
#: every read of it filters on first.
UNIQUE_INDEXES = (
    ("mv_consensus_holdings", ("period_of_report", "security_id")),
    ("mv_quarter_flows", ("period_of_report", "security_id")),
    ("mv_filer_summary", ("filer_id", "period_of_report")),
)


#: The definitions someone will ask about, on the columns themselves: Postgres
#: drops the comments inside a view's SQL, and these are what ``\d+`` shows.
COLUMN_COMMENTS = (
    (
        "mv_consensus_holdings",
        "median_weight_pct",
        "PERCENTILE_CONT(0.5) of weight_pct over the stock's holders, converted back to "
        "numeric exactly and rounded to 6 places, as avg_weight_pct is.",
    ),
    (
        "mv_quarter_flows",
        "bought_value_usd",
        "new and add: shares bought x the period-end price. Not value_delta, which counts "
        "a price move on shares held as buying. A filer's first period is not counted.",
    ),
    (
        "mv_quarter_flows",
        "sold_value_usd",
        "trim and exit, as a positive number: shares sold x the period-end price, or for "
        "an exit the price it was last held at, which is the whole value it had.",
    ),
    (
        "mv_filer_summary",
        "turnover_pct",
        "Percent: sum(traded) / 2 / previous portfolio value. Traded is shares bought or "
        "sold x the period-end price, an exit at its last price, a hold nothing. Null in "
        "the filer's first period. Across a gap, covers every quarter since the previous "
        "period.",
    ),
)


def upgrade() -> None:
    for statement in (CONSENSUS_HOLDINGS, QUARTER_FLOWS, FILER_SUMMARY):
        op.execute(statement)
    for view, columns in UNIQUE_INDEXES:
        # Plain columns and no WHERE: a concurrent refresh can use no other
        # kind. Named by the "uq" convention, by hand, since Alembic has no
        # model of a materialised view to apply it to.
        op.execute(f"CREATE UNIQUE INDEX uq_{view}_{columns[0]} ON {view} ({', '.join(columns)})")
    for view, column, comment in COLUMN_COMMENTS:
        quoted = comment.replace("'", "''")
        op.execute(f"COMMENT ON COLUMN {view}.{column} IS '{quoted}'")


def downgrade() -> None:
    """Their indexes go with them. Nothing is lost: each is an aggregate of the
    derived tables, and the upgrade fills it again."""
    for view, _ in reversed(UNIQUE_INDEXES):
        op.execute(f"DROP MATERIALIZED VIEW {view}")
