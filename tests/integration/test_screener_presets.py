"""``/v1/screener/presets``, against a real Postgres and the presets the migrations wrote.

A preset is rows, not code, so the test that matters most is the one that
reads every preset a migration wrote through the vocabulary and runs it: a
misspelt filter in the next migration fails here, not on the page. The rest
is that a preset's screen is the screener's for the same filters, that its
filters are fixed, and that its URL takes a quarter.
"""

from datetime import date
from typing import Any
from urllib.parse import parse_qsl

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.entitlements import EntitlementContext, entitlement_context
from app.api.screener_presets import InvalidPresetError, parse_preset
from app.db.models import ScreenerPreset
from tests.integration.test_market_endpoint import _get
from tests.integration.test_screener_endpoint import _two_quarters

#: The launch set, in the order listed: the two that lead, then the rest.
LAUNCH = [
    "quiet-accumulation",
    "insiders-buying-what-funds-are-buying",
    "most-bought",
    "most-sold",
    "brand-new-positions",
    "everyone-sold-out",
    "big-bets",
]


async def test_every_preset_a_migration_wrote_reads_through_the_vocabulary(
    db_session: AsyncSession,
) -> None:
    rows = (await db_session.scalars(select(ScreenerPreset).order_by("position"))).all()

    assert [row.slug for row in rows] == LAUNCH
    for row in rows:
        preset = parse_preset(row)  # raises for a filter, value or sort it can't read
        assert preset.screen.applied, f"{row.slug} has no filters: it is every position"
        assert row.explanation.endswith("."), f"{row.slug}'s explanation is not a sentence"


async def test_the_listing_is_every_preset_in_order_with_its_filters(
    client: AsyncClient,
) -> None:
    body = await _get(client, "/v1/screener/presets")

    assert [p["slug"] for p in body["data"]] == LAUNCH
    assert body["page"] is None
    quiet = body["data"][0]
    assert quiet["name"] == "Quiet accumulation"
    assert quiet["filters"] == [
        {"key": "added_quarters", "label": "Added to, quarters running", "values": ["3"]}
    ]
    assert (quiet["sort"], quiet["order"]) == ("value", "desc")
    assert quiet["query"] == "added_quarters=3&sort=value&order=desc"


@pytest.mark.parametrize("slug", LAUNCH)
async def test_a_presets_screen_is_the_screeners_for_its_query(
    client: AsyncClient, db_session: AsyncSession, slug: str
) -> None:
    """The same rows, and the same filters echoed, as its `query` asked of
    ``GET /v1/screener`` for the quarter."""
    await _two_quarters(db_session)

    body = await _get(client, f"/v1/screener/presets/{slug}", period="2024Q2")
    preset = body["meta"]["preset"]
    screened = await _get(
        client, "/v1/screener", period="2024Q2", **dict(parse_qsl(preset["query"]))
    )

    assert preset["slug"] == slug
    assert body["data"] == screened["data"]
    assert body["meta"]["filters"] == screened["meta"]["filters"] == preset["filters"]


async def test_a_preset_is_of_the_latest_quarter_unless_given_another(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q1 is every filer's first period, so a change has nothing to be against."""
    await _two_quarters(db_session)

    latest = await _get(client, "/v1/screener/presets/brand-new-positions")
    first = await _get(client, "/v1/screener/presets/brand-new-positions", period="2024-03-31")

    assert latest["meta"]["period"] == "2024Q2"
    assert [(row["slug"], row["action"]) for row in latest["data"]] == [("value-a", "new")]
    assert latest["meta"]["preset"]["explanation"].startswith("Stocks investors bought")
    assert first["meta"]["period"] == "2024Q1"
    assert first["data"] == []


async def test_a_presets_filters_are_its_own(client: AsyncClient, db_session: AsyncSession) -> None:
    await _two_quarters(db_session)

    response = await client.get(
        "/v1/screener/presets/big-bets", params={"min_value": "5", "sort": "value"}
    )

    assert response.status_code == 422, response.text
    errors = {e["loc"][-1]: e["msg"] for e in response.json()["error"]["detail"]["errors"]}
    assert set(errors) == {"min_value", "sort"}
    assert "this screen's filters are its own" in errors["min_value"]


async def test_a_slug_no_preset_has_is_a_404(client: AsyncClient) -> None:
    response = await client.get("/v1/screener/presets/most-loved")

    assert response.status_code == 404
    assert "most-loved" in response.json()["error"]["message"]


async def test_a_preset_that_does_not_read_is_left_out_of_the_listing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(
        insert(ScreenerPreset).values(
            slug="misspelt",
            position=99,
            name="Misspelt",
            explanation="A preset a migration got wrong.",
            query={"min_wieght": ["5"], "action": ["bought"], "min_value": "5"},
            sort="biggest",
            sort_order="desc",
        )
    )
    row = await db_session.get(ScreenerPreset, "misspelt")
    assert row is not None

    with pytest.raises(InvalidPresetError) as raised:
        parse_preset(row)
    body = await _get(client, "/v1/screener/presets")

    message = str(raised.value)
    for part in ("min_wieght: not a filter", "bought", "min_value: a list", "'biggest'"):
        assert part in message
    assert "misspelt" not in [p["slug"] for p in body["data"]]


@pytest.mark.parametrize(
    "query",
    [{"period": ["2024Q2"], "min_weight": ["10"]}, ["min_weight", "10"]],
)
async def test_a_presets_query_is_filters_and_never_the_period(
    db_session: AsyncSession, query: Any
) -> None:
    with pytest.raises(IntegrityError, match="query_is_filters"):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(ScreenerPreset).values(
                    slug="pinned",
                    position=99,
                    name="Pinned",
                    explanation="A preset of one quarter.",
                    query=query,
                    sort="value",
                    sort_order="desc",
                )
            )


@pytest.fixture
def free(app: FastAPI) -> None:
    """Free, on 10 May 2025: 2024Q2 to 2025Q1, and trades from 10 November 2024."""
    app.dependency_overrides[entitlement_context] = lambda: EntitlementContext(
        history_quarters=4, insider_months=6, today=date(2025, 5, 10)
    )


@pytest.mark.usefixtures("free")
async def test_a_preset_is_free_but_its_quarter_is_the_plans(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Signed out is Free. Q2's insider trades are before Free's six months,
    and Q1 before its four quarters."""
    await _two_quarters(db_session)

    big_bets = await client.get("/v1/screener/presets/big-bets")
    insiders = await client.get("/v1/screener/presets/insiders-buying-what-funds-are-buying")
    before = await client.get("/v1/screener/presets/big-bets", params={"period": "2024Q1"})

    assert big_bets.status_code == 200
    assert (insiders.status_code, before.status_code) == (402, 402)
    assert insiders.json()["error"]["detail"]["limit"] == "insider_months"
    assert before.json()["error"]["detail"]["limit"] == "history_quarters"
