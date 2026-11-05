"""CON-3, against a real Postgres: the admin console's PTR review queue.

Lines ``ingest-house-ptrs`` could not read in full wait as ``needs_review``;
an admin accepts them, as read or corrected, or rejects them, and each call is
logged in ``admin_action``. The rows are seeded as a run leaves them (the run
is tests/integration/test_cli_ingest_house_ptrs.py's).
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
from app.db.models import AdminAction, CongressFiling, CongressTransaction
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND
from tests.integration.test_admin import admin_in, make_admin

URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/9116212.pdf"


@pytest.fixture
def console(
    app: FastAPI, client: AsyncClient, session_redis: FakeRedis, settings: Settings
) -> None:
    """The admins' sessions, as the lifespan builds them."""
    app.state.admin_sessions = admin_store(session_redis, settings)  # type: ignore[arg-type]


@pytest.fixture
async def admin(client: AsyncClient, db_session: AsyncSession, console: None) -> int:
    """An admin, signed in to the console."""
    admin_id = await make_admin(db_session)
    assert (await admin_in(client)).status_code == 200
    return admin_id


async def _filing(session: AsyncSession, doc_id: str, **values: Any) -> int:
    filing_id = await session.scalar(
        insert(CongressFiling)
        .values(
            chamber="house",
            doc_id=doc_id,
            report_type="ptr",
            prefix="Hon.",
            first_name="Charles J.",
            last_name="Fleischmann",
            office="TN03",
            filing_date=values.pop("filing_date", date(2026, 7, 8)),
            year=2026,
            source_key="raw/house-clerk/index/2026/x.zip",
            document_url=URL,
            parser_version=1,
            **values,
        )
        .returning(CongressFiling.id)
    )
    assert filing_id is not None
    return filing_id


async def _line(session: AsyncSession, filing_id: int, line: int, **values: Any) -> int:
    row_id = await session.scalar(
        insert(CongressTransaction)
        .values(filing_id=filing_id, line=line, **values)
        .returning(CongressTransaction.id)
    )
    assert row_id is not None
    return row_id


@pytest.fixture
async def queue(db_session: AsyncSession) -> dict[str, int]:
    """A scan read by OCR — one line sure, one not, one with no type — a text
    filing read in full, and a scan with a placeholder."""
    scan = await _filing(db_session, "9116212", extraction="ocr", pages=1)
    common = {"extraction": "ocr", "owner": "JT", "page": 1}
    unsure = await _line(
        db_session,
        scan,
        1,
        parse_status="needs_review",
        asset_name="Tractor Supp1y Co TSCO",
        transaction_type="sale",
        transaction_date=date(2026, 6, 9),
        notification_date=date(2026, 7, 1),
        amount_low=1_001,
        amount_high=15_000,
        confidence=0.588,
        problems=["OCR confidence 0.59 under 0.80"],
        raw_text="JT | Tractor Supp1y Co TSCO | 06/09/26 | 07/01/26",
        **common,
    )
    await _line(
        db_session,
        scan,
        2,
        parse_status="parsed",
        asset_name="Zoetis Inc ZTS",
        transaction_type="purchase",
        transaction_date=date(2026, 6, 9),
        amount_low=15_001,
        amount_high=50_000,
        confidence=0.856,
        **common,
    )
    no_type = await _line(
        db_session,
        scan,
        3,
        parse_status="needs_review",
        asset_name="Example Mega Corp",
        confidence=0.0,
        problems=["no single type box ticked"],
        **common,
    )
    text = await _filing(
        db_session, "20034179", extraction="text", pages=1, filing_date=date(2026, 3, 17)
    )
    await _line(
        db_session,
        text,
        1,
        parse_status="parsed",
        extraction="text",
        asset_name="Microsoft Corporation - Common Stock",
        ticker="MSFT",
        transaction_type="purchase",
        transaction_date=date(2026, 3, 12),
        amount_low=1_001,
        amount_high=15_000,
    )
    paper = await _filing(
        db_session,
        "8221334",
        extraction="ocr",
        pages=2,
        parse_error="OCR found no form grid on any page",
        filing_date=date(2026, 2, 11),
    )
    await _line(
        db_session,
        paper,
        1,
        parse_status="placeholder",
        extraction="ocr",
        problems=["OCR found no form grid on any page"],
    )
    await db_session.commit()
    return {"unsure": unsure, "no_type": no_type}


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _review(client: AsyncClient, line_id: int, **body: Any) -> Response:
    return await client.post(f"/admin/congress/review/{line_id}", json=body, headers=FRONTEND)


