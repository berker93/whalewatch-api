"""``/v1/meta/periods`` and ``/v1/meta/freshness``, against a real Postgres.

The quarter rail's numbers have to be the numbers on the page each quarter
leads to, so the cases are the ones where a filing exists and is not the data:
a suspect filing withheld, an original replaced by a restatement, a filer that
has not filed. Completeness is decided on today's date, which a test cannot
move, so its boundaries are tested on rows built by hand.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import invalidate
from app.api.deps import get_redis
from app.api.routers.meta import _period_coverage
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, IngestionRun, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import MATERIALISED_VIEWS, refresh_views
from tests.fake_redis import FakeRedis

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)

_ciks = count(1)
_accessions = count(1)


@pytest.fixture
def redis(app: FastAPI) -> Iterator[FakeRedis]:
    fake = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake
    yield fake


async def _fund(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=1)
    )
    return filer_id


async def _file(
    session: AsyncSession,
    filer_id: int,
    period: date,
    filed_at: datetime,
    *,
    suspect: bool = False,
    restates: bool = False,
) -> None:
    """A 13F-HR for ``period`` holding one position, or a 13F-HR/A restating
    the period, inserted as loaded."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-{period:%y}-{next(_accessions):06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR/A" if restates else "13F-HR",
            amendment_kind=AmendmentKind.RESTATEMENT if restates else None,
            period_of_report=period,
            filed_at=filed_at,
            value_multiplier=1,
            parse_status="suspect" if suspect else "ok",
            parse_notes=(
                [{"kind": "entry_count", "severity": "error", "detail": "made up"}]
                if suspect
                else None
            ),
        )
        .returning(Filing.id)
    )
    await session.execute(
        pg_insert(Security)
        .values(cusip="037833100")
        .on_conflict_do_nothing(index_elements=[Security.cusip])
    )
    await session.execute(
        insert(Holding).values(
            filing_id=filing_id,
            security_id=select(Security.id).where(Security.cusip == "037833100").scalar_subquery(),
            filer_id=filer_id,
            period_of_report=period,
            cusip="037833100",
            value_usd=Decimal(1_000),
            shares=Decimal(10),
            sshprnamt_type="SH",
        )
    )


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, tzinfo=UTC)


async def _publish(session: AsyncSession) -> None:
    """``recompute --all``, then ``refresh-views``."""
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _get(client: AsyncClient, path: str) -> Any:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# --- periods --------------------------------------------------------------------


