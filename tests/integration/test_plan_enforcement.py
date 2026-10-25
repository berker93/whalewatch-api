"""TIER-4, against a real Postgres: what a plan lets a request see and save.

The test that matters most is the first: a Free session and a Pro session
asking the same URL one after the other, against a warm cache, each answered
with its own plan's rows. Then each gated read in turn, as Free, for a row
before the window (none), and the 402 for asking before it; the writes'
402s; and the headers that keep a gated answer out of shared caches.

Today is 10 February 2025 throughout (``today``): the quarter being filed is
2024Q4, so Free's four quarters are 2024Q1 to 2024Q4 and its six months of
insider trades start on 10 August 2024. The fund filed every quarter of 2023
and 2024: eight, of which Free sees four.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis
from app.api.entitlements import entitlement_context
from app.db.models import Filer, Filing, Insider, InsiderTransaction, ReportingOwner, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, make_user, sign_in, subscribe

# The 13F fixtures the portfolio tests publish with: a fund, and a quarter of it.
from tests.integration.test_portfolio_endpoint import _fund, _quarter, held

TODAY = date(2025, 2, 10)
QUARTERS = [
    date(2023, 3, 31),
    date(2023, 6, 30),
    date(2023, 9, 30),
    date(2023, 12, 31),
    date(2024, 3, 31),
    date(2024, 6, 30),
    date(2024, 9, 30),
    date(2024, 12, 31),
]
FREE_FROM = date(2024, 3, 31)
FREE_TRADES_FROM = date(2024, 8, 10)
ALPHA = "11111A101"
BRAVO = "22222B202"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch, app: FastAPI, client: AsyncClient) -> None:
    """The real dependency, counting from 10 February 2025. After ``client``,
    which puts the every-period override in."""
    monkeypatch.setattr("app.api.entitlements.utc_today", lambda: TODAY)
    app.dependency_overrides.pop(entitlement_context, None)


@pytest.fixture
def cache(app: FastAPI) -> FakeRedis:
    """One Redis for every request of the test, so the second asker finds the
    first one's answer: the cache warm, as in production."""
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    return redis


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    """The fund ``alpha``, holding ALPHA in all eight quarters and BRAVO from
    the third, so each quarter after the first trades; ``bravo``, a fund that
    stopped filing in 2023; and an insider at ALPHA who bought in March 2024
    and in December."""
    alpha = await _fund(db_session, "alpha")
    accessions = {}
    for n, period in enumerate(QUARTERS):
        positions = [held(ALPHA, 100 + 10 * n)]
        if n >= 2:
            positions.append(held(BRAVO, 50 + 5 * n))
        await _quarter(db_session, alpha, period, *positions)
        accessions[period] = await db_session.scalar(
            select(Filing.accession_no).where(
                Filing.filer_id == alpha, Filing.period_of_report == period
            )
        )
    bravo = await _fund(db_session, "bravo")
    for period in QUARTERS[:3]:
        await _quarter(db_session, bravo, period, held(ALPHA, 40))
    await recompute(db_session, EVERYTHING)
    await refresh_views(db_session)

    security_id = await db_session.scalar(select(Security.id).where(Security.cusip == ALPHA))
    insider_id = await db_session.scalar(
        insert(Insider).values(cik="0000000042", name="DOE JANE").returning(Insider.id)
    )
    for n, day in enumerate((date(2024, 3, 4), date(2024, 12, 2)), start=1):
        filing_id = await db_session.scalar(
            insert(Filing)
            .values(
                accession_no=f"0000000042-24-00000{n}",
                cik="0000000099",
                form_type="4",
                filed_at=datetime(day.year, day.month, day.day + 1, 21, tzinfo=UTC),
                value_multiplier=1,
                parse_status="ok",
            )
            .returning(Filing.id)
        )
        await db_session.execute(
            insert(ReportingOwner).values(
                filing_id=filing_id, insider_id=insider_id, is_officer=True
            )
        )
        await db_session.execute(
            insert(InsiderTransaction).values(
                filing_id=filing_id,
                insider_id=insider_id,
                line_number=1,
                issuer_cik="0000000099",
                issuer_name="ISSUER ALPHA",
                security_id=security_id,
                transaction_date=day,
                transaction_code="P",
                acquired_disposed="A",
                is_derivative=False,
                shares=100,
                price_per_share=10,
            )
        )
    await db_session.flush()
    return {"accessions": accessions}


