"""Draining the Form 4 queue: what ``ingest-form4`` works on, and in what order.

``crawl-form4`` queues Forms 4 and 4/A in ``pending_filing``. This plans a run
over them the way :func:`~app.ingestion.backfill.plan_backfill` plans one over
13Fs, with the same shapes, so that :func:`~app.ingestion.backfill.run_backfill`
runs either: the queue's filings not yet loaded and, with ``--force``, every
loaded Form 4 again from the raw store.

Oldest filing date first. Not needed for correctness, since an amendment and
its original can load in either order (:mod:`app.ingestion.loaders.insider`),
but an original loaded before its 4/A has its lines written once and deleted
once, rather than written and deleted in the same transaction.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import Date, Row, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filing import LOADED_STATUSES, Filing
from app.db.models.pending_filing import PendingFiling, PendingStatus
from app.ingestion.backfill import BackfillPlan, LoadedFiling, PlannedFiling
from app.ingestion.parsers.form4 import FORM_TYPES

_EDGAR_TIMEZONE: Final = "America/New_York"
_FORM_TYPES: Final = tuple(sorted(FORM_TYPES))


async def plan_form4(session: AsyncSession, *, force: bool, limit: int | None) -> BackfillPlan:
    """Every Form 4 queued or loaded, sorted into work and skips.

    As :func:`~app.ingestion.backfill.plan_backfill` does for 13Fs, including
    marking ``done`` a queue row whose filing is loaded. Does not commit.
    """
    queue = {
        row.accession_no: row
        for row in await session.execute(
            select(
                PendingFiling.accession_no,
                PendingFiling.cik,
                PendingFiling.filing_date,
                PendingFiling.status,
            ).where(PendingFiling.form_type.in_(_FORM_TYPES))
        )
    }
    filed = {
        row.accession_no: row
        for row in await session.execute(
            select(
                Filing.accession_no,
                Filing.cik,
                Filing.filed_at,
                cast(func.timezone(_EDGAR_TIMEZONE, Filing.filed_at), Date).label("filed_on"),
                Filing.parse_status,
                Filing.raw_key,
                Filing.source_url,
            ).where(Filing.form_type.in_(_FORM_TYPES))
        )
    }

    def loaded(row: Row[Any] | None) -> LoadedFiling | None:
        if row is None or row.parse_status not in LOADED_STATUSES:
            return None
        return LoadedFiling(filed_at=row.filed_at, raw_key=row.raw_key, source_url=row.source_url)

    planned = [
        PlannedFiling(
            accession_no=accession_no,
            cik=queued.cik,
            slug=None,
            filing_date=queued.filing_date,
            period=None,
            queued=True,
            loaded=loaded(filed.get(accession_no)),
        )
        for accession_no, queued in queue.items()
    ]
    planned.extend(
        PlannedFiling(
            accession_no=accession_no,
            cik=row.cik,
            slug=None,
            filing_date=row.filed_on,
            period=None,
            queued=False,
            loaded=loaded(row),
        )
        for accession_no, row in filed.items()
        if accession_no not in queue
    )
    planned.sort(key=lambda filing: (filing.filing_date, filing.accession_no))

    stale = [
        filing.accession_no
        for filing in planned
        if filing.loaded is not None
        and filing.queued
        and queue[filing.accession_no].status != PendingStatus.DONE
    ]
    if stale:
        await session.execute(
            update(PendingFiling)
            .where(PendingFiling.accession_no.in_(stale))
            .values(status=PendingStatus.DONE.value)
        )

    work = [filing for filing in planned if force or filing.loaded is None]
    return BackfillPlan(
        work=tuple(work if limit is None else work[:limit]),
        skipped=tuple(filing for filing in planned if not force and filing.loaded is not None),
        held_back=0 if limit is None else max(0, len(work) - limit),
    )