async def _actions(session: AsyncSession) -> list[tuple[str, dict[str, Any]]]:
    rows = await session.execute(
        select(AdminAction.action, AdminAction.detail)
        .where(AdminAction.action.like("ptr_row_%"))
        .order_by(AdminAction.id)
    )
    return [tuple(row) for row in rows]


# --- the queue -----------------------------------------------------------------------


async def test_the_queue_needs_an_admin(
    client: AsyncClient, console: None, queue: dict[str, int]
) -> None:
    assert _error(await client.get("/admin/congress/review")) == (401, "unauthenticated")


async def test_the_queue_lists_the_lines_in_review_beside_their_filing(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/review")

    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (2, 50, 0)
    assert [item["id"] for item in page["items"]] == [queue["unsure"], queue["no_type"]]
    first = page["items"][0]
    assert first["filing"] == {
        "id": first["filing"]["id"],
        "chamber": "house",
        "doc_id": "9116212",
        "member": "Hon. Charles J. Fleischmann",
        "office": "TN03",
        "filing_date": "2026-07-08",
        "document_url": URL,
        "extraction": "ocr",
        "pages": 1,
        "parse_error": None,
    }
    assert (first["line"], first["parse_status"], first["confidence"]) == (
        1,
        "needs_review",
        "0.588",
    )
    assert first["problems"] == ["OCR confidence 0.59 under 0.80"]
    assert first["raw_text"].startswith("JT | Tractor Supp1y")


async def test_the_queue_lists_placeholders_too(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/review", params={"status": "placeholder"})

    [item] = response.json()["items"]
    assert (item["filing"]["doc_id"], item["parse_status"], item["asset_name"]) == (
        "8221334",
        "placeholder",
        None,
    )
    assert item["filing"]["parse_error"] == "OCR found no form grid on any page"


# --- reviewing -----------------------------------------------------------------------


async def test_accepting_a_line_with_a_correction_makes_it_parsed_and_logs_it(
    client: AsyncClient, admin: int, db_session: AsyncSession, queue: dict[str, int]
) -> None:
    response = await _review(
        client,
        queue["unsure"],
        decision="accept",
        corrections={"asset_name": "Tractor Supply Co", "ticker": "TSCO"},
        note="1 for l",
    )

    assert response.status_code == 200, response.text
    line = response.json()
    assert (line["parse_status"], line["asset_name"], line["ticker"], line["review_note"]) == (
        "parsed",
        "Tractor Supply Co",
        "TSCO",
        "1 for l",
    )
    assert line["reviewed_at"] is not None
    # What the reader found wrong stays on the row.
    assert line["problems"] == ["OCR confidence 0.59 under 0.80"]
    reviewed_by = await db_session.scalar(
        select(CongressTransaction.reviewed_by).where(CongressTransaction.id == queue["unsure"])
    )
    assert reviewed_by == admin
    assert await _actions(db_session) == [
        (
            "ptr_row_accepted",
            {
                "transaction_id": queue["unsure"],
                "chamber": "house",
                "doc_id": "9116212",
                "line": 1,
                "corrected": ["asset_name", "ticker"],
            },
        )
    ]
    # Out of the queue.
    assert (await client.get("/admin/congress/review")).json()["total"] == 1


async def test_a_correction_can_clear_a_field(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await _review(
        client, queue["unsure"], decision="accept", corrections={"owner": None}
    )

    assert response.json()["owner"] is None


async def test_a_line_is_accepted_only_complete(
    client: AsyncClient, admin: int, db_session: AsyncSession, queue: dict[str, int]
) -> None:
    response = await _review(client, queue["no_type"], decision="accept")

    assert _error(response) == (422, "validation_error")
    assert "transaction_type" in response.json()["error"]["message"]
    not_a_band = await _review(
        client,
        queue["unsure"],
        decision="accept",
        corrections={"amount_low": 1_000, "amount_high": 15_000},
    )
    assert _error(not_a_band) == (422, "validation_error")
    assert await _actions(db_session) == []


async def test_a_line_with_its_type_supplied_is_accepted(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await _review(
        client,
        queue["no_type"],
        decision="accept",
        corrections={
            "asset_name": "Ford Motor Company",
            "transaction_type": "purchase",
            "transaction_date": "2018-06-19",
            "amount_low": 1001,
            "amount_high": 15000,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["transaction_type"] == "purchase"


async def test_a_line_can_be_accepted_with_an_exact_figure_for_its_band(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    both = await _review(
        client, queue["unsure"], decision="accept", corrections={"amount_exact": "710.52"}
    )
    assert _error(both) == (422, "validation_error")

    response = await _review(
        client,
        queue["unsure"],
        decision="accept",
        corrections={"amount_exact": "710.52", "amount_low": None, "amount_high": None},
    )

    assert response.status_code == 200, response.text
    line = response.json()
    assert (line["amount_low"], line["amount_high"], line["amount_exact"]) == (None, None, "710.52")


async def test_rejecting_a_line_keeps_it_out_of_the_feed(
    client: AsyncClient, admin: int, db_session: AsyncSession, queue: dict[str, int]
) -> None:
    response = await _review(client, queue["no_type"], decision="reject", note="the example")

    assert response.status_code == 200, response.text
    assert response.json()["parse_status"] == "rejected"
    status = await db_session.scalar(
        select(CongressTransaction.parse_status).where(CongressTransaction.id == queue["no_type"])
    )
    assert status == "rejected"
    assert [action for action, _ in await _actions(db_session)] == ["ptr_row_rejected"]


async def test_a_rejection_takes_no_corrections(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await _review(
        client, queue["no_type"], decision="reject", corrections={"owner": "SP"}
    )

    assert _error(response) == (422, "validation_error")


async def test_a_line_is_reviewed_once(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    assert (await _review(client, queue["no_type"], decision="reject")).status_code == 200

    again = await _review(client, queue["no_type"], decision="reject")

    assert _error(again) == (409, "not_in_review")
    assert _error(await _review(client, 999_999, decision="reject")) == (404, "not_found")


# --- coverage ------------------------------------------------------------------------


async def test_coverage_counts_each_chambers_filings_and_lines(
    client: AsyncClient, admin: int, queue: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/coverage")

    assert response.status_code == 200, response.text
    house, senate = response.json()
    assert house == {
        "chamber": "house",
        "filings_seen": 3,
        "filings_read": 2,
        "review_only": 0,
        "by_text": 1,
        "by_ocr": 1,
        "by_html": 0,
        "placeholders": 1,
        "missing": 0,
        "rows_parsed": 2,
        "rows_needs_review": 2,
        "coverage_pct": "66.7",
        # Inserted as an index run would, but not linked: no run has matched them.
        "members_matched": 0,
        "members_in_review": 0,
        "members_unmatched": 0,
        "members_unlinked": 3,
        # Inserted as a read leaves them: no run has resolved their assets yet.
        "assets": {
            "lines": 2,
            "pending": 2,
            "by_class": {},
            "resolvable": 0,
            "resolved": 0,
            "by_method": {},
            "low_confidence": 0,
            "coverage_pct": None,
            "filings": 0,
            "filings_resolved": 0,
            "filings_partly": 0,
            "filing_coverage_pct": None,
        },
    }
    assert (senate["chamber"], senate["filings_seen"], senate["coverage_pct"]) == (
        "senate",
        0,
        None,
    )
