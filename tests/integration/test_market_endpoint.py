"""``/v1/market/*`` and ``/v1/flows``, against a real Postgres.

Every test publishes the way an operator does, ``recompute`` then
``refresh-views``, and most read one made-up year (:func:`_a_year`) small
enough to work out by hand. The cases that matter are the ones a wrong reading
would get wrong without failing: a position bought and sold within the year,
which a net-only "top buys" leaves out; a filer buying in two quarters, counted
as two buyers; a filer's first period counted as buying; a stock sold least
ranked among the buys; a withheld filing in the feed; and a page walk that
repeats or drops a row.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filer, FilerCik, Filing, Holding, MatviewRefresh, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
ALPHA = "11111A101"  # widely held, and small
BRAVO = "22222B202"  # one holder, and large
CHARLIE = "33333C303"  # bought in Q2, sold out of in Q4
DELTA = "44444D404"  # added to every quarter
ECHO = "55555E505"  # a newcomer's, in its first period

_ciks = count(1)
_accessions = count(1)


async def _fund(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=0)
    )
    return filer_id


async def _quarter(
    session: AsyncSession,
    filer_id: int,
    period: date,
    positions: dict[str, int],
    *,
    suspect: bool = False,
) -> str:
    """A 13F-HR for ``period`` holding ``positions`` (CUSIP: shares) at $10 a
    share, filed 45 days after it, as loaded. Its accession number."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    accession_no = f"{cik}-{period:%y}-{next(_accessions):06d}"
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=accession_no,
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR",
            period_of_report=period,
            filed_at=datetime(period.year, period.month, period.day, 16, tzinfo=UTC)
            + timedelta(days=45),
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
    for cusip, shares in positions.items():
        await session.execute(
            pg_insert(Security)
            .values(cusip=cusip, name=f"ISSUER {cusip}")
            .on_conflict_do_nothing(index_elements=[Security.cusip])
        )
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id).where(Security.cusip == cusip).scalar_subquery(),
                filer_id=filer_id,
                period_of_report=period,
                cusip=cusip,
                value_usd=Decimal(shares) * 10,
                shares=Decimal(shares),
                sshprnamt_type="SH",
            )
        )
    return accession_no


