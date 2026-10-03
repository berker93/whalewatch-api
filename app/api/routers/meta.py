"""Meta endpoints: what there is to ask for, and how old it is.

``GET /v1/meta/periods``
    Every quarter with published data, oldest first, with its filing deadline
    and coverage. What the frontend's quarter rail is drawn from.
``GET /v1/meta/freshness``
    When each materialised view was last refreshed, and when each job last
    succeeded.

Both change only when a job runs, and both are read on every page load, so
both are kept in Redis for five minutes (:mod:`app.api.cache`), and dropped
from it when a job publishes. An answer can be that much older than the data
it describes; ``meta.generated_at`` says when it was built.

Coverage
--------
A quarter's ``filers_reported`` is ``meta.coverage.filers_reported`` on every
other endpoint for that quarter: read from ``mv_filer_summary``, so as of the
views' last refresh (``meta.refreshed_at``), and a filer withheld as suspect
has not reported. ``filers_tracked`` is every filer tracked today, for old
quarters too. Both are :func:`app.api.meta.period_meta`'s, so that the rail and
the page it leads to never disagree about a quarter.
"""

from typing import Any, Final

from fastapi import APIRouter
from sqlalchemy import Date, Row, Select, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import Lifetime, cached
from app.api.deps import SessionDep
from app.api.meta import refreshed_at, unscoped_meta
from app.api.schemas.envelope import Envelope
from app.api.schemas.meta import Freshness, FreshnessKind, PeriodCoverage
from app.core.periods import filing_deadline, quarter_label
from app.db.models import Filer, Filing, MatviewRefresh
from app.db.queries.effective import EFFECTIVE_FILING
from app.db.queries.runs import job_names, last_successes
from app.derived.views import FILER_SUMMARY, MATERIALISED_VIEWS

router = APIRouter(tags=["meta"])

#: The share of the tracked filers, in percent, that makes a quarter whose
#: deadline has passed complete.
COMPLETE_PCT: Final = 95

#: Where a deadline is reckoned: EDGAR's clock. Asked of Postgres, which has a
#: time zone database, rather than of ``zoneinfo``, which in the container
#: might not.
_EDGAR_TIMEZONE: Final = "America/New_York"


# --- periods --------------------------------------------------------------------


def periods_query() -> Select[Any]:
    """Every quarter in ``mv_filer_summary``, oldest first, with its coverage
    and the span of its published filings' filing times, and today's date on
    EDGAR's clock."""
    summary = FILER_SUMMARY
    reported = (
        select(summary.c.period_of_report, func.count().label("filers_reported"))
        .group_by(summary.c.period_of_report)
        .subquery("reported")
    )
    # Through effective_filing, as period_meta's latest_filing_at is: a
    # superseded original or a withheld filing is not what the quarter's
    # figures were built from.
    filed = (
        select(
            EFFECTIVE_FILING.c.period_of_report,
            func.min(Filing.filed_at).label("first_filed_at"),
            func.max(Filing.filed_at).label("last_filed_at"),
        )
        .join(Filing, Filing.id == EFFECTIVE_FILING.c.filing_id)
        .join(
            summary,
            and_(
                summary.c.filer_id == EFFECTIVE_FILING.c.filer_id,
                summary.c.period_of_report == EFFECTIVE_FILING.c.period_of_report,
            ),
        )
        .group_by(EFFECTIVE_FILING.c.period_of_report)
        .subquery("filed")
    )
    return (
        select(
            reported.c.period_of_report,
            reported.c.filers_reported,
            # Every filer is tracked: see period_meta.
            select(func.count()).select_from(Filer).scalar_subquery().label("filers_tracked"),
            filed.c.first_filed_at,
            filed.c.last_filed_at,
            cast(func.timezone(_EDGAR_TIMEZONE, func.now()), Date).label("today"),
        )
        .outerjoin(filed, filed.c.period_of_report == reported.c.period_of_report)
        .order_by(reported.c.period_of_report)
    )


def _period_coverage(row: Row[Any]) -> PeriodCoverage:
    deadline = filing_deadline(row.period_of_report)
    reported: int = row.filers_reported
    tracked: int = row.filers_tracked
    return PeriodCoverage(
        period=quarter_label(row.period_of_report),
        period_end=row.period_of_report,
        filing_deadline=deadline,
        filers_reported=reported,
        filers_tracked=tracked,
        # In whole numbers, so 95 of 100 is complete and not lost to a float.
        is_complete=row.today > deadline
        and tracked > 0
        and reported * 100 >= COMPLETE_PCT * tracked,
        first_filed_at=row.first_filed_at,
        last_filed_at=row.last_filed_at,
    )


async def build_periods(session: AsyncSession) -> Envelope[PeriodCoverage]:
    rows = await session.execute(periods_query())
    meta = unscoped_meta()
    meta.refreshed_at = await refreshed_at(session, FILER_SUMMARY)
    return Envelope(data=[_period_coverage(row) for row in rows], meta=meta)


@router.get(
    "/meta/periods",
    response_model=Envelope[PeriodCoverage],
    summary="Every quarter with data, and how far each has filled in",
)
@cached(Lifetime.METADATA)
async def read_periods(session: SessionDep) -> Envelope[PeriodCoverage]:
    """Every quarter with a published portfolio, oldest first. A quarter
    nothing has been published for yet, such as one whose filings have not
    started arriving, is not listed. Not paginated: there are four a year.
    Cached for five minutes."""
    return await build_periods(session)


# --- freshness ------------------------------------------------------------------


async def build_freshness(session: AsyncSession) -> Envelope[Freshness]:
    refreshes = {
        refresh.view_name: refresh for refresh in await session.scalars(select(MatviewRefresh))
    }
    views = []
    for view in MATERIALISED_VIEWS:
        refresh = refreshes.get(view.name)
        views.append(
            Freshness(
                kind=FreshnessKind.VIEW,
                name=view.name,
                last_success_at=refresh.refreshed_at if refresh else None,
                run_id=refresh.run_id if refresh else None,
            )
        )

    successes = await last_successes(session)
    jobs = []
    for name in await job_names(session):
        success = successes.get(name)
        jobs.append(
            Freshness(
                kind=FreshnessKind.JOB,
                name=name,
                last_success_at=success.finished_at if success else None,
                run_id=success.run_id if success else None,
            )
        )
    return Envelope(data=views + jobs, meta=unscoped_meta())


@router.get(
    "/meta/freshness",
    response_model=Envelope[Freshness],
    summary="When each materialised view was refreshed, and each job last succeeded",
)
@cached(Lifetime.METADATA)
async def read_freshness(session: SessionDep) -> Envelope[Freshness]:
    """Every materialised view, then every job that has recorded a run,
    alphabetically. A view never refreshed since its refresh began to be
    recorded, or a job that has never succeeded, is listed with nulls. Cached
    for five minutes."""
    return await build_freshness(session)
