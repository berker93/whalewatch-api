"""Cluster buy detection: several of one company's insiders buying within a window.

What counts
-----------
A Form 4 line counts towards a cluster when it is a purchase someone chose to
make, with their own money, now:

- code ``P``, an open-market or private purchase, of shares: not a line of the
  derivative table;
- by an officer or a director of the issuer, as the filing's owner row says.
  A 10% owner or an "other" is left out: often a fund, and a fund family's
  entities filing separately would be three "insiders" that are one buyer;
- not under a Rule 10b5-1 plan (:attr:`~InsiderTransaction.is_10b5_1`), which
  was set up months before;
- not by an employee stock purchase plan or a dividend reinvestment plan, as
  the line's footnotes say (:data:`PLAN_PURCHASE_PATTERN`). Both buy on a
  schedule for everyone enrolled. TSMC's ESPP files one Form 4 for each of
  some thirty officers on the same day, at the same price, as code ``P``,
  and that is a payroll date, not thirty opinions.

A line is attributed to one insider, the first owner a filing names (see
:class:`~app.db.models.insider.ReportingOwner`), so a joint filing is one
insider, however many entities sign it.

The window
----------
For each issuer, each day with a counted purchase *anchors* a window: that day
and the :data:`window` days before it, so 30 is a purchase and every other up
to 30 days earlier. A window *qualifies* when it holds at least
``min_insiders`` distinct insiders. Rolling, not a lookback from today: every
day of history anchors one, so a cluster from 2021 is found as surely as one
from last week, and the answer does not change with the day it is asked.

Postgres has no ``COUNT(DISTINCT ...)`` as a window function, so the window
collects the insiders instead, ``array_agg(insider_id) OVER (... RANGE BETWEEN
'30 days' PRECEDING AND CURRENT ROW)``, and a subquery counts the distinct
ones in each array. The rows are first reduced to one per issuer, insider and
day, so an insider who bought in three tranches on one day is one element,
and an insider who bought on three days is three elements of one insider.

Episodes
--------
A burst of buying qualifies on many anchor days in a row, and the windows
overlap. Overlapping windows are one episode, a gaps-and-islands problem:
order an issuer's qualifying windows by anchor day, start a new island
wherever a window starts after the previous one ended, and number the islands
with a running sum of the starts. A window's start is its first purchase,
not the calendar day it opens on, so "starts after the previous one ended"
is "shares no purchase with it": the previous window ends on a purchase. The
episode runs from its first window's start to its last window's anchor, and
every counted line in that span is in it, since the windows chain across it.

Rebuilt whole
-------------
Every point of :data:`WINDOWS` by :data:`MIN_INSIDERS`, in one transaction
under :data:`CLUSTER_LOCK`. Counted lines are a small fraction of Form 4
lines, so a full rebuild is a fraction of a second and is never out of step
with a 4/A that deleted the lines it amended.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InsiderCluster

#: The windows, in days, clusters are detected over and the API offers.
WINDOWS: Final = (14, 30, 60, 90)
#: The thresholds, distinct insiders, clusters are detected at.
MIN_INSIDERS: Final = (2, 3, 4, 5)

DEFAULT_WINDOW: Final = 30
DEFAULT_MIN_INSIDERS: Final = 3

#: A footnote that says the purchase was a plan's: an employee stock purchase
#: plan, or a dividend reinvestment plan. Case-insensitive, Postgres regex,
#: matched against the line's footnotes as JSON text. Can leave out a real
#: purchase whose footnote only mentions plan shares among those held after.
PLAN_PURCHASE_PATTERN: Final = (
    r"employee stock purchase plan|\mESPP\M|dividend reinvestment|\mDRIP\M"
)

#: The advisory lock a rebuild holds until it commits, so two do not
#: interleave their deletes and inserts. The bytes of ``wwclustr``.
CLUSTER_LOCK: Final = int.from_bytes(b"wwclustr", "big")

#: What a cluster counts, as the API states it in ``meta.rules``: the module
#: docstring's rules, one sentence each.
RULES: Final = (
    "Open-market purchases (code P) of shares, not of derivatives.",
    "By officers and directors. 10% owners and others are not counted.",
    "Not made under a Rule 10b5-1 plan.",
    "Not bought by an employee stock purchase plan or a dividend reinvestment plan, "
    "as the footnotes say.",
    "Each insider counts once, however many times they bought.",
)

# The lines that count, and why the others do not. One row per P line, the
# owner's row joined for the role, the filing's for the spelling of the
# issuer's name. ``excluded`` is null for a line that counts.
_CLASSIFIED: Final = """
    SELECT
        t.id,
        t.transaction_date,
        t.issuer_cik,
        t.issuer_name,
        t.insider_id,
        t.security_id,
        t.shares,
        t.value_usd,
        f.filed_at,
        CASE
            WHEN t.is_derivative THEN 'derivative'
            WHEN NOT (o.is_director OR o.is_officer) THEN 'role'
            WHEN t.is_10b5_1 THEN '10b5_1'
            WHEN coalesce(t.footnotes::text, '') ~* :plan_pattern THEN 'plan'
        END AS excluded
    FROM insider_transaction AS t
    JOIN reporting_owner AS o
        ON o.filing_id = t.filing_id AND o.insider_id = t.insider_id
    JOIN filing AS f ON f.id = t.filing_id
    WHERE t.transaction_code = 'P'
