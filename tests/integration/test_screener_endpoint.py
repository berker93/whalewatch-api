"""``/v1/screener``, against a real Postgres.

One made-up pair of quarters (:func:`_two_quarters`), published as an operator
does, small enough that every filter's rows can be worked out by hand. The
cases that matter are the ones a wrong reading would get wrong without
failing: a filer's first period passing a change filter, an exit showing up in
a screen of holdings, two filters satisfied by two different rows of one
stock, an insider cluster found by its issuer rather than its security, and a
misspelt filter widening the screen instead of refusing it.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.entitlements import EntitlementContext, entitlement_context
from app.api.errors import ApiError
from app.api.routers.screener import statement_timeout
from app.api.screener import VOCABULARY
from app.db.models import Filer, InsiderCluster, SecCompanyTicker, Security
from tests.integration.test_insiders_endpoint import _form4, _insider, _line
from tests.integration.test_market_endpoint import _fund, _get, _publish, _quarter, _walk

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
ALPHA = "11111A101"  # held by value-a and activist-b throughout
BRAVO = "22222B202"  # value-a adds to it; its company's insiders cluster, by CIK
CHARLIE = "33333C303"  # value-a opens it; newcomer holds it in its first period
DELTA = "44444D404"  # activist-b sells out of it
BRAVO_CIK = "0000000222"


async def _two_quarters(session: AsyncSession) -> dict[str, int]:
    """2024Q1 and Q2 at $10 a share, so every value is shares times ten.

    ``value-a`` (value): A 100 and B 100, then A 100, B 300 and C 600: a hold
    at 10%, an add to 30% and a new position at 60%, from weights of 50 each.
    ``activist-b`` (activist): A 100 and D 100, then A alone: a hold up 50
    points to 100%, and an exit. ``newcomer`` (growth): C 100 in Q2, its first
    period, which is every position ``new`` and none of them a change.
    """
    value_a, activist_b, newcomer = [
        await _fund(session, slug) for slug in ("value-a", "activist-b", "newcomer")
    ]
    for filer_id, category in (
        (value_a, "value"),
        (activist_b, "activist"),
        (newcomer, "growth"),
    ):
        await session.execute(update(Filer).where(Filer.id == filer_id).values(category=category))
    await _quarter(session, value_a, Q1, {ALPHA: 100, BRAVO: 100})
    await _quarter(session, value_a, Q2, {ALPHA: 100, BRAVO: 300, CHARLIE: 600})
    await _quarter(session, activist_b, Q1, {ALPHA: 100, DELTA: 100})
    await _quarter(session, activist_b, Q2, {ALPHA: 100})
    await _quarter(session, newcomer, Q2, {CHARLIE: 100})
    await _publish(session)
    return {"value-a": value_a, "activist-b": activist_b, "newcomer": newcomer}


def _rows(body: Any) -> list[tuple[str, str]]:
    return [(row["slug"], row["cusip"]) for row in body["data"]]


async def _screen(client: AsyncClient, **params: Any) -> list[tuple[str, str]]:
    return _rows(await _get(client, "/v1/screener", period="2024Q2", **params))


async def _refused(client: AsyncClient, query: str) -> dict[str, Any]:
    response = await client.get(f"/v1/screener?{query}")
    assert response.status_code == 422, response.text
    error: dict[str, Any] = response.json()["error"]
    return error


def _errors(error: dict[str, Any]) -> dict[str, str]:
    return {e["loc"][-1]: e["msg"] for e in error["detail"]["errors"]}


# --- the rows -------------------------------------------------------------------


async def test_the_period_alone_is_every_position_held_at_its_end_largest_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Activist-b's exit from Delta is not a position held, so not a row.
    Three rows of $1,000 tie, and go to the later filer first."""
    await _two_quarters(db_session)

    body = await _get(client, "/v1/screener", period="2024-06-30")

    assert _rows(body) == [
        ("value-a", CHARLIE),
        ("value-a", BRAVO),
        ("newcomer", CHARLIE),
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
    ]
    assert body["meta"]["period"] == "2024Q2"
    assert body["meta"]["filters"] == []
    assert body["meta"]["coverage"] == {"filers_reported": 3, "filers_tracked": 3}
    assert body["meta"]["refreshed_at"] is not None
    assert body["page"] == {"limit": 50, "next_cursor": None}