async def test_every_published_quarter_oldest_first_with_its_coverage(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    """Q1: all three report. Q2: one reports, one's filing is withheld (and
    the latest), one has not filed. Q3: nothing published, so not listed."""
    alpha, bravo, charlie = [await _fund(db_session, slug) for slug in ("a", "b", "c")]
    await _file(db_session, alpha, Q1, _at(date(2024, 4, 20)))
    await _file(db_session, bravo, Q1, _at(date(2024, 5, 15)))
    await _file(db_session, charlie, Q1, _at(date(2024, 5, 1)))
    await _file(db_session, alpha, Q2, _at(date(2024, 8, 1)))
    await _file(db_session, bravo, Q2, _at(date(2024, 8, 14)), suspect=True)
    await _file(db_session, charlie, Q3, _at(date(2024, 11, 1)), suspect=True)
    await _publish(db_session)

    body = await _get(client, "/v1/meta/periods")

    assert body["data"] == [
        {
            "period": "2024Q1",
            "period_end": "2024-03-31",
            "filing_deadline": "2024-05-15",
            "filers_reported": 3,
            "filers_tracked": 3,
            "is_complete": True,
            "first_filed_at": "2024-04-20T16:00:00Z",
            "last_filed_at": "2024-05-15T16:00:00Z",
        },
        {
            "period": "2024Q2",
            "period_end": "2024-06-30",
            "filing_deadline": "2024-08-14",
            "filers_reported": 1,
            "filers_tracked": 3,
            "is_complete": False,
            "first_filed_at": "2024-08-01T16:00:00Z",
            "last_filed_at": "2024-08-01T16:00:00Z",
        },
    ]
    assert body["meta"]["period"] is None
    assert body["meta"]["refreshed_at"] is not None
    assert body["page"] is None


async def test_a_quarters_filings_are_those_its_figures_are_built_from(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    """The original was filed first and replaced: the quarter's figures are the
    restatement's, and so are its filing times."""
    alpha = await _fund(db_session, "a")
    await _file(db_session, alpha, Q1, _at(date(2024, 4, 20)))
    await _file(db_session, alpha, Q1, _at(date(2024, 6, 1)), restates=True)
    await _publish(db_session)

    [q1] = (await _get(client, "/v1/meta/periods"))["data"]

    assert (q1["first_filed_at"], q1["last_filed_at"]) == (
        "2024-06-01T16:00:00Z",
        "2024-06-01T16:00:00Z",
    )


async def test_a_quarters_coverage_is_the_one_its_pages_state(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    """The rail and the page it leads to read one count."""
    alpha, bravo, _ = [await _fund(db_session, slug) for slug in ("a", "b", "c")]
    await _file(db_session, alpha, Q1, _at(date(2024, 4, 20)))
    await _file(db_session, bravo, Q1, _at(date(2024, 5, 1)), suspect=True)
    await _publish(db_session)

    [q1] = (await _get(client, "/v1/meta/periods"))["data"]
    page = await _get(client, "/v1/market/top-holdings?period=2024Q1")

    assert page["meta"]["coverage"] == {
        "filers_reported": q1["filers_reported"],
        "filers_tracked": q1["filers_tracked"],
    }
    assert page["meta"]["latest_filing_at"] == q1["last_filed_at"]


async def test_nothing_published_is_no_quarters(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    await _fund(db_session, "a")

    body = await _get(client, "/v1/meta/periods")

    assert body["data"] == []
    assert body["meta"]["refreshed_at"] is None


async def test_the_periods_are_cached_for_five_minutes(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    """A quarter published in the meantime is not listed until the answer
    expires, or a publish invalidates it."""
    alpha = await _fund(db_session, "a")
    await _file(db_session, alpha, Q1, _at(date(2024, 4, 20)))
    await _publish(db_session)

    first = await client.get("/v1/meta/periods")
    await _file(db_session, alpha, Q2, _at(date(2024, 8, 1)))
    await _publish(db_session)
    cached = await client.get("/v1/meta/periods")
    await invalidate(redis)  # type: ignore[arg-type]
    rebuilt = await client.get("/v1/meta/periods")

    assert first.headers["cache-control"] == "public, max-age=300"
    assert [r.headers["x-cache"] for r in (first, cached, rebuilt)] == ["MISS", "HIT", "MISS"]
    assert cached.json() == first.json()
    assert [row["period"] for row in rebuilt.json()["data"]] == ["2024Q1", "2024Q2"]


async def test_with_redis_down_the_periods_are_still_served(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    alpha = await _fund(db_session, "a")
    await _file(db_session, alpha, Q1, _at(date(2024, 4, 20)))
    await _publish(db_session)
    redis.down = True

    body = await _get(client, "/v1/meta/periods")

    assert [row["period"] for row in body["data"]] == ["2024Q1"]


# --- completeness, on rows built by hand ----------------------------------------


def _row(*, reported: int, tracked: int, today: date) -> Any:
    return SimpleNamespace(
        period_of_report=Q1,
        filers_reported=reported,
        filers_tracked=tracked,
        first_filed_at=None,
        last_filed_at=None,
        today=today,
    )


AFTER = date(2024, 5, 16)  # the day after Q1's deadline


@pytest.mark.parametrize(
    ("reported", "tracked", "today", "complete"),
    [
        (95, 100, AFTER, True),
        (94, 100, AFTER, False),
        (19, 20, AFTER, True),
        (18, 20, AFTER, False),
        # 95.2%, where a share rounded to whole percent would read 95.
        (20, 21, AFTER, True),
        # 94.7%, likewise rounded up to 95.
        (18, 19, AFTER, False),
        (100, 100, date(2024, 5, 15), False),  # the deadline day itself: not passed
        (0, 0, AFTER, False),
    ],
)
def test_a_quarter_is_complete_after_its_deadline_at_95_percent(
    reported: int, tracked: int, today: date, complete: bool
) -> None:
    coverage = _period_coverage(_row(reported=reported, tracked=tracked, today=today))
    assert coverage.is_complete is complete


# --- freshness ------------------------------------------------------------------


async def _run(
    session: AsyncSession, job_name: str, status: str, finished_at: datetime
) -> uuid.UUID:
    run_id = await session.scalar(
        insert(IngestionRun)
        .values(
            job_name=job_name,
            status=status,
            started_at=finished_at - timedelta(minutes=5),
            finished_at=finished_at,
            error=None if status == "success" else "RuntimeError: made up",
        )
        .returning(IngestionRun.id)
    )
    assert run_id is not None
    return run_id


async def test_freshness_lists_every_view_then_every_job(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    """A job's latest success, not its latest run. A partial run is not a
    success, so a job with nothing else has none."""
    refresh = uuid.uuid4()
    await refresh_views(db_session, run_id=refresh)
    await _run(db_session, "backfill_13f", "success", _at(date(2024, 1, 1)))
    latest = await _run(db_session, "backfill_13f", "success", _at(date(2024, 2, 1)))
    await _run(db_session, "backfill_13f", "failed", _at(date(2024, 3, 1)))
    await _run(db_session, "discover-filings", "partial", _at(date(2024, 3, 1)))

    body = await _get(client, "/v1/meta/freshness")

    views = [row for row in body["data"] if row["kind"] == "view"]
    jobs = [row for row in body["data"] if row["kind"] == "job"]
    assert body["data"] == views + jobs
    assert [row["name"] for row in views] == [view.name for view in MATERIALISED_VIEWS]
    assert {row["run_id"] for row in views} == {str(refresh)}
    assert all(row["last_success_at"] is not None for row in views)
    assert jobs == [
        {
            "kind": "job",
            "name": "backfill_13f",
            "last_success_at": "2024-02-01T16:00:00Z",
            "run_id": str(latest),
        },
        {"kind": "job", "name": "discover-filings", "last_success_at": None, "run_id": None},
    ]
    assert body["meta"]["period"] is None


async def test_a_view_never_refreshed_is_listed_as_unknown(
    client: AsyncClient, redis: FakeRedis
) -> None:
    body = await _get(client, "/v1/meta/freshness")

    assert body["data"] == [
        {"kind": "view", "name": view.name, "last_success_at": None, "run_id": None}
        for view in MATERIALISED_VIEWS
    ]