"""

# One parameter set's episodes, as insider_cluster rows. :window and :min.
_EPISODES: Final = f"""
WITH classified AS ({_CLASSIFIED}),
line AS (SELECT * FROM classified WHERE excluded IS NULL),
-- One row per issuer, insider and day: tranches are one purchase.
buy AS (
    SELECT DISTINCT issuer_cik, insider_id, transaction_date FROM line
),
-- Every buy anchors the window ending on its day. Rows on one day are peers
-- in RANGE mode, so each sees the whole day, and all of a day's rows agree.
anchored AS (
    SELECT
        issuer_cik,
        transaction_date AS window_end,
        min(transaction_date) OVER w AS window_start,
        array_agg(insider_id) OVER w AS insiders
    FROM buy
    WINDOW w AS (
        PARTITION BY issuer_cik
        ORDER BY transaction_date
        RANGE BETWEEN make_interval(days => :window) PRECEDING AND CURRENT ROW
    )
),
qualifying AS (
    SELECT DISTINCT issuer_cik, window_start, window_end
    FROM anchored
    WHERE (SELECT count(DISTINCT insider) FROM unnest(insiders) AS insider) >= :min
),
-- Gaps and islands: a window that starts after the previous one's anchor
-- shares no purchase with it, and begins a new episode.
island AS (
    SELECT
        issuer_cik,
        window_start,
        window_end,
        sum(starts) OVER (PARTITION BY issuer_cik ORDER BY window_end) AS episode
    FROM (
        SELECT
            *,
            CASE
                WHEN window_start
                    <= lag(window_end) OVER (PARTITION BY issuer_cik ORDER BY window_end)
                THEN 0
                ELSE 1
            END AS starts
        FROM qualifying
    ) AS flagged
),
episode AS (
    SELECT issuer_cik, min(window_start) AS window_start, max(window_end) AS window_end
    FROM island
    GROUP BY issuer_cik, episode
)
SELECT
    CAST(:window AS smallint) AS window_days,
    CAST(:min AS smallint) AS min_insiders,
    e.issuer_cik,
    e.window_start,
    e.window_end,
    (array_agg(l.issuer_name ORDER BY l.filed_at DESC, l.id DESC))[1] AS issuer_name,
    CASE
        WHEN bool_and(l.security_id IS NOT NULL) AND count(DISTINCT l.security_id) = 1
        THEN min(l.security_id)
    END AS security_id,
    count(DISTINCT l.insider_id) AS insider_count,
    count(*) AS transaction_count,
    sum(l.shares) AS total_shares,
    sum(l.value_usd) AS total_value,
    array_agg(l.id ORDER BY l.transaction_date, l.id) AS transaction_ids
