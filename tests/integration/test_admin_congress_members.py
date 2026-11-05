"""CON-5, against a real Postgres: the admin console's member review queue.

The registry is the fixture cut of the congress-legislators dataset, stored as
``seed-congress-members`` stores it; the filings are inserted as an index run
leaves them, and linked and matched as it would (the runs are
tests/integration/test_cli_congress_members.py's). An admin accepts a
candidate, assigns a member, or rejects the candidates, and each call is
logged in ``admin_action``.
"""

from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import admin_store
from app.core.config import Settings
from app.db.models import AdminAction, CongressFiledName, CongressFiling
from app.ingestion.congress.legislators import FILES, Copy, read_dataset
from app.ingestion.congress.members import link_and_match, store_members
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND
from tests.integration.test_admin import admin_in, make_admin

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "congress_legislators"
URL: Final = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/{}.pdf"


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


async def _filing(
    session: AsyncSession, doc_id: str, first: str, last: str, office: str, filed: date
) -> None:
    await session.execute(
        insert(CongressFiling).values(
            chamber="house",
            doc_id=doc_id,
            report_type="ptr",
            prefix="Hon.",
            first_name=first,
            last_name=last,
            office=office,
            filing_date=filed,
            year=filed.year,
            source_key="raw/house-clerk/index/2026/x.zip",
            document_url=URL.format(doc_id),
        )
    )


@pytest.fixture
async def names(db_session: AsyncSession) -> dict[str, int]:
    """Pelosi, matched; Rohit Khanna twice, in review; a stranger, unmatched."""
    dataset = read_dataset(
        [
            Copy(name, (FIXTURES / f"{name}.json").read_bytes(), f"raw/cl/{name}.json", "u")
            for name in FILES
        ]
    )
    await store_members(db_session, dataset)
    await _filing(db_session, "20033725", "Nancy", "Pelosi", "CA11", date(2026, 1, 23))
    await _filing(db_session, "20033801", "Rohit", "Khanna", "CA17", date(2026, 2, 2))
    await _filing(db_session, "20034410", "Rohit", "Khanna", "CA17", date(2026, 9, 14))
    await _filing(db_session, "20034100", "Zebulon", "Quartermaine", "WY00", date(2026, 5, 5))
    await link_and_match(db_session)
    await db_session.commit()
    rows = await db_session.execute(select(CongressFiledName.name_key, CongressFiledName.id))
    return dict(rows.tuples().all())


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _decide(client: AsyncClient, name_id: int, **body: Any) -> Response:
    return await client.post(
        f"/admin/congress/members/review/{name_id}", json=body, headers=FRONTEND
    )


async def _actions(session: AsyncSession) -> list[tuple[str, dict[str, Any]]]:
    rows = await session.execute(
        select(AdminAction.action, AdminAction.detail)
        .where(AdminAction.action.like("member_%"))
        .order_by(AdminAction.id)
    )
    return [tuple(row) for row in rows]


async def _queued(client: AsyncClient, status: str = "review") -> list[dict[str, Any]]:
    response = await client.get("/admin/congress/members/review", params={"status": status})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


# --- the queue -----------------------------------------------------------------------