async def _publish(session: AsyncSession) -> None:
    """``recompute --all``, then ``refresh-views``."""
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _a_year(session: AsyncSession) -> None:
    """2024, at $10 a share throughout, so every figure is shares times ten.

    Alpha: ten shares each at three funds, all year. Bravo: 10,000 at one.
    Charlie: 1,000 bought by ``trader`` in Q2 and sold out of in Q4. Delta:
    ``builder`` adds 100 shares every quarter. Echo: 50 held by ``newcomer``
    from Q3, its first period, which is not a purchase.
    """
    trader, builder, holder, whale, newcomer = [
        await _fund(session, slug) for slug in ("trader", "builder", "holder", "whale", "newcomer")
    ]
    charlie = {Q1: 0, Q2: 1_000, Q3: 1_000, Q4: 0}
    delta = {Q1: 100, Q2: 200, Q3: 300, Q4: 400}
    for period in (Q1, Q2, Q3, Q4):
        trader_book = {ALPHA: 10} | ({CHARLIE: charlie[period]} if charlie[period] else {})
        await _quarter(session, trader, period, trader_book)
        await _quarter(session, builder, period, {ALPHA: 10, DELTA: delta[period]})
        await _quarter(session, holder, period, {ALPHA: 10})
        await _quarter(session, whale, period, {BRAVO: 10_000})
    await _quarter(session, newcomer, Q3, {ECHO: 50})
    await _quarter(session, newcomer, Q4, {ECHO: 50})
    await _publish(session)


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _walk(client: AsyncClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Every page, start to end; the rows in the order they came."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        body = await _get(client, path, **params, **({"cursor": cursor} if cursor else {}))
        rows += body["data"]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return rows


def _cusips(body: Any) -> list[str]:
    return [row["cusip"] for row in body["data"]]


# --- top holdings ---------------------------------------------------------------


async def test_top_holdings_by_holders_and_by_value_are_two_lists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Alpha is held by the most funds and Bravo for the most dollars. Charlie,
    sold out of, is not held at Q4 at all."""
    await _a_year(db_session)

    by_holders = await _get(client, "/v1/market/top-holdings")
    by_value = await _get(client, "/v1/market/top-holdings", metric="value", limit=2)

    assert _cusips(by_holders) == [ALPHA, BRAVO, DELTA, ECHO]
    assert _cusips(by_value) == [BRAVO, DELTA]
    alpha = by_holders["data"][0]
    assert (alpha["holder_count"], alpha["total_value_usd"], alpha["value_rank"]) == (
        3,
        "300.00",
        4,
    )
    assert alpha["sector"] is None
    assert by_holders["meta"]["period"] == "2024Q4"
    assert by_holders["meta"]["coverage"] == {"filers_reported": 5, "filers_tracked": 5}
    assert by_holders["meta"]["refreshed_at"] is not None
    assert by_holders["page"] is None


async def test_a_period_is_named_either_way_and_one_not_published_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_year(db_session)

    for period in ("2024Q2", "2024-06-30"):
        body = await _get(client, "/v1/market/top-holdings", period=period)
        assert body["meta"]["period"] == "2024Q2"
        assert ECHO not in _cusips(body)

    response = await client.get("/v1/market/top-holdings", params={"period": "2025Q1"})
    assert response.status_code == 404
    assert "2024Q4" in response.json()["error"]["message"]
    response = await client.get("/v1/market/top-holdings", params={"period": "2024-06-29"})
    assert response.status_code == 422


async def test_a_top_list_is_refused_above_its_limit(client: AsyncClient) -> None:
    response = await client.get("/v1/market/top-holdings", params={"limit": 101})
    assert response.status_code == 422


async def test_nothing_published_is_a_404(client: AsyncClient) -> None:
    for path in ("/v1/market/top-holdings", "/v1/market/top-buys", "/v1/flows"):
        response = await client.get(path)
        assert response.status_code == 404, path


# --- top buys and sells ---------------------------------------------------------


async def test_the_years_top_buys_by_gross_value_keep_a_position_sold_again(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The ticket's case. Charlie was bought for $10,000 and sold for the
    same within the year: the year's largest accumulation, and a net of
    nothing. Ranked by net value it is not a buy at all."""
    await _a_year(db_session)

    gross = await _get(client, "/v1/market/top-buys", period="2024Q4", period_type="year")
    net = await _get(
        client, "/v1/market/top-buys", period="2024Q4", period_type="year", metric="net_value"
    )

    assert _cusips(gross) == [CHARLIE, DELTA]
    assert _cusips(net) == [DELTA]
    charlie = gross["data"][0]
    assert (charlie["gross_bought_usd"], charlie["gross_sold_usd"], charlie["net_value_usd"]) == (
        "10000.00",
        "10000.00",
        "0.00",
    )
    assert gross["meta"]["period"] == "2024Q4"
    assert gross["meta"]["quarters"] == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]


async def test_a_filer_adding_every_quarter_is_one_buyer_of_the_year_and_three_of_the_quarters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Delta's builder bought in Q2, Q3 and Q4 (Q1 is its first period). The
    year's dollars are the three quarters' added up; its buyers are one."""
    await _a_year(db_session)

    year = await _get(client, "/v1/market/top-buys", period_type="year", metric="holders")
    quarters = [
        (await _get(client, "/v1/market/top-buys", period=period))["data"]
        for period in ("2024Q2", "2024Q3", "2024Q4")
    ]

    [delta] = [row for row in year["data"] if row["cusip"] == DELTA]
    assert (delta["buyer_count"], delta["gross_bought_usd"], delta["net_shares"]) == (
        1,
        "3000.00",
        "300.0000",
    )
    assert (
        sum(row["buyer_count"] for rows in quarters for row in rows if row["cusip"] == DELTA) == 3
    )


async def test_the_quarters_top_buys_and_sells(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q4: Delta bought, Charlie sold out of. Neither list carries a stock that
    only went the other way, and Alpha and Bravo, held still, are in neither."""
    await _a_year(db_session)

    buys = await _get(client, "/v1/market/top-buys")
    sells = await _get(client, "/v1/market/top-sells")
    net_sells = await _get(client, "/v1/market/top-sells", metric="net_value")
    sellers = await _get(client, "/v1/market/top-sells", metric="holders")

    assert _cusips(buys) == [DELTA]
    assert _cusips(sells) == _cusips(net_sells) == _cusips(sellers) == [CHARLIE]
    charlie = sells["data"][0]
    assert (charlie["gross_sold_usd"], charlie["net_value_usd"], charlie["exits"]) == (
        "10000.00",
        "-10000.00",
        1,
    )
    assert (charlie["holder_count"], charlie["total_value_usd"]) == (0, "0.00")
    assert buys["meta"]["quarters"] is None