FROM episode AS e
JOIN line AS l
    ON l.issuer_cik = e.issuer_cik
    AND l.transaction_date BETWEEN e.window_start AND e.window_end
GROUP BY e.issuer_cik, e.window_start, e.window_end
"""

_COLUMNS: Final = (
    "window_days, min_insiders, issuer_cik, window_start, window_end, issuer_name, "
    "security_id, insider_count, transaction_count, total_shares, total_value, transaction_ids"
)

ClusterKey = tuple[str, date]
"""An episode within one parameter set: ``(issuer_cik, window_start)``."""


@dataclass(frozen=True, slots=True)
class Episode:
    """One episode of the default parameter set, for the report."""

    issuer_cik: str
    issuer_name: str
    window_start: date
    window_end: date
    insider_count: int


@dataclass(frozen=True, slots=True)
class ClusterRebuild:
    """What :func:`detect_clusters` wrote, and how it differs from before."""

    written: dict[tuple[int, int], int]
    """Episodes per ``(window_days, min_insiders)``, every point of the grid."""
    lines: Counter[str] = field(default_factory=Counter)
    """Code ``P`` lines by what became of them: ``counted``, or the reason
    they were not (``derivative``, ``role``, ``10b5_1``, ``plan``)."""
    added: tuple[Episode, ...] = ()
    """The default set's episodes that are new, or have grown or changed."""
    removed: tuple[ClusterKey, ...] = ()
    """The default set's episodes that are gone."""

    @property
    def episodes(self) -> int:
        """Episodes in the default parameter set."""
        return self.written[DEFAULT_WINDOW, DEFAULT_MIN_INSIDERS]


async def detect_clusters(session: AsyncSession) -> ClusterRebuild:
    """Rebuild ``insider_cluster`` from ``insider_transaction``, every parameter set.

    In the caller's transaction, under :data:`CLUSTER_LOCK`. Readers see the
    old episodes until it commits.
    """
    await session.execute(select(func.pg_advisory_xact_lock(CLUSTER_LOCK)))
    before = await _default_episodes(session)

    lines: Counter[str] = Counter(
        {
            reason or "counted": count
            for reason, count in await session.execute(
                text(f"SELECT excluded, count(*) FROM ({_CLASSIFIED}) AS c GROUP BY excluded"),
                {"plan_pattern": PLAN_PURCHASE_PATTERN},
            )
        }
    )

    await session.execute(text("DELETE FROM insider_cluster"))
    written: dict[tuple[int, int], int] = {}
    for window in WINDOWS:
        for minimum in MIN_INSIDERS:
            result = await session.execute(
                text(f"INSERT INTO insider_cluster ({_COLUMNS}) {_EPISODES}"),
                {"window": window, "min": minimum, "plan_pattern": PLAN_PURCHASE_PATTERN},
            )
            written[window, minimum] = result.rowcount  # type: ignore[attr-defined]

    after = await _default_episodes(session)
    return ClusterRebuild(
        written=written,
        lines=lines,
        added=tuple(episode for key, episode in after.items() if before.get(key) != episode),
        removed=tuple(key for key in before if key not in after),
    )


async def _default_episodes(session: AsyncSession) -> dict[ClusterKey, Episode]:
    rows = await session.execute(
        select(
            InsiderCluster.issuer_cik,
            InsiderCluster.issuer_name,
            InsiderCluster.window_start,
            InsiderCluster.window_end,
            InsiderCluster.insider_count,
        )
        .where(
            InsiderCluster.window_days == DEFAULT_WINDOW,
            InsiderCluster.min_insiders == DEFAULT_MIN_INSIDERS,
        )
        .order_by(InsiderCluster.window_end, InsiderCluster.issuer_cik)
    )
    return {
        (row.issuer_cik, row.window_start): Episode(
            issuer_cik=row.issuer_cik,
            issuer_name=row.issuer_name,
            window_start=row.window_start,
            window_end=row.window_end,
            insider_count=row.insider_count,
        )
        for row in rows
    }