async def test_the_queue_needs_an_admin(
    client: AsyncClient, console: None, names: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/members/review")

    assert _error(response) == (401, "unauthenticated")


async def test_the_queue_lists_names_in_review_with_their_candidates(
    client: AsyncClient, admin: int, names: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/members/review")

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    item = page["items"][0]
    candidate = item["candidates"][0]
    assert item == {
        "id": names["rohit khanna"],
        "chamber": "house",
        "state": "CA",
        "first_name": "Rohit",
        "last_name": "Khanna",
        "name_key": "rohit khanna",
        "status": "review",
        "method": None,
        "confidence": "0.643",
        "member": None,
        "filings": 2,
        "first_filed": "2026-02-02",
        "last_filed": "2026-09-14",
        "document_url": URL.format("20034410"),
        "candidates": [
            {
                "id": candidate["id"],
                "member": {
                    "bioguide_id": "K000389",
                    "full_name": "Ro Khanna",
                    "chamber": "house",
                    "state": "CA",
                    "district": 17,
                    "party": "Democrat",
                    "current": True,
                },
                "method": "trigram",
                "confidence": "0.643",
            }
        ],
        "reviewed_at": None,
        "review_note": None,
    }


async def test_the_unmatched_are_listed_apart(
    client: AsyncClient, admin: int, names: dict[str, int]
) -> None:
    unmatched = await _queued(client, "unmatched")

    assert [(item["name_key"], item["candidates"]) for item in unmatched] == [
        ("zebulon quartermaine", [])
    ]


async def test_coverage_counts_filings_by_whether_their_filer_is_known(
    client: AsyncClient, admin: int, names: dict[str, int]
) -> None:
    response = await client.get("/admin/congress/coverage")

    house = next(row for row in response.json() if row["chamber"] == "house")
    assert (
        house["members_matched"],
        house["members_in_review"],
        house["members_unmatched"],
        house["members_unlinked"],
    ) == (1, 2, 1, 0)


# --- decisions -----------------------------------------------------------------------


async def test_accepting_a_candidate_matches_every_filing_under_the_name(
    client: AsyncClient, admin: int, db_session: AsyncSession, names: dict[str, int]
) -> None:
    candidate = (await _queued(client))[0]["candidates"][0]["id"]

    response = await _decide(
        client, names["rohit khanna"], decision="accept", candidate_id=candidate, note="Ro"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["method"], body["confidence"]) == ("matched", "manual", "1.000")
    assert body["member"]["bioguide_id"] == "K000389"
    assert body["candidates"] == []
    assert body["review_note"] == "Ro"
    assert await _queued(client) == []
    assert await _actions(db_session) == [
        (
            "member_matched",
            {
                "filed_name_id": names["rohit khanna"],
                "chamber": "house",
                "state": "CA",
                "name_key": "rohit khanna",
                "decision": "accept",
                "bioguide_id": "K000389",
                "previous_bioguide_id": None,
                "rejected": [],
            },
        )
    ]


async def test_a_persons_match_is_not_undone_by_a_rematch(
    client: AsyncClient, admin: int, db_session: AsyncSession, names: dict[str, int]
) -> None:
    """Assigning Pelosi's name to someone else, wrongly: the next pass leaves it."""
    await _decide(client, names["nancy pelosi"], decision="assign", bioguide_id="K000389")

    await link_and_match(db_session, every_name=True)

    name = await db_session.get(CongressFiledName, names["nancy pelosi"])
    assert name is not None
    await db_session.refresh(name)
    assert (name.method, name.status) == ("manual", "matched")


async def test_assigning_corrects_an_automatic_match(
    client: AsyncClient, admin: int, db_session: AsyncSession, names: dict[str, int]
) -> None:
    response = await _decide(
        client, names["nancy pelosi"], decision="assign", bioguide_id="W000797"
    )

    assert response.status_code == 200, response.text
    assert response.json()["member"]["bioguide_id"] == "W000797"
    action = (await _actions(db_session))[0][1]
    assert (action["bioguide_id"], action["previous_bioguide_id"]) == ("W000797", "P000197")


async def test_rejecting_leaves_the_name_unmatched_and_the_member_never_suggested_again(
    client: AsyncClient, admin: int, db_session: AsyncSession, names: dict[str, int]
) -> None:
    response = await _decide(client, names["rohit khanna"], decision="reject")

    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["candidates"]) == ("unmatched", [])
    await link_and_match(db_session, every_name=True)
    await db_session.commit()
    assert await _queued(client) == []
    assert [item["name_key"] for item in await _queued(client, "unmatched")] == [
        "rohit khanna",
        "zebulon quartermaine",
    ]
    assert (await _actions(db_session))[0][0] == "member_rejected"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"decision": "accept"}, (422, "validation_error")),
        ({"decision": "assign"}, (422, "validation_error")),
        ({"decision": "assign", "bioguide_id": "Z999999"}, (404, "not_found")),
        ({"decision": "accept", "candidate_id": 999_999}, (404, "not_found")),
        ({"decision": "reject", "candidate_id": 1}, (422, "validation_error")),
    ],
)
async def test_a_decision_that_does_not_add_up_is_refused(
    client: AsyncClient,
    admin: int,
    names: dict[str, int],
    body: dict[str, Any],
    expected: tuple[int, str],
) -> None:
    assert _error(await _decide(client, names["rohit khanna"], **body)) == expected


async def test_accepting_or_rejecting_a_name_with_no_candidates_is_a_conflict(
    client: AsyncClient, admin: int, names: dict[str, int]
) -> None:
    for decision in ("accept", "reject"):
        body: dict[str, Any] = {"decision": decision}
        if decision == "accept":
            body["candidate_id"] = 1
        response = await _decide(client, names["nancy pelosi"], **body)

        assert _error(response) == (409, "not_in_review")


async def test_no_such_name_is_a_404(
    client: AsyncClient, admin: int, names: dict[str, int]
) -> None:
    response = await _decide(client, 999_999, decision="reject")

    assert _error(response) == (404, "not_found")
