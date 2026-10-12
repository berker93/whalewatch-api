"""``figi_response_log`` and :class:`DatabaseResponseLog`, on a real Postgres.

The client's behaviour is tests/test_openfigi_client.py's, against an in-memory
log. What is asserted here is what only the database can show: that a response
is committed per request — durable whatever the caller's transaction does — and
that a re-run against what was committed asks OpenFIGI nothing.

The commits are real, so the table is truncated around each test, as in
test_cli_discover_filings. OpenFIGI is a ``MockTransport``.
"""

import asyncio
import json
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.core.rate_limit import AsyncTokenBucket
from app.db.models.figi_response_log import FIGIOutcome, FigiResponseLog
from app.db.session import create_session_factory
from app.ingestion.enrichment.figi import DatabaseResponseLog, OpenFIGIClient

APPLE = "037833100"
NOWHERE = "000000000"
_APPLE = {"figi": "BBG000B9XRY4", "ticker": "AAPL", "exchCode": "US", "name": "APPLE INC"}


@pytest.fixture(autouse=True)
def _empty_log(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE figi_response_log"))

    asyncio.run(run())


def _openfigi(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        jobs = json.loads(request.content)
        known = {APPLE: {"data": [_APPLE]}}
        return httpx.Response(
            200,
            json=[known.get(job["idValue"], {"warning": "No identifier found."}) for job in jobs],
        )

    return httpx.MockTransport(handler)


def _client(
    settings: Settings, engine: AsyncEngine, requests: list[httpx.Request]
) -> OpenFIGIClient:
    return OpenFIGIClient(
        settings,
        DatabaseResponseLog(create_session_factory(engine)),
        limiter=AsyncTokenBucket(1000.0),
        transport=_openfigi(requests),
    )


async def test_responses_are_committed_and_a_re_run_requests_nothing(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    requests: list[httpx.Request] = []
    async with _client(settings, migrated_engine, requests) as figi:
        first = await figi.map_cusips([APPLE, NOWHERE])
    # A new client, as a new run would have: what it knows came from Postgres.
    async with _client(settings, migrated_engine, requests) as figi:
        second = await figi.map_cusips([APPLE, NOWHERE])

    assert len(requests) == 1
    assert second == first
    assert second[APPLE].matches[0].ticker == "AAPL"
    assert second[NOWHERE].outcome is FIGIOutcome.NOT_FOUND


async def test_rows_hold_the_cusip_outcome_and_verbatim_response(
    settings: Settings, migrated_engine: AsyncEngine
) -> None:
    async with _client(settings, migrated_engine, []) as figi:
        await figi.map_cusips([APPLE, NOWHERE])

    async with create_session_factory(migrated_engine)() as session:
        rows = (
            await session.execute(
                select(
                    FigiResponseLog.cusip, FigiResponseLog.outcome, FigiResponseLog.response
                ).order_by(FigiResponseLog.cusip)
            )
        ).all()
        stamps = await session.scalar(
            select(func.count(func.distinct(FigiResponseLog.requested_at)))
        )

    assert [tuple(row) for row in rows] == [
        (NOWHERE, "not_found", {"warning": "No identifier found."}),
        (APPLE, "found", {"data": [_APPLE]}),
    ]
    assert stamps == 1  # one request, one transaction, one timestamp


async def test_the_latest_answer_wins(settings: Settings, migrated_engine: AsyncEngine) -> None:
    log = DatabaseResponseLog(create_session_factory(migrated_engine))
    await log.record([(APPLE, {"warning": "No identifier found."})])
    await log.record([(APPLE, {"data": [_APPLE]})])

    latest = await log.latest([APPLE, NOWHERE])

    assert list(latest) == [APPLE]
    assert latest[APPLE].found


async def test_an_unknown_outcome_is_refused(db_session: AsyncSession) -> None:
    db_session.add(FigiResponseLog(cusip=APPLE, outcome="maybe", response={}))
    with pytest.raises(IntegrityError, match="ck_figi_response_log_outcome_is_known"):
        await db_session.flush()