async def test_a_row_is_the_position_its_stock_and_how_sure_the_price_is(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_quarters(db_session)

    body = await _get(client, "/v1/screener", period="2024Q2", filer_slug="value-a")
    bravo = next(row for row in body["data"] if row["cusip"] == BRAVO)

    assert {k: bravo[k] for k in ("display_name", "category", "prev_period", "action")} == {
        "display_name": "VALUE-A",
        "category": "value",
        "prev_period": "2024-03-31",
        "action": "add",
    }
    assert (bravo["shares"], bravo["shares_delta"], bravo["value_usd"]) == (
        "300.0000",
        "200.0000",
        "3000.00",
    )
    assert (bravo["weight_pct"], bravo["weight_delta"]) == ("30.000000", "-20.000000")
    # One holder; 200 shares bought at $10, and nobody sold.
    assert (bravo["holder_count"], bravo["net_value_usd"]) == (1, "2000.00")
    assert bravo["price_estimate"]["price"] == "10.00000000"
    assert bravo["price_estimate"]["confidence"] == "low"
    assert bravo["price_estimate"]["observation_count"] == 1
    assert bravo["is_split"] is False


async def test_a_first_period_passes_no_change_filter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Newcomer's Charlie is `new` only because Q2 is the first period we have."""
    await _two_quarters(db_session)

    assert await _screen(client, action="new") == [("value-a", CHARLIE)]
    assert await _screen(client, is_new_position="true") == [("value-a", CHARLIE)]
    assert await _screen(client, is_new_position="false") == [
        ("value-a", BRAVO),
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
    ]
    assert ("newcomer", CHARLIE) not in await _screen(client, weight_change="1")


async def test_an_exit_is_a_row_only_when_asked_for(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_quarters(db_session)

    assert await _screen(client, action="exit") == [("activist-b", DELTA)]
    assert await _screen(client, action="hold,exit") == [
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
        ("activist-b", DELTA),
    ]


async def test_each_position_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    await _two_quarters(db_session)

    assert await _screen(client, min_value="3000") == [("value-a", CHARLIE), ("value-a", BRAVO)]
    assert await _screen(client, min_weight="60") == [
        ("value-a", CHARLIE),
        ("newcomer", CHARLIE),
        ("activist-b", ALPHA),
    ]
    # Signed: up by at least 50 points, and down by at least 20.
    assert await _screen(client, weight_change="50") == [
        ("value-a", CHARLIE),
        ("activist-b", ALPHA),
    ]
    assert await _screen(client, weight_change="-20") == [("value-a", BRAVO), ("value-a", ALPHA)]


async def test_each_investor_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    await _two_quarters(db_session)

    assert await _screen(client, filer_category="activist,growth") == [
        ("newcomer", CHARLIE),
        ("activist-b", ALPHA),
    ]
    both = await _screen(client, filer_slug=["activist-b", "newcomer"])
    assert both == await _screen(client, filer_slug="newcomer,activist-b")
    assert both == [("newcomer", CHARLIE), ("activist-b", ALPHA)]


async def test_each_stock_filter(client: AsyncClient, db_session: AsyncSession) -> None:
    """Holders at the period end, and the quarter's net flow, across every filer."""
    await _two_quarters(db_session)

    assert await _screen(client, min_holders="2") == [
        ("value-a", CHARLIE),
        ("newcomer", CHARLIE),
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
    ]
    assert await _screen(client, net_flow="buy") == [
        ("value-a", CHARLIE),
        ("value-a", BRAVO),
        ("newcomer", CHARLIE),
    ]
    # Delta was sold, and nobody holds it: only its exit is a row.
    assert await _screen(client, net_flow="sell") == []
    assert await _screen(client, net_flow="sell", action="exit") == [("activist-b", DELTA)]


async def test_filters_are_anded_on_one_position(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Charlie is held by two, and value-a opened it: both of one row. Alpha
    is held by two too, but value-a holds rather than opens it."""
    await _two_quarters(db_session)

    body = await _get(
        client,
        "/v1/screener",
        period="2024Q2",
        min_holders="2",
        filer_category="value",
        action="new",
        min_weight="50.5",
    )

    assert _rows(body) == [("value-a", CHARLIE)]
    assert body["meta"]["filters"] == [
        {"key": "action", "label": "What happened", "values": ["new"]},
        {"key": "min_holders", "label": "How many investors", "values": ["2"]},
        {"key": "min_weight", "label": "Share of their portfolio", "values": ["50.5"]},
        {"key": "filer_category", "label": "Kind of investor", "values": ["value"]},
    ]


async def test_an_insider_cluster_is_the_securitys_or_its_issuers_in_the_quarter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Charlie's episode is linked to it. Bravo's is linked to no security,
    and found by the CIK SEC lists Bravo's ticker under. Alpha's is the
    quarter before, and another grid point's is not the screen's."""
    await _two_quarters(db_session)
    await db_session.execute(
        update(Security)
        .where(Security.cusip == BRAVO)
        .values(ticker="BRV.A", resolution_source="manual")
    )
    await db_session.execute(
        insert(SecCompanyTicker).values(
            ticker="BRV-A", cik=BRAVO_CIK, name="BRAVO CORP", seen_at=datetime.now(UTC)
        )
    )

    def episode(cik: str, cusip: str | None, start: date, end: date, **grid: int) -> Any:
        security_id = (
            None
            if cusip is None
            else select(Security.id).where(Security.cusip == cusip).scalar_subquery()
        )
        return insert(InsiderCluster).values(
            window_days=grid.get("window_days", 30),
            min_insiders=grid.get("min_insiders", 3),
            issuer_cik=cik,
            window_start=start,
            window_end=end,
            issuer_name=f"ISSUER {cik}",
            security_id=security_id,
            insider_count=3,
            transaction_count=3,
            # Made up: nothing here reads the lines.
            transaction_ids=[1, 2, 3],
        )

    await db_session.execute(episode("0000000333", CHARLIE, date(2024, 3, 25), date(2024, 4, 2)))
    await db_session.execute(episode(BRAVO_CIK, None, date(2024, 6, 10), date(2024, 6, 20)))
    await db_session.execute(episode("0000000111", ALPHA, date(2024, 2, 1), date(2024, 2, 9)))
    await db_session.execute(
        episode("0000000111", ALPHA, date(2024, 5, 1), date(2024, 5, 9), min_insiders=2)
    )

    assert await _screen(client, insider_cluster="true") == [
        ("value-a", CHARLIE),
        ("value-a", BRAVO),
        ("newcomer", CHARLIE),
    ]
    assert await _screen(client, insider_cluster="false") == [
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
    ]


async def test_added_quarters_is_a_run_of_adds_back_through_the_previous_periods(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Steady adds to Alpha three quarters running. Bravo is held, then added to
    twice; Charlie is opened, then added to twice: an opening is not an add.
    In Q3, Alpha's run is two, since Q1 is the first period we have."""
    steady = await _fund(db_session, "steady")
    q3, q4 = date(2024, 9, 30), date(2024, 12, 31)
    await _quarter(db_session, steady, Q1, {ALPHA: 100, BRAVO: 100})
    await _quarter(db_session, steady, Q2, {ALPHA: 200, BRAVO: 100, CHARLIE: 100})
    await _quarter(db_session, steady, q3, {ALPHA: 300, BRAVO: 200, CHARLIE: 200})
    await _quarter(db_session, steady, q4, {ALPHA: 400, BRAVO: 300, CHARLIE: 300})
    await _publish(db_session)

    async def run(period: str, quarters: str) -> set[str]:
        body = await _get(client, "/v1/screener", period=period, added_quarters=quarters)
        return {row["cusip"] for row in body["data"]}

    assert await run("2024Q4", "3") == {ALPHA}
    assert await run("2024Q4", "2") == {ALPHA, BRAVO, CHARLIE}
    assert await run("2024Q3", "2") == {ALPHA}
    assert await run("2024Q3", "3") == set()


async def test_min_exits_is_the_stocks_that_many_investors_sold_out_of(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Activist-b alone sold out of Delta, and nobody holds it after."""
    await _two_quarters(db_session)

    assert await _screen(client, action="exit", min_exits="1") == [("activist-b", DELTA)]
    assert await _screen(client, action="exit", min_exits="2") == []
    assert await _screen(client, min_exits="1") == []


async def test_insider_buyers_counts_the_officers_and_directors_buying_in_the_quarter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two directors buy Charlie, linked to it, in Q2. One officer buys Bravo,
    found by its issuer's CIK. Alpha's buys don't count: one under a 10b5-1
    plan, one by a 10% owner, one in Q1, and one director buying twice is one."""
    await _two_quarters(db_session)
    await db_session.execute(
        update(Security)
        .where(Security.cusip == BRAVO)
        .values(ticker="BRV.A", resolution_source="manual")
    )
    await db_session.execute(
        insert(SecCompanyTicker).values(
            ticker="BRV-A", cik=BRAVO_CIK, name="BRAVO CORP", seen_at=datetime.now(UTC)
        )
    )
    ids: dict[str, int] = {
        cusip: id_ for cusip, id_ in await db_session.execute(select(Security.cusip, Security.id))
    }
    alpha_cik, charlie_cik = "0000000111", "0000000333"

    async def buy(
        cik: str, cusip: str | None, *days: date, plan: bool = False, **roles: Any
    ) -> None:
        await _form4(
            db_session,
            await _insider(db_session, f"INSIDER {next(names)}"),
            *(_line("P", day, plan=plan) for day in days),
            issuer_cik=cik,
            security_id=None if cusip is None else ids[cusip],
            **(roles or {"is_director": True}),
        )

    names = iter(range(100))
    await buy(charlie_cik, CHARLIE, date(2024, 4, 2))
    await buy(charlie_cik, CHARLIE, date(2024, 6, 30))
    await buy(BRAVO_CIK, None, date(2024, 5, 15), is_officer=True)
    await buy(alpha_cik, ALPHA, date(2024, 5, 1), plan=True)
    await buy(alpha_cik, ALPHA, date(2024, 5, 1), is_ten_percent_owner=True)
    await buy(alpha_cik, ALPHA, date(2024, 3, 29))
    await buy(alpha_cik, ALPHA, date(2024, 5, 1), date(2024, 5, 2))

    assert await _screen(client, insider_buyers="1") == [
        ("value-a", CHARLIE),
        ("value-a", BRAVO),
        ("newcomer", CHARLIE),
        ("activist-b", ALPHA),
        ("value-a", ALPHA),
    ]
    assert await _screen(client, insider_buyers="2") == [
        ("value-a", CHARLIE),
        ("newcomer", CHARLIE),
    ]
    assert await _screen(client, insider_buyers="2", action="new,add") == [("value-a", CHARLIE)]


# --- paging and sorting ---------------------------------------------------------


@pytest.mark.parametrize("sort", ["value", "weight", "weight_change", "traded"])
@pytest.mark.parametrize("order", ["desc", "asc"])
async def test_a_walk_of_pages_is_the_whole_screen_once_in_order(
    client: AsyncClient, db_session: AsyncSession, sort: str, order: str
) -> None:
    await _two_quarters(db_session)
    params = {"period": "2024Q2", "action": "new,add,hold,exit", "sort": sort, "order": order}

    whole = (await _get(client, "/v1/screener", **params))["data"]
    walked = await _walk(client, "/v1/screener", **params, limit=2)

    assert walked == whole
    assert len(whole) == 5  # newcomer's first period passes no action filter


async def test_weight_change_sorts_the_biggest_move_first_either_way(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_quarters(db_session)

    up = await _screen(client, action="new,add,hold", sort="weight_change")
    down = await _screen(client, action="new,add,hold", sort="weight_change", order="asc")

    assert up == [
        ("value-a", CHARLIE),
        ("activist-b", ALPHA),
        ("value-a", BRAVO),
        ("value-a", ALPHA),
    ]
    assert down == list(reversed(up))


async def test_traded_sorts_the_dollars_bought_or_sold_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """At $10: Charlie opened with 600 shares, Bravo added 200, Delta's 100
    sold out, and the holds traded nothing. Newcomer's first period bought
    nothing we can say, and is last."""
    await _two_quarters(db_session)

    body = await _get(
        client,
        "/v1/screener",
        period="2024Q2",
        action="new,add,hold,exit",
        sort="traded",
    )
    every = await _get(client, "/v1/screener", period="2024Q2", sort="traded")

    assert [(row["cusip"], row["traded_value_usd"]) for row in body["data"]] == [
        (CHARLIE, "6000.00"),
        (BRAVO, "2000.00"),
        (DELTA, "1000.00"),
        (ALPHA, "0.00"),
        (ALPHA, "0.00"),
    ]
    assert [(row["slug"], row["traded_value_usd"]) for row in every["data"]][-1] == (
        "newcomer",
        None,
    )


# --- refusals -------------------------------------------------------------------


async def test_a_parameter_that_is_not_a_filter_is_refused_not_ignored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Every mistake at once, each under its own name."""
    await _two_quarters(db_session)

    error = await _refused(
        client, "period=2024Q2&min_wieght=5&min_value=abc&action=new,bogus&sector=tech"
    )

    errors = _errors(error)
    assert set(errors) == {"min_wieght", "min_value", "action", "sector"}
    assert "is not a filter" in errors["min_wieght"]
    assert "not a number" in errors["min_value"]
    assert "bogus" in errors["action"]
    assert "not available yet" in errors["sector"]


@pytest.mark.parametrize(
    ("query", "key", "message"),
    [
        ("min_weight=5", "period", "Field required"),
        ("period=2024Q2&min_value=1&min_value=2", "min_value", "give min_value once"),
        ("period=2024Q2&min_holders=0", "min_holders", "out of range"),
        ("period=2024Q2&min_holders=99999999999999999999", "min_holders", "out of range"),
        ("period=2024Q2&min_value=1E%2B400", "min_value", "out of range"),
        ("period=2024Q2&min_weight=101", "min_weight", "out of range"),
        ("period=2024Q2&weight_change=0", "weight_change", "a move of 0 points"),
        ("period=2024Q2&is_new_position=yes", "is_new_position", "not true or false"),
        ("period=2024Q2&filer_category=hedge", "filer_category", "unknown category"),
        ("period=2024Q2&net_flow=up", "net_flow", "unknown direction"),
        ("period=2024Q2&filer_slug=No%20Such", "filer_slug", "not an investor's slug"),
        ("period=2024Q2&action=", "action", "no value given"),
        ("period=2024Q2&added_quarters=1", "added_quarters", "out of range: 2 to 8"),
        ("period=2024Q2&added_quarters=9", "added_quarters", "out of range: 2 to 8"),
        ("period=2024Q2&min_exits=0", "min_exits", "out of range"),
        ("period=2024Q2&insider_buyers=two", "insider_buyers", "not a whole number"),
    ],
)
async def test_a_malformed_value_is_refused_by_name(
    client: AsyncClient, query: str, key: str, message: str
) -> None:
    errors = _errors(await _refused(client, query))
    assert message in errors[key]


async def test_a_slug_that_names_no_investor_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_quarters(db_session)

    errors = _errors(await _refused(client, "period=2024Q2&filer_slug=value-a,nobody"))
    assert errors == {"filer_slug": "no investor has the slug 'nobody'"}


async def test_a_period_nothing_is_published_for_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_quarters(db_session)

    response = await client.get("/v1/screener", params={"period": "2024Q3"})

    assert response.status_code == 404
    assert "2024Q2" in response.json()["error"]["message"]


@pytest.fixture
def free(app: FastAPI) -> None:
    """Free, on 10 May 2025: 2024Q2 to 2025Q1, and trades from 10 November 2024."""
    app.dependency_overrides[entitlement_context] = lambda: EntitlementContext(
        history_quarters=4, insider_months=6, today=date(2025, 5, 10)
    )


@pytest.mark.usefixtures("free")
async def test_the_plans_window_holds_for_the_period_and_the_insiders(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """2024Q1 is before Free's history, and Q2's insider trades before its
    six months: neither is an empty screen."""
    await _two_quarters(db_session)

    assert (await client.get("/v1/screener", params={"period": "2024Q2"})).status_code == 200
    before = await client.get("/v1/screener", params={"period": "2024Q1"})
    insiders = [
        await client.get("/v1/screener", params={"period": "2024Q2", key: value})
        for key, value in (("insider_cluster", "true"), ("insider_buyers", "1"))
    ]

    assert before.status_code == 402
    assert before.json()["error"]["detail"]["limit"] == "history_quarters"
    for response in insiders:
        assert response.status_code == 402
        assert response.json()["error"]["detail"]["limit"] == "insider_months"


async def test_a_query_past_the_timeout_is_cancelled_and_a_504(db_session: AsyncSession) -> None:
    with pytest.raises(ApiError) as raised:
        async with db_session.begin_nested(), statement_timeout(db_session, 0.05):
            await db_session.execute(text("SELECT pg_sleep(1)"))

    assert raised.value.status_code == 504
    assert raised.value.code == "timeout"


# --- the vocabulary -------------------------------------------------------------


async def test_the_vocabulary_is_listed_in_order_with_what_each_filter_takes(
    client: AsyncClient,
) -> None:
    body = await _get(client, "/v1/screener/filters")

    assert [f["key"] for f in body["data"]] == [f.key for f in VOCABULARY]
    by_key = {f["key"]: f for f in body["data"]}
    assert by_key["period"]["required"] is True
    assert by_key["sector"]["available"] is False
    assert by_key["action"]["options"] == ["new", "add", "trim", "hold", "exit"]
    assert (by_key["min_weight"]["minimum"], by_key["min_weight"]["maximum"]) == ("0", "100")
    assert by_key["filer_slug"]["type"] == "slugs"
    assert body["page"] is None