@pytest.fixture
async def pro(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    """A second browser, signed in on Pro. ``client`` is signed in on Free."""
    await make_user(db_session, "free@example.com")
    await sign_in(client, "free@example.com")
    user_id = await make_user(db_session, "pro@example.com")
    await subscribe(db_session, user_id, "pro")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as browser:
        await sign_in(browser, "pro@example.com")
        yield browser


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as browser:
        yield browser


def _periods(body: dict[str, Any]) -> list[str]:
    return [row["period"] for row in body["data"]]


# --- one URL, two plans, a warm cache -------------------------------------------------


async def test_free_and_pro_each_get_their_own_history_from_a_warm_cache(
    client: AsyncClient, pro: AsyncClient, cache: FakeRedis, world: dict[str, Any]
) -> None:
    """Pro asks first and warms the cache: the direction that would leak."""
    url = "/v1/investors/alpha/history"
    answers = [
        await pro.get(url),
        await client.get(url),
        await pro.get(url),
        await client.get(url),
    ]

    assert [r.status_code for r in answers] == [200] * 4
    assert [r.headers["x-cache"] for r in answers] == ["MISS", "MISS", "HIT", "HIT"]
    pro_periods = [p.isoformat() for p in QUARTERS]
    free_periods = [p.isoformat() for p in QUARTERS[4:]]
    assert [_periods(r.json()) for r in answers] == [
        pro_periods,
        free_periods,
        pro_periods,
        free_periods,
    ]
    assert len([key for key in cache.values if "portfolio.read_history" in key]) == 2


async def test_free_first_does_not_cut_pros_answer(
    client: AsyncClient, pro: AsyncClient, cache: FakeRedis, world: dict[str, Any]
) -> None:
    url = "/v1/investors/alpha/activity"
    free = await client.get(url)
    pro_answer = await pro.get(url)

    assert {row["period"] for row in free.json()["data"]} == {p.isoformat() for p in QUARTERS[4:]}
    assert {row["period"] for row in pro_answer.json()["data"]} == {
        p.isoformat() for p in QUARTERS[1:]
    }
    assert pro_answer.headers["x-cache"] == "MISS"


async def test_signed_out_is_free_and_shares_frees_entry(
    client: AsyncClient,
    pro: AsyncClient,
    anonymous: AsyncClient,
    cache: FakeRedis,
    world: dict[str, Any],
) -> None:
    url = "/v1/investors/alpha/history"
    await pro.get(url)
    await client.get(url)
    signed_out = await anonymous.get(url)

    assert signed_out.headers["x-cache"] == "HIT"
    assert _periods(signed_out.json()) == [p.isoformat() for p in QUARTERS[4:]]


async def test_a_period_only_pro_sees_is_a_402_for_free_even_once_pro_has_cached_it(
    client: AsyncClient, pro: AsyncClient, cache: FakeRedis, world: dict[str, Any]
) -> None:
    url = "/v1/investors/alpha/portfolio"
    params = {"period": "2023Q4"}
    warmed = await pro.get(url, params=params)
    refused = await client.get(url, params=params)

    assert warmed.status_code == 200
    assert refused.status_code == 402
    assert refused.json()["error"]["code"] == "plan_limit"
    assert refused.json()["error"]["detail"] == {
        "limit": "history_quarters",
        "allowed": 4,
        # 2023Q4 to 2024Q4, the quarter being filed.
        "needed": 5,
        "plan": {
            "slug": "pro",
            "name": "Pro",
            "price_cents": 1400,
            "currency": "USD",
            "interval": "month",
            "value": 20,
        },
    }


# --- every gated read, as Free ------------------------------------------------------


async def test_the_portfolio_defaults_inside_the_window_and_first_period_starts_at_it(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    free = (await client.get("/v1/investors/alpha/portfolio")).json()
    paid = (await pro.get("/v1/investors/alpha/portfolio")).json()

    assert free["meta"]["period_end"] == paid["meta"]["period_end"] == "2024-12-31"
    first = {row["cusip"]: row["first_period"] for row in free["data"]}
    assert first == {ALPHA: "2024-03-31", BRAVO: "2024-03-31"}
    assert {row["cusip"]: row["first_period"] for row in paid["data"]} == {
        ALPHA: "2023-03-31",
        BRAVO: "2023-09-30",
    }


async def test_activity_before_the_window_is_a_402(
    client: AsyncClient, world: dict[str, Any]
) -> None:
    for params in ({"from": "2023Q4"}, {"to": "2023Q4"}):
        response = await client.get("/v1/investors/alpha/activity", params=params)
        assert response.status_code == 402, params


async def test_the_investor_list_and_detail_stop_at_the_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    rows = {row["slug"]: row for row in (await client.get("/v1/investors")).json()["data"]}

    # bravo's every period is older: published nothing Free can see.
    assert rows["bravo"]["latest_period"] is None
    assert rows["bravo"]["sparkline"] == []
    # Eight quarters ending at 2024Q4, the four before the window empty.
    assert rows["alpha"]["sparkline"][:4] == [None] * 4
    assert all(value is not None for value in rows["alpha"]["sparkline"][4:])

    detail = (await client.get("/v1/investors/alpha")).json()
    assert detail["first_period"] == "2024-03-31"
    assert (await pro.get("/v1/investors/alpha")).json()["first_period"] == "2023-03-31"
    paid = {row["slug"]: row for row in (await pro.get("/v1/investors")).json()["data"]}
    assert paid["bravo"]["latest_period"] == "2023-09-30"


async def test_a_portfolio_wholly_before_the_window_is_a_402_not_a_404(
    client: AsyncClient, world: dict[str, Any]
) -> None:
    response = await client.get("/v1/investors/bravo/portfolio")

    assert response.status_code == 402
    assert response.json()["error"]["detail"]["limit"] == "history_quarters"


async def test_a_stocks_history_prices_and_owners_stop_at_the_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    for path in (
        f"/v1/stocks/{ALPHA}/ownership-history",
        f"/v1/stocks/{ALPHA}/implied-prices",
        f"/v1/stocks/{ALPHA}/splits",
    ):
        periods = _periods((await client.get(path)).json())
        assert all(period >= FREE_FROM.isoformat() for period in periods), path

    history = (await client.get(f"/v1/stocks/{ALPHA}/ownership-history")).json()["data"]
    assert [point["period"] for point in history] == [p.isoformat() for p in QUARTERS[4:]]
    # bravo held ALPHA only before the window: not one of its holders in it.
    assert {h["slug"] for h in history[0]["top_holders"]} == {"alpha"}
    paid = (await pro.get(f"/v1/stocks/{ALPHA}/ownership-history")).json()["data"]
    assert len(paid) == 8

    owners = await client.get(f"/v1/stocks/{ALPHA}/owners", params={"period": "2023Q3"})
    assert owners.status_code == 402
    assert (await pro.get(f"/v1/stocks/{ALPHA}/owners", params={"period": "2023Q3"})).json()["data"]


async def test_market_lists_need_their_quarters_inside_the_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    assert (
        await client.get("/v1/market/top-holdings", params={"period": "2023Q4"})
    ).status_code == 402
    assert (
        await pro.get("/v1/market/top-holdings", params={"period": "2023Q4"})
    ).status_code == 200

    # A year to 2024Q3 is 2023Q4 to 2024Q3: one quarter outside.
    year = {"period_type": "year", "period": "2024Q3"}
    for path in ("/v1/market/top-buys", "/v1/flows"):
        assert (await client.get(path, params=year)).status_code == 402, path
        assert (await pro.get(path, params=year)).status_code == 200, path
    latest_year = await client.get("/v1/flows", params={"period_type": "year"})
    assert latest_year.status_code == 200
    assert latest_year.json()["meta"]["quarters"] == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]


async def test_the_filing_feed_has_no_filing_before_the_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    free = (await client.get("/v1/market/activity", params={"limit": 200})).json()["data"]
    paid = (await pro.get("/v1/market/activity", params={"limit": 200})).json()["data"]

    assert free and all(row["period"] >= FREE_FROM.isoformat() for row in free)
    assert len(paid) == len(QUARTERS) + 3
    assert len(free) == 4


async def test_a_filing_before_the_window_is_a_402_before_its_holdings_are_read(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    old = world["accessions"][date(2023, 12, 31)]
    new = world["accessions"][date(2024, 12, 31)]

    assert (await client.get(f"/filings/{old}")).status_code == 402
    assert (await client.get(f"/filings/{new}")).status_code == 200
    assert (await pro.get(f"/filings/{old}")).json()["holdings"]


async def test_insider_trades_stop_at_the_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    for path in ("/v1/insiders/transactions", f"/v1/stocks/{ALPHA}/insiders"):
        free = (await client.get(path)).json()["data"]
        paid = (await pro.get(path)).json()["data"]
        assert [row["transaction_date"] for row in free] == ["2024-12-02"], path
        assert [row["transaction_date"] for row in paid] == ["2024-12-02", "2024-03-04"], path

    for params in ({"from": "2024-01-01"}, {"to": "2024-08-09"}, {"filed_from": "2024-03-01"}):
        response = await client.get("/v1/insiders/transactions", params=params)
        assert response.status_code == 402, params
        assert response.json()["error"]["detail"]["limit"] == "insider_months"
    assert (
        await client.get("/v1/insiders/clusters", params={"from": "2024-01-01"})
    ).status_code == 402
    assert (
        await client.get("/v1/insiders/transactions", params={"from": "2024-08-10"})
    ).status_code == 200


async def test_the_timeline_has_nothing_from_before_either_window(
    client: AsyncClient, pro: AsyncClient, world: dict[str, Any]
) -> None:
    """2023Q4's 13F was filed on 14 February 2024, and the March trade on the
    5th: both in a window of days Free may ask for, neither in its plan's."""
    params = {
        "ref": ["investor:alpha", f"stock:{ALPHA}", "insider:0000000042"],
        "from": "2024-01-20",
        "to": "2024-03-31",
        "tz": "UTC",
    }
    free = (await client.get("/v1/timeline", params=params)).json()["data"]
    paid = (await pro.get("/v1/timeline", params=params)).json()["data"]

    assert free == []
    assert {event["kind"] for event in paid} >= {"insider_trade", "filing_summary"}


# --- the writes -----------------------------------------------------------------------


async def test_a_full_watchlist_is_a_402_naming_the_plan_that_keeps_more(
    client: AsyncClient, pro: AsyncClient, db_session: AsyncSession
) -> None:
    slugs = [f"filer-{i:02}" for i in range(11)]
    await db_session.execute(insert(Filer), [{"name": s.upper(), "slug": s} for s in slugs])
    responses = [
        await client.put(
            "/v1/watchlist/items", json={"ref": {"kind": "investor", "slug": s}}, headers=FRONTEND
        )
        for s in slugs
    ]

    assert [r.status_code for r in responses] == [200] * 10 + [402]
    error = responses[-1].json()["error"]
    assert error["code"] == "plan_limit"
    assert error["detail"]["limit"] == "watchlist_items"
    assert (error["detail"]["allowed"], error["detail"]["needed"]) == (10, 11)
    assert error["detail"]["plan"]["slug"] == "pro"
    assert error["detail"]["plan"]["value"] is None
    # Following one already followed is no change, full or not.
    again = await client.put(
        "/v1/watchlist/items",
        json={"ref": {"kind": "investor", "slug": slugs[0]}},
        headers=FRONTEND,
    )
    assert again.status_code == 200


async def test_free_saves_three_filters_and_the_fourth_is_a_402(
    client: AsyncClient, pro: AsyncClient
) -> None:
    def save(browser: AsyncClient, n: int) -> Any:
        return browser.post(
            "/v1/saved-filters",
            json={"name": f" Filter {n} ", "query": {"direction": "buy", "period": "2024-12-31"}},
            headers=FRONTEND,
        )

    statuses = [(await save(client, n)).status_code for n in range(4)]
    assert statuses == [201, 201, 201, 402]
    refused = (await save(client, 4)).json()["error"]["detail"]
    assert (refused["limit"], refused["allowed"], refused["needed"]) == (
        "screener_saved_filters",
        3,
        4,
    )
    assert [(await save(pro, n)).status_code for n in range(4)] == [201] * 4

    saved = (await client.get("/v1/saved-filters")).json()["data"]
    assert [f["name"] for f in saved] == ["Filter 0", "Filter 1", "Filter 2"]
    assert saved[0]["query"] == {"direction": "buy", "period": "2024Q4"}

    deleted = await client.delete(f"/v1/saved-filters/{saved[0]['id']}", headers=FRONTEND)
    assert deleted.status_code == 204
    assert (await save(client, 5)).status_code == 201
    # Another user's filter is no change.
    others = (await pro.get("/v1/saved-filters")).json()["data"]
    await client.delete(f"/v1/saved-filters/{others[0]['id']}", headers=FRONTEND)
    assert len((await pro.get("/v1/saved-filters")).json()["data"]) == 4


async def test_a_filter_the_screener_could_not_apply_is_a_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)
    for query in ({"sector": "Energy"}, {"sort": "loudest"}, {"period": "2024-12-30"}):
        response = await client.post(
            "/v1/saved-filters", json={"name": "x", "query": query}, headers=FRONTEND
        )
        assert response.status_code == 422, query


async def test_an_unverified_account_saves_no_filter(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, verified=False)
    await sign_in(client)

    response = await client.post(
        "/v1/saved-filters", json={"name": "x", "query": {}}, headers=FRONTEND
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_unverified"


# --- headers ----------------------------------------------------------------------------


async def test_every_gated_answer_is_private_and_varies_by_cookie(
    client: AsyncClient, world: dict[str, Any]
) -> None:
    """The cached, the uncached, and the refused alike: none may sit in a CDN."""
    for path, params in (
        ("/v1/investors/alpha/history", {}),
        ("/v1/investors/alpha/portfolio", {"period": "2023Q1"}),
        ("/v1/timeline", {"from": "2024-12-01", "to": "2024-12-31", "ref": "investor:alpha"}),
        (f"/filings/{world['accessions'][date(2024, 12, 31)]}", {}),
    ):
        response = await client.get(path, params=params)
        assert "private" in response.headers["cache-control"], path
        assert "public" not in response.headers["cache-control"], path
        assert "Cookie" in response.headers["vary"], path


async def test_an_ungated_answer_stays_public(client: AsyncClient, world: dict[str, Any]) -> None:
    response = await client.get("/v1/search", params={"q": "alpha"})

    assert response.headers["cache-control"].startswith("public, ")
