"""CON-6, against a real Postgres: the admin console's asset queue and overrides.

The lines are inserted as the PTR readers leave them and resolved as both PTR
jobs resolve them (the resolver's own cases are
tests/integration/test_congress_asset_resolution.py's). An admin sees what no
run resolved, says what an asset key is, and takes it back; each change is
logged in ``admin_action``.
"""

from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import admin_store
from app.core.config import Settings
from app.db.models import AdminAction, CongressFiling, CongressTransaction, Security
from app.ingestion.congress.asset_resolution import resolve_assets
from app.ingestion.congress.assets import asset_key
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND
from tests.integration.test_admin import admin_in, make_admin

URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf"
NINTENDO = asset_key("ST", "NTDOF", "NINTENDO CO LTD ORD")


@pytest.fixture
def console(
    app: FastAPI, client: AsyncClient, session_redis: FakeRedis, settings: Settings
) -> None:
    app.state.admin_sessions = admin_store(session_redis, settings)  # type: ignore[arg-type]


@pytest.fixture
async def admin(client: AsyncClient, db_session: AsyncSession, console: None) -> int:
    admin_id = await make_admin(db_session)
    assert (await admin_in(client)).status_code == 200
    return admin_id


@pytest.fixture
async def lines(db_session: AsyncSession) -> dict[str, int]:
    """Microsoft by its ticker, GE by a ticker its name disagrees with, Nintendo's
    ordinary shares twice with no US listing, and a muni."""
    for cusip, ours, listed in (
        ("594918104", "MICROSOFT CORP", "MSFT"),
        ("369604301", "GENERAL ELECTRIC", "GE"),
        ("654445303", "NINTENDO CO LTD-UNSPONS ADR", "NTDOY"),
    ):
        await db_session.execute(
            insert(Security).values(
                cusip=cusip, name=ours, filed_name=ours, ticker=listed, resolution_source="openfigi"
            )
        )
    filing_id = await db_session.scalar(
        insert(CongressFiling)
        .values(
            chamber="house",
            doc_id="20033725",
            report_type="ptr",
            last_name="Pelosi",
            filing_date=date(2026, 7, 8),
            year=2026,
            source_key="raw/house-clerk/index/2026/x.zip",
            document_url=URL,
            parser_version=1,
        )
        .returning(CongressFiling.id)
    )
    found: dict[str, int] = {}
    for number, (label, name, ticker, code) in enumerate(
        (
            ("msft", "Microsoft Corporation - Common Stock", "MSFT", "ST"),
            ("ge", "GE Aerospace Common Stock", "GE", "ST"),
            ("nintendo", "NINTENDO CO LTD ORD", "NTDOF", "ST"),
            ("nintendo_again", "Nintendo Co Ltd Ord", "NTDOF", "ST"),
            ("muni", "Taylor TX ISD BE/R/", None, None),
        ),
        start=1,
    ):
        line_id = await db_session.scalar(
            insert(CongressTransaction)
            .values(
                filing_id=filing_id,
                line=number,
                parse_status="parsed",
                extraction="text",
                asset_name=name,
                ticker=ticker,
                asset_type=code,
                transaction_type="purchase",
                transaction_date=date(2026, 6, 9),
                amount_low=1_001,
                amount_high=15_000,
            )
            .returning(CongressTransaction.id)
        )
        assert line_id is not None
        found[label] = line_id
    await resolve_assets(db_session)
    await db_session.commit()
    return found


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _queue(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/admin/congress/assets/review", params=params)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def _override(client: AsyncClient, **body: Any) -> Response:
    return await client.put("/admin/congress/assets/overrides", json=body, headers=FRONTEND)


async def test_the_queue_needs_an_admin(
    client: AsyncClient, console: None, lines: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/assets/review")

    assert _error(response) == (401, "unauthenticated")


async def test_the_queue_groups_unresolved_lines_by_asset_key(
    client: AsyncClient, admin: int, lines: dict[str, int]
) -> None:
    page = await _queue(client)

    assert page["total"] == 1
    [asset] = page["items"]
    assert {
        key: asset[key]
        for key in ("asset_key", "asset_class", "lines", "filings", "document_url", "security")
    } == {
        "asset_key": NINTENDO,
        "asset_class": "equity",
        "lines": 2,
        "filings": 1,
        "document_url": URL,
        "security": None,
    }
    assert asset["candidate"]["security"]["ticker"] == "NTDOY"


async def test_the_low_confidence_queue_shows_what_was_taken(
    client: AsyncClient, admin: int, lines: dict[str, int]
) -> None:
    page = await _queue(client, status="low_confidence")

    [asset] = page["items"]
    assert (asset["ticker"], asset["security"]["cusip"], asset["confidence"]) == (
        "GE",
        "369604301",
        "0.800",
    )


async def test_an_override_resolves_the_keys_lines_and_leaves_the_queue(
    client: AsyncClient, admin: int, lines: dict[str, int], db_session: AsyncSession
) -> None:
    response = await _override(
        client, asset_key=NINTENDO, asset_class="equity", ticker="ntdoy", note="as its ADR"
    )

    assert response.status_code == 200, response.text
    made = response.json()
    assert made["lines"] == 2
    assert (made["override"]["asset_class"], made["override"]["security"]["cusip"]) == (
        "equity",
        "654445303",
    )
    assert made["override"]["created_by"] == admin
    assert (await _queue(client))["total"] == 0
    review = await db_session.execute(
        select(CongressTransaction.resolution_method).where(
            CongressTransaction.id.in_([lines["nintendo"], lines["nintendo_again"]])
        )
    )
    assert set(review.scalars()) == {"manual"}
    listed = (await client.get("/admin/congress/assets/overrides")).json()
    assert [item["asset_key"] for item in listed["items"]] == [NINTENDO]

    removed = await client.delete(
        f"/admin/congress/assets/overrides/{made['override']['id']}", headers=FRONTEND
    )

    assert removed.status_code == 204
    assert (await _queue(client))["total"] == 1
    actions = await db_session.execute(
        select(AdminAction.action, AdminAction.admin_id)
        .where(AdminAction.action.like("asset_%"), AdminAction.admin_id == admin)
        .order_by(AdminAction.id)
    )
    assert [tuple(row) for row in actions] == [
        ("asset_overridden", admin),
        ("asset_override_removed", admin),
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"asset_class": "equity"}, (422, "validation_error")),
        ({"asset_class": "municipal_bond", "ticker": "NTDOY"}, (422, "validation_error")),
        (
            {"asset_class": "equity", "ticker": "NTDOY", "cusip": "654445303"},
            (422, "validation_error"),
        ),
        ({"asset_class": "equity", "ticker": "NOPE"}, (404, "not_found")),
        ({"asset_class": "unknown"}, (422, "validation_error")),
        ({"asset_class": "other", "asset_key": "ST|XX|nothing"}, (404, "not_found")),
    ],
)
async def test_an_override_is_refused_unless_it_says_one_thing(
    client: AsyncClient, admin: int, lines: dict[str, int], body: dict[str, Any], expected: Any
) -> None:
    response = await _override(client, **({"asset_key": NINTENDO} | body))

    assert _error(response) == expected


async def test_removing_no_override_is_a_404(
    client: AsyncClient, admin: int, lines: dict[str, int]
) -> None:
    response = await client.delete("/admin/congress/assets/overrides/999999", headers=FRONTEND)

    assert _error(response) == (404, "not_found")


async def test_coverage_says_how_much_resolved(
    client: AsyncClient, admin: int, lines: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/coverage")

    assert response.status_code == 200, response.text
    house = response.json()[0]
    assert house["assets"] == {
        "lines": 5,
        "pending": 0,
        "by_class": {"equity": 4, "municipal_bond": 1},
        "resolvable": 4,
        "resolved": 2,
        "by_method": {"ticker": 2},
        "low_confidence": 1,
        "coverage_pct": "50.0",
        "filings": 1,
        "filings_resolved": 0,
        "filings_partly": 1,
        "filing_coverage_pct": "0.0",
    }