async def test_net_sells_rank_the_most_sold_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    a, b = await _fund(db_session, "a"), await _fund(db_session, "b")
    await _quarter(db_session, a, Q1, {ALPHA: 100, BRAVO: 100})
    await _quarter(db_session, b, Q1, {ALPHA: 100})
    await _quarter(db_session, a, Q2, {ALPHA: 90, BRAVO: 40})
    await _quarter(db_session, b, Q2, {ALPHA: 150})
    await _publish(db_session)

    sells = await _get(client, "/v1/market/top-sells", metric="net_value")
    buys = await _get(client, "/v1/market/top-buys", metric="net_value")

    # Bravo: $600 sold. Alpha: $100 sold and $500 bought, so a net buy.
    assert [(row["cusip"], row["net_value_usd"]) for row in sells["data"]] == [(BRAVO, "-600.00")]
    assert [(row["cusip"], row["net_value_usd"]) for row in buys["data"]] == [(ALPHA, "400.00")]


# --- new positions --------------------------------------------------------------


async def test_new_positions_leave_out_a_filers_first_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q2: the trader opened Charlie. Q3: the newcomer's Echo is its first
    period, which is where its history starts, not a purchase."""
    await _a_year(db_session)

    q2 = await _get(client, "/v1/market/new-positions", period="2024Q2")
    q3 = await _get(client, "/v1/market/new-positions", period="2024Q3")
    year = await _get(client, "/v1/market/new-positions", period_type="year")

    assert [(row["cusip"], row["new_positions"]) for row in q2["data"]] == [(CHARLIE, 1)]
    assert q3["data"] == []
    assert _cusips(year) == [CHARLIE]


# --- the feed -------------------------------------------------------------------


async def test_the_feed_is_every_published_filing_newest_first_each_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_year(db_session)

    rows = await _walk(client, "/v1/market/activity", limit=3)

    # Four funds' four quarters, and the newcomer's two.
    assert len(rows) == len({row["accession_no"] for row in rows}) == 18
    filed = [row["filed_at"] for row in rows]
    assert filed == sorted(filed, reverse=True)
    assert {row["period"] for row in rows[:5]} == {"2024-12-31"}


async def test_a_feed_row_carries_its_periods_size_and_largest_trade(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The trader's Q4 sold out of Charlie, its largest trade, and left one
    position. Its Q1 is its first period: no largest trade."""
    await _a_year(db_session)

    rows = await _walk(client, "/v1/market/activity", limit=50)

    trader = {row["period"]: row for row in rows if row["slug"] == "trader"}
    q4 = trader["2024-12-31"]
    assert (q4["form_type"], q4["position_count"], q4["display_name"]) == ("13F-HR", 1, "TRADER")
    assert q4["largest_change"] == {
        "cusip": CHARLIE,
        "ticker": None,
        "issuer_name": f"ISSUER {CHARLIE}",
        "action": "exit",
        "shares_delta": "-1000.0000",
        "traded_value_usd": "10000.00",
    }
    assert trader["2024-03-31"]["largest_change"] is None
    # Nothing traded at all in the holder's Q2: no largest trade either.
    [holder_q2] = [r for r in rows if r["slug"] == "holder" and r["period"] == "2024-06-30"]
    assert holder_q2["largest_change"] is None


async def test_a_filing_withheld_as_suspect_is_not_in_the_feed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    fund = await _fund(db_session, "fund")
    kept = await _quarter(db_session, fund, Q1, {ALPHA: 10})
    await _quarter(db_session, fund, Q2, {ALPHA: 20}, suspect=True)
    await _publish(db_session)

    body = await _get(client, "/v1/market/activity")

    assert [row["accession_no"] for row in body["data"]] == [kept]


