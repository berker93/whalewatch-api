"""market views

Two more aggregates, for the market endpoints, which read nothing else:

- ``mv_year_flows``: per ``(period, security)``, ``mv_quarter_flows`` over the
  four quarters ending at the period, with filers counted once each however
  many of the four they traded in.
- ``mv_filing_feed``: per effective filing whose period is published, the
  period's position count and its largest trade, for the recent-filings feed.

A year is not one figure. A position bought in Q1 and sold in Q3 nets to about
nothing, which is right for net flow and wrong for "the year's top buys", so
the year keeps bought and sold apart, as the quarter does, and the API serves
both. Its dollars are the sums of the quarters' rows, each already rounded to
cents, so a year adds up to its four quarters exactly. Its counts cannot be
sums: a filer that bought in two of the quarters is one buyer of the year, not
two. So they are read from ``position_change`` again, ``DISTINCT`` per filer.

Neither reads another materialised view. The year recomputes the quarters'
rows rather than reading ``mv_quarter_flows``, so a refresh of one cannot leave
the other reading stale rows, and the refresh order stays the catalog's.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-03 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0014's, unchanged: the dollars a position_change row traded.
TRADED_USD = """
    CASE
        WHEN shares > 0 THEN abs(shares_delta) * value_usd / shares
        WHEN prev_shares > 0 THEN abs(shares_delta) * prev_value_usd / prev_shares
        ELSE 0
    END
"""

# The four quarter ends up to and including a year's: after the same day a year
# earlier, and not after it. A quarter end less a year is the quarter end four
# quarters before, March 31st and December 31st included.
IN_YEAR = """
    {period} <= ends.period_of_report
    AND {period} > ends.period_of_report - interval '1 year'
"""

YEAR_FLOWS = f"""
CREATE MATERIALIZED VIEW mv_year_flows AS
WITH quarters AS (
    -- mv_quarter_flows' dollars, worked out again rather than read from it.
    SELECT
        period_of_report,
        security_id,
        round(coalesce(sum({TRADED_USD}) FILTER (WHERE action IN ('new', 'add')), 0), 2)
            AS bought_value_usd,
        round(coalesce(sum({TRADED_USD}) FILTER (WHERE action IN ('trim', 'exit')), 0), 2)
            AS sold_value_usd,
        sum(shares_delta) AS net_shares,
        bool_or(suspect) AS suspect
    FROM position_change
    WHERE prev_period_of_report IS NOT NULL
    GROUP BY period_of_report, security_id
),
-- Every period with flows ends a year. None ends after the latest: a year
-- ending in a quarter not yet filed would be the same rows as the last one.
ends AS (
    SELECT DISTINCT period_of_report FROM quarters
),
dollars AS (
    SELECT
        ends.period_of_report,
        quarters.security_id,
        sum(quarters.bought_value_usd) AS bought_value_usd,
        sum(quarters.sold_value_usd) AS sold_value_usd,
        sum(quarters.net_shares) AS net_shares,
        bool_or(quarters.suspect) AS suspect
    FROM ends
    JOIN quarters ON {IN_YEAR.format(period="quarters.period_of_report")}
    GROUP BY ends.period_of_report, quarters.security_id
),
filers AS (
    -- Distinct, so a filer that bought in two quarters is one buyer. One that
    -- bought and later sold is a buyer and a seller, as it is in a quarter
    -- only across two filers.
    SELECT
        ends.period_of_report,
        change.security_id,
        count(DISTINCT change.filer_id) FILTER (WHERE change.action = 'new') AS new_positions,
        count(DISTINCT change.filer_id) FILTER (WHERE change.action = 'exit') AS exits,
        count(DISTINCT change.filer_id) FILTER (WHERE change.action IN ('new', 'add'))
            AS buyer_count,
        count(DISTINCT change.filer_id) FILTER (WHERE change.action IN ('trim', 'exit'))
            AS seller_count
    FROM ends
    JOIN position_change change ON {IN_YEAR.format(period="change.period_of_report")}
    WHERE change.prev_period_of_report IS NOT NULL
      AND change.action <> 'hold'
    GROUP BY ends.period_of_report, change.security_id
)
SELECT
    dollars.period_of_report,
    dollars.security_id,
    dollars.bought_value_usd,
    dollars.sold_value_usd,
    dollars.bought_value_usd - dollars.sold_value_usd AS net_value_usd,
    dollars.net_shares,
    -- A security only held on through the year has no row in filers.
    coalesce(filers.new_positions, 0) AS new_positions,
    coalesce(filers.exits, 0) AS exits,
    coalesce(filers.buyer_count, 0) AS buyer_count,
    coalesce(filers.seller_count, 0) AS seller_count,
    dollars.suspect
