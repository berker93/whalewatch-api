"""Each filer's largest position in a period: a per-group top-1, written two ways.

``GET /v1/investors`` shows every filer's top holding, for whichever period is
its latest, in the same query as the rest of the row. That is "the first row of
each group", and Postgres has a clause for exactly that::

    SELECT DISTINCT ON (p.filer_id) p.filer_id, p.security_id, p.weight_pct
    FROM position_snapshot p
    JOIN periods USING (filer_id, period_of_report)
    ORDER BY p.filer_id, p.value_usd DESC, p.security_id

``DISTINCT ON`` keeps the first row of each run of equal ``filer_id`` in the
``ORDER BY``. It is Postgres-only. The portable spelling numbers the rows of
each group and keeps the first::

    SELECT filer_id, security_id, weight_pct FROM (
        SELECT p.filer_id, p.security_id, p.weight_pct,
               row_number() OVER (PARTITION BY p.filer_id
                                  ORDER BY p.value_usd DESC, p.security_id) AS place
        FROM position_snapshot p
        JOIN periods USING (filer_id, period_of_report)
    ) ranked
    WHERE place = 1

The endpoint uses :func:`top_holdings`, the first. :func:`top_holdings_ranked`
is the second, kept so the two stay comparable. The tests hold them to the
same rows, and ``make explain`` times both.

The trade
---------
Not speed. On the dev database's full backfill, the latest periods of a page
of 50 filers are 46,000 positions, and the two plans are the same plan: read
them through the primary key, sort all 46,000 by ``(filer_id, value_usd DESC,
security_id)``, keep the first of each filer. About 20 ms against 18 ms. The
window's "Run Condition" stops numbering a partition once it passes 1, which
saves only the numbering, since the sort has already happened.

So the difference is in the reading. ``DISTINCT ON`` says what it means in one
clause, and its ordering is the ``ORDER BY`` everyone already reads. The window
needs a subquery and a filter, and says "first" as ``place = 1``. In return it
runs on every database, and it generalises: ``place <= 3`` is a top-3, and
``rank()`` in place of ``row_number()`` keeps ties, where ``DISTINCT ON`` can
only ever return one row per group.

What both cost is the sort. A ``LATERAL`` subquery with ``ORDER BY ... LIMIT
1`` per filer keeps only one row while it reads each group (a top-N heapsort),
and does the same 50 filers in about 4 ms. Every one of these reads all of a
filer's positions, since nothing indexes ``position_snapshot`` by value within
a period. That is the next thing to change if this query ever shows up in a
profile. ``mv_filer_summary`` already ranks every period's positions to sum
its top ten, and could keep the first of them as a column.

The order
---------
By ``value_usd`` rather than ``weight_pct``: the same order within a period,
and never null. Weights are null when a period's positions are all worth
nothing, and ``DESC`` puts nulls first. ``security_id`` breaks a tie, so
two positions of equal value give the same top holding on every request.
"""

from typing import Any

from sqlalchemy import FromClause, Select, and_, func, select

from app.db.models.position_snapshot import PositionSnapshot


def _join(periods: FromClause) -> Any:
    snapshot = PositionSnapshot
    return and_(
        snapshot.filer_id == periods.c.filer_id,
        snapshot.period_of_report == periods.c.period_of_report,
    )


def top_holdings(periods: FromClause) -> Select[Any]:
    """``(filer_id, security_id, weight_pct)``: the largest position of each
    ``(filer_id, period_of_report)`` in ``periods``, by ``DISTINCT ON``.

    ``periods`` must hold at most one period per filer, since the result is
    one row per filer.
    """
    snapshot = PositionSnapshot
    return (
        select(snapshot.filer_id, snapshot.security_id, snapshot.weight_pct)
        .join(periods, _join(periods))
        .distinct(snapshot.filer_id)
        .order_by(snapshot.filer_id, snapshot.value_usd.desc(), snapshot.security_id)
    )


def top_holdings_ranked(periods: FromClause) -> Select[Any]:
    """:func:`top_holdings`, by ``row_number()``: the same rows, on any database."""
    snapshot = PositionSnapshot
    ranked = (
        select(
            snapshot.filer_id,
            snapshot.security_id,
            snapshot.weight_pct,
            func.row_number()
            .over(
                partition_by=snapshot.filer_id,
                order_by=(snapshot.value_usd.desc(), snapshot.security_id),
            )
            .label("place"),
        )
        .join(periods, _join(periods))
        .subquery("ranked")
    )
    return select(ranked.c.filer_id, ranked.c.security_id, ranked.c.weight_pct).where(
        ranked.c.place == 1
    )