async def test_the_feed_is_as_of_the_last_refresh_and_says_when_that_was(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    fund = await _fund(db_session, "fund")
    await _quarter(db_session, fund, Q1, {ALPHA: 10})
    await _publish(db_session)
    await _quarter(db_session, fund, Q2, {ALPHA: 20})
    await recompute(db_session, EVERYTHING)

    body = await _get(client, "/v1/market/activity")

    refreshed = await db_session.scalar(
        select(MatviewRefresh.refreshed_at).where(MatviewRefresh.view_name == "mv_filing_feed")
    )
    assert refreshed is not None
    assert [row["period"] for row in body["data"]] == ["2024-03-31"]
    assert datetime.fromisoformat(body["meta"]["refreshed_at"]) == refreshed
    assert body["meta"]["period"] is None


# --- the screener ---------------------------------------------------------------


async def test_the_screener_walks_every_stock_once_in_order(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q4: every stock changed, held on through, or exited, in net value
    order, ties to the larger holding: Delta, then the three held still,
    largest first, then Charlie, sold."""
    await _a_year(db_session)

    rows = await _walk(client, "/v1/flows", limit=2)

    assert [row["cusip"] for row in rows] == [DELTA, BRAVO, ECHO, ALPHA, CHARLIE]
    ascending = await _walk(client, "/v1/flows", limit=2, order="asc")
    assert [row["cusip"] for row in ascending] == [CHARLIE, ALPHA, ECHO, BRAVO, DELTA]


@pytest.mark.parametrize(
    ("params", "cusips"),
    [
        ({"direction": "buy"}, [DELTA]),
        ({"direction": "sell"}, [CHARLIE]),
        ({"min_investors": 2}, [ALPHA]),
        ({"min_investors": 1, "min_value": "1000"}, [DELTA, BRAVO]),
        ({"sort": "holders"}, [ALPHA, BRAVO, DELTA, ECHO, CHARLIE]),
        ({"sort": "gross_sold"}, [CHARLIE, BRAVO, DELTA, ECHO, ALPHA]),
        ({"sort": "value_held"}, [BRAVO, DELTA, ECHO, ALPHA, CHARLIE]),
        ({"period_type": "year", "sort": "gross_bought"}, [CHARLIE, DELTA, BRAVO, ECHO, ALPHA]),
        ({"period_type": "year", "sort": "buyers", "direction": "buy"}, [DELTA]),
    ],
)
async def test_the_screener_filters_and_sorts(
    client: AsyncClient,
    db_session: AsyncSession,
    params: dict[str, Any],
    cusips: list[str],
) -> None:
    await _a_year(db_session)

    rows = await _walk(client, "/v1/flows", limit=2, **params)

    assert [row["cusip"] for row in rows] == cusips


async def test_a_cursor_from_one_sort_is_refused_by_another(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_year(db_session)

    first = await _get(client, "/v1/flows", limit=1)
    response = await client.get(
        "/v1/flows", params={"limit": 1, "sort": "holders", "cursor": first["page"]["next_cursor"]}
    )

    assert response.status_code == 400


async def test_a_sector_is_a_422_saying_there_are_none_yet(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_year(db_session)

    response = await client.get("/v1/flows", params={"sector": "Information Technology"})

    assert response.status_code == 422
    body = response.json()["error"]
    assert "sector" in body["message"]
    # Shaped as a malformed parameter is, so a client handles both alike.
    assert (body["code"], body["detail"]["errors"][0]["loc"]) == (
        "validation_error",
        ["query", "sector"],
    )
    assert all(row["sector"] is None for row in (await _get(client, "/v1/flows"))["data"])


async def test_a_year_with_no_flows_is_a_404_naming_the_latest(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q1 is every fund's first period, so no year ends there."""
    await _a_year(db_session)

    response = await client.get("/v1/flows", params={"period": "2024Q1", "period_type": "year"})

    assert response.status_code == 404
    assert "2024Q4" in response.json()["error"]["message"]
