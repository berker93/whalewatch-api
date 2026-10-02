"""Building the ``meta`` block every collection response carries.

For a period, all three facts — how many filers have published it, how many
are tracked, and the newest filing among the published — come back in one
round trip, as scalar subqueries of one ``SELECT``.

Published means in ``mv_filer_summary``, one row per ``(filer, period)`` in
``position_snapshot``. The view rather than ``filer_period``, which groups the
period's whole snapshot to find the same filers: 10 ms on the dev database
against 0.1 ms, on every page of every walk. The cost is that the count lags a
publish by as long as the views do, which is the same lag every aggregate
endpoint already has, and ``ingest-filing``, ``backfill`` and ``recompute``
refresh the views as they finish.

The whole query is about 4 ms on the dev database's full backfill, nearly all
of it ``effective_filing`` resolving the period's amendments for
``latest_filing_at``. It changes only when a period is published, so it is the
first thing to cache if it ever shows up in a profile.
"""

from datetime import UTC, date, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.envelope import Coverage, Meta
from app.core.periods import quarter_label
from app.db.models import Filer, Filing
from app.db.queries.effective import EFFECTIVE_FILING
from app.db.queries.periods import FILER_PERIOD
from app.derived.views import FILER_SUMMARY


def unscoped_meta() -> Meta:
    """``meta`` for a collection that is not about a period, like a list of filers."""
    return Meta(generated_at=datetime.now(UTC))


async def period_meta(session: AsyncSession, period: date, *, filer_id: int | None = None) -> Meta:
    """``meta`` for a collection of ``period``'s data.

    With ``filer_id``, for one filer's data: ``latest_filing_at`` is then the
    newest of *its* published filings for the period, which is what dates the
    rows. Published is read from ``position_snapshot`` (through
    ``filer_period``), not the view, because a filer's rows are read live
    from the snapshot too, and a stale view would date them with nothing.
    Coverage is the universe's either way. It says how far the period has
    filled in, which a reader comparing this filer with others needs.

    :raises ValueError: ``period`` is not a quarter end. A router parses the
        ``?period=`` it was given with :func:`~app.core.periods.parse_period`
        before it gets here.
    """
    label = quarter_label(period)

    publications = FILER_SUMMARY if filer_id is None else FILER_PERIOD
    published = and_(
        publications.c.filer_id == EFFECTIVE_FILING.c.filer_id,
        publications.c.period_of_report == EFFECTIVE_FILING.c.period_of_report,
    )
    # Through effective_filing, so a superseded original or a withheld suspect
    # filing does not make the period look newer than the data it is served
    # with.
    latest_filing = (
        select(func.max(Filing.filed_at))
        .join(EFFECTIVE_FILING, EFFECTIVE_FILING.c.filing_id == Filing.id)
        .join(publications, published)
        .where(EFFECTIVE_FILING.c.period_of_report == period)
    )
    if filer_id is not None:
        latest_filing = latest_filing.where(EFFECTIVE_FILING.c.filer_id == filer_id)

    row = (
        await session.execute(
            select(
                select(func.count())
                .select_from(FILER_SUMMARY)
                .where(FILER_SUMMARY.c.period_of_report == period)
                .scalar_subquery()
                .label("filers_reported"),
                # Every filer is tracked: discovery polls every row of the
                # table (tracked_filer_ids), and seed-investors deletes none.
                # Not narrowed by first_period, which ingestion does not fill.
                select(func.count()).select_from(Filer).scalar_subquery().label("filers_tracked"),
                latest_filing.scalar_subquery().label("latest_filing_at"),
            )
        )
    ).one()

    return Meta(
        period=label,
        period_end=period,
        latest_filing_at=row.latest_filing_at,
        coverage=Coverage(filers_reported=row.filers_reported, filers_tracked=row.filers_tracked),
        generated_at=datetime.now(UTC),
    )
