"""With ``QUOTE_PROVIDER=null``, production's setting, every endpoint still answers 200.

06c's exit criterion, as a test: the API works with no market data at all. Every
GET in the OpenAPI document is called, over one published quarter pair, with
``security_quote`` empty. The paths are read from the document rather than
listed here, so a new endpoint is covered as soon as it exists, and a new path
parameter fails :func:`test_every_endpoint_answers_200` until it is given a
value below.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis
from app.core.config import Settings
from app.db.models import Filer, FilerCik, Filing, Holding, Security, SecurityQuote
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views
from tests.fake_redis import FakeRedis

Q1: Final = date(2024, 3, 31)
Q2: Final = date(2024, 6, 30)
CIK: Final = "0000000042"
ACCESSION: Final = "0000000042-24-000002"

#: A value for every path parameter and required query parameter in the API.
PARAMETERS: Final = {
    "accession_no": ACCESSION,
    "slug": "whale",
    "ticker": "NVDA",
    "q": "nvidia",
}


class _ReadyRedis(FakeRedis):
    """A fake that answers ``/ready``'s ping, which the shared one does not."""

    async def ping(self) -> bool:
        return True


async def _publish_two_quarters(session: AsyncSession) -> None:
    filer_id = await session.scalar(
        insert(Filer).values(name="WHALE CAPITAL", slug="whale").returning(Filer.id)
    )
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=CIK, priority=0))
    security_id = await session.scalar(
        insert(Security)
        .values(cusip="67066G104", name="NVIDIA CORP", ticker="NVDA", resolution_source="manual")
        .returning(Security.id)
    )
    for n, (period, shares) in enumerate([(Q1, 100), (Q2, 150)], start=1):
        filing_id = await session.scalar(
            insert(Filing)
            .values(
                accession_no=f"{CIK}-24-{n:06d}",
                cik=CIK,
                filer_id=filer_id,
                form_type="13F-HR",
                period_of_report=period,
                filed_at=datetime(period.year, period.month, period.day, 16, tzinfo=UTC)
                + timedelta(days=45),
                value_multiplier=1,
                parse_status="ok",
            )
            .returning(Filing.id)
        )
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=security_id,
                filer_id=filer_id,
                period_of_report=period,
                cusip="67066G104",
                value_usd=Decimal(shares * 120),
                shares=Decimal(shares),
                sshprnamt_type="SH",
            )
        )
    await recompute(session, EVERYTHING)
    await refresh_views(session)


def _gets(app: FastAPI) -> list[tuple[str, dict[str, Any]]]:
    """Every GET in the document, its path filled in and its required query
    parameters given."""
    calls = []
    for template, item in app.openapi()["paths"].items():
        if "get" not in item:
            continue
        required = [p for p in item["get"].get("parameters", []) if p.get("required")]
        path = template.format(**{p["name"]: PARAMETERS[p["name"]] for p in required})
        query = {p["name"]: PARAMETERS[p["name"]] for p in required if p["in"] == "query"}
        calls.append((path, query))
    return calls


async def test_every_endpoint_answers_200(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    app.dependency_overrides[get_redis] = lambda: _ReadyRedis()
    await _publish_two_quarters(db_session)
    calls = _gets(app)

    responses = {path: await client.get(path, params=query) for path, query in calls}

    assert settings.quote_provider == "null"
    assert await db_session.scalar(select(func.count()).select_from(SecurityQuote)) == 0
    assert len(calls) >= 20
    assert {path: r.status_code for path, r in responses.items() if r.status_code != 200} == {}
