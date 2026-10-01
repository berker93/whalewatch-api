"""What ``runs`` lists: the latest rows of ``ingestion_run``, newest first."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.ingestion_run import IngestionRun


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One run, as the listing shows it."""

    id: uuid.UUID
    job_name: str
    status: str
    started_at: datetime

    elapsed: timedelta
    """Start to finish, or to now for a run still ``running``. From the
    database's clock throughout, so it never mixes two machines' clocks."""

    items_seen: int
    items_written: int
    error: str | None


async def recent_runs(
    session: AsyncSession, *, job_name: str | None = None, limit: int
) -> list[RunSummary]:
    """The ``limit`` most recently started runs, of ``job_name`` or of every job."""
    statement = (
        select(
            IngestionRun.id,
            IngestionRun.job_name,
            IngestionRun.status,
            IngestionRun.started_at,
            (func.coalesce(IngestionRun.finished_at, func.now()) - IngestionRun.started_at).label(
                "elapsed"
            ),
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.error,
        )
        # id breaks ties between runs started in one transaction's now(),
        # which only a test manages, so that the order is the same every time.
        .order_by(IngestionRun.started_at.desc(), IngestionRun.id)
        .limit(limit)
    )
    if job_name is not None:
        statement = statement.where(IngestionRun.job_name == job_name)
    return [RunSummary(**row._mapping) for row in await session.execute(statement)]


async def job_names(session: AsyncSession) -> list[str]:
    """Every job that has a run recorded, alphabetically."""
    return list(
        await session.scalars(
            select(IngestionRun.job_name).distinct().order_by(IngestionRun.job_name)
        )
    )