FROM dollars
LEFT JOIN filers USING (period_of_report, security_id)
WITH DATA
"""

FILING_FEED = f"""
CREATE MATERIALIZED VIEW mv_filing_feed AS
WITH largest AS (
    -- The period's largest trade, valued as the flows value it. Not in the
    -- filer's first period, where every position is new only to us.
    SELECT DISTINCT ON (filer_id, period_of_report)
        filer_id,
        period_of_report,
        security_id,
        action,
        shares_delta,
        round({TRADED_USD}, 2) AS traded_value_usd
    FROM position_change
    WHERE action <> 'hold'
      AND prev_period_of_report IS NOT NULL
    ORDER BY filer_id, period_of_report, traded_value_usd DESC, security_id DESC
),
positions AS (
    SELECT filer_id, period_of_report, count(*) AS position_count
    FROM position_snapshot
    GROUP BY filer_id, period_of_report
)
SELECT
    effective.filing_id,
    effective.filer_id,
    effective.period_of_report,
    filing.filed_at,
    positions.position_count,
    largest.security_id AS largest_security_id,
    largest.action AS largest_action,
    largest.shares_delta AS largest_shares_delta,
    largest.traded_value_usd AS largest_traded_value_usd
FROM effective_filing effective
JOIN filing ON filing.id = effective.filing_id
-- Inner: a filing whose period is not published, withheld as suspect, is
-- not news yet.
JOIN positions
  ON positions.filer_id = effective.filer_id
 AND positions.period_of_report = effective.period_of_report
LEFT JOIN largest
  ON largest.filer_id = effective.filer_id
 AND largest.period_of_report = effective.period_of_report
WITH DATA
"""

#: As 0014's: each view's key, which a concurrent refresh matches rows on. A
#: filing is one filer's for one period, so the feed's filing_id alone is
#: unique; the two before it are there for reconcile, which names the filer and
#: period of a row that disagrees with its live query.
UNIQUE_INDEXES = (
    ("mv_year_flows", ("period_of_report", "security_id")),
    ("mv_filing_feed", ("filer_id", "period_of_report", "filing_id")),
)

COLUMN_COMMENTS = (
    (
        "mv_year_flows",
        "period_of_report",
        "The last of the four quarters: the year is the quarters after the same day a year "
        "earlier, up to and including this one.",
    ),
    (
        "mv_year_flows",
        "bought_value_usd",
        "Gross: the sum of the four quarters' bought_value_usd in mv_quarter_flows. A "
        "position bought and sold within the year counts here in full.",
    ),
    (
        "mv_year_flows",
        "net_value_usd",
        "bought_value_usd - sold_value_usd: about zero for a position bought and sold "
        "within the year.",
    ),
    (
        "mv_year_flows",
        "buyer_count",
        "Distinct filers that opened or added to it in any of the four quarters.",
    ),
    (
        "mv_filing_feed",
        "position_count",
        "Positions in the period as published, which this filing is one of the filings of.",
    ),
)


def upgrade() -> None:
    for statement in (YEAR_FLOWS, FILING_FEED):
        op.execute(statement)
    for view, columns in UNIQUE_INDEXES:
        op.execute(f"CREATE UNIQUE INDEX uq_{view}_{columns[0]} ON {view} ({', '.join(columns)})")
    # The feed is read newest first, and paged on (filed_at, filing_id).
    op.execute("CREATE INDEX ix_mv_filing_feed_filed_at ON mv_filing_feed (filed_at, filing_id)")
    for view, column, comment in COLUMN_COMMENTS:
        quoted = comment.replace("'", "''")
        op.execute(f"COMMENT ON COLUMN {view}.{column} IS '{quoted}'")


def downgrade() -> None:
    """Nothing is lost, for 0014's reason."""
    for view, _ in reversed(UNIQUE_INDEXES):
        op.execute(f"DROP MATERIALIZED VIEW {view}")
