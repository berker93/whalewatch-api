"""``/v1/stocks/{ticker}``, ``/owners`` and ``/ownership-history``, against a real Postgres.

Every test publishes the way an operator does, ``recompute`` then
``refresh-views``. The cases that matter are the ones where the wrong security
or the wrong figure could be served: a ticker that matches two securities, an
alias that shadows a ticker, a CUSIP-length string that is not a CUSIP, a
filer's first period counted as buying, an exit listed as an owner, a top-five
holder that changes from one quarter to the next, a gap drawn as a zero.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from httpx import AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filer, FilerCik, Filing, Holding, Security, SecurityAlias
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"

_ciks = count(1)
_accessions = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal
    put_call: str | None


def held(cusip: str, shares: int, *, put_call: str | None = None) -> _Held:
    return _Held(cusip, Decimal(shares), put_call)


async def _fund(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=0)
    )
    return filer_id


async def _quarter(session: AsyncSession, filer_id: int, period: date, *positions: _Held) -> None:
    """A 13F-HR for ``period`` holding ``positions`` at $10 a share, as loaded."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-{period:%y}-{next(_accessions):06d}",
            cik=cik,
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
    for position in positions:
        await _security(session, position.cusip)
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id)
                .where(Security.cusip == position.cusip)
                .scalar_subquery(),
                filer_id=filer_id,
                period_of_report=period,
                cusip=position.cusip,
                value_usd=position.shares * 10,
                shares=position.shares,
                sshprnamt_type="SH",
                put_call=position.put_call,
            )
        )


async def _security(
    session: AsyncSession, cusip: str, *, name: str | None = None, ticker: str | None = None
) -> int:
    await session.execute(
        pg_insert(Security)
        .values(cusip=cusip, name=name or f"ISSUER {cusip}")
        .on_conflict_do_nothing(index_elements=[Security.cusip])
    )
    if ticker is not None:
        await session.execute(update(Security).where(Security.cusip == cusip).values(ticker=ticker))
    security_id = await session.scalar(select(Security.id).where(Security.cusip == cusip))
    assert security_id is not None
    return security_id


async def _alias(session: AsyncSession, cusip: str, alias: str) -> None:
    await session.execute(
        insert(SecurityAlias).values(
            security_id=await _security(session, cusip), alias=alias, source="manual"
        )
    )


async def _publish(session: AsyncSession) -> None:
    """``recompute --all``, then ``refresh-views``."""
    await recompute(session, EVERYTHING)
    await refresh_views(session)


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


async def _a_quarter_of_trading(session: AsyncSession) -> None:
    """ALPHA, from Q1 to Q2: added to, exited, held, opened, and held by a
    filer in its first period, which is not a trade."""
    adder, exiter, holder, opener, newcomer = [
        await _fund(session, slug) for slug in ("adder", "exiter", "holder", "opener", "newcomer")
    ]
    await _quarter(session, adder, Q1, held(ALPHA, 100), held(BRAVO, 10))
    await _quarter(session, exiter, Q1, held(ALPHA, 50), held(BRAVO, 10))
    await _quarter(session, holder, Q1, held(ALPHA, 30), held(BRAVO, 10))
    await _quarter(session, opener, Q1, held(BRAVO, 10))
    await _quarter(session, adder, Q2, held(ALPHA, 150), held(BRAVO, 10))
    await _quarter(session, exiter, Q2, held(BRAVO, 10))
    await _quarter(session, holder, Q2, held(ALPHA, 30), held(BRAVO, 10))
    await _quarter(session, opener, Q2, held(ALPHA, 20), held(BRAVO, 10))
    await _quarter(session, newcomer, Q2, held(ALPHA, 40))
    await _publish(session)


# --- finding the stock ----------------------------------------------------------


async def test_a_ticker_is_found_whatever_its_case(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _security(db_session, ALPHA, ticker="ALF")
    await _publish(db_session)

    for ticker in ("ALF", "alf", "Alf"):
        body = await _get(client, f"/v1/stocks/{ticker}")
        assert (body["cusip"], body["ticker"]) == (ALPHA, "ALF"), ticker


async def test_an_alias_finds_its_security_but_never_shadows_a_ticker(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 10))
    await _security(db_session, ALPHA, ticker="META")
    await _alias(db_session, ALPHA, "fb")
    # BRAVO once traded as META too. The ticker ALPHA holds now wins.
    await _alias(db_session, BRAVO, "META")
    await _publish(db_session)

    assert (await _get(client, "/v1/stocks/FB"))["cusip"] == ALPHA
    assert (await _get(client, "/v1/stocks/meta"))["cusip"] == ALPHA


async def test_a_recycled_ticker_names_the_security_held_most_recently(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    # ALPHA delisted after Q1, and BRAVO took its ticker. Inserted in that
    # order and the other, so it is not the newer row that decides.
    await _quarter(db_session, filer, Q1, held(BRAVO, 5), held(ALPHA, 100))
    await _quarter(db_session, filer, Q2, held(BRAVO, 10))
    await _security(db_session, ALPHA, ticker="RCY")
    await _security(db_session, BRAVO, ticker="RCY")
    await _publish(db_session)

    assert (await _get(client, "/v1/stocks/RCY"))["cusip"] == BRAVO


async def test_a_cusip_finds_a_security_with_no_ticker_whatever_its_case(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)

    for key in (ALPHA, ALPHA.lower()):
        body = await _get(client, f"/v1/stocks/{key}")
        assert (body["cusip"], body["ticker"], body["issuer_name"]) == (
            ALPHA,
            None,
            f"ISSUER {ALPHA}",
        )
    # A CUSIP and then some is not that CUSIP: never compared on its first nine.
    response = await client.get(f"/v1/stocks/{ALPHA}X")
    assert response.status_code == 404


async def test_an_unknown_ticker_is_a_404_suggesting_tickers_and_names_like_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    small, large = await _fund(db_session, "small"), await _fund(db_session, "large")
    await _security(db_session, ALPHA, name="ACME WIDGETS INC")
    await _security(db_session, BRAVO, name="ACME ROCKETS CORP")
    await _security(db_session, CHARLIE, name="UNRELATED HOLDINGS", ticker="ACMEX")
    await _quarter(db_session, small, Q1, held(ALPHA, 1))
    await _quarter(db_session, large, Q1, held(BRAVO, 1000), held(CHARLIE, 1))
    await _publish(db_session)

    response = await client.get("/v1/stocks/acme")

    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "not_found"
    assert "acme" in body["message"]
    # A ticker starting with it first, then names alike, the most held first.
    assert [s["cusip"] for s in body["detail"]["suggestions"]] == [CHARLIE, BRAVO, ALPHA]
    assert body["detail"]["suggestions"][0] == {
        "cusip": CHARLIE,
        "ticker": "ACMEX",
        "issuer_name": "UNRELATED HOLDINGS",
    }


async def test_a_404_with_nothing_like_it_suggests_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    for path in (
        "/v1/stocks/ZZZZ",
        "/v1/stocks/ZZZZ/owners",
        "/v1/stocks/ZZZZ/ownership-history",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["detail"]["suggestions"] == [], path


# --- the detail -----------------------------------------------------------------


async def test_the_detail_is_the_latest_period_held_and_traded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_quarter_of_trading(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}")

    assert body == {
        "cusip": ALPHA,
        "ticker": None,
        "issuer_name": f"ISSUER {ALPHA}",
        "sector": None,
        "period": "2024-06-30",
        "coverage": {"filers_reported": 5, "filers_tracked": 5},
        # adder 150, holder 30, opener 20, newcomer 40.
        "holder_count": 4,
        "total_shares": "240.0000",
        "total_value_usd": "2400.00",
        # +50 added, -50 exited, +20 opened. The newcomer's 40 is its first
        # period, not a purchase.
        "net_shares": "20.0000",
        "net_value_usd": "200.00",
        "new_positions": 1,
        "exits": 1,
    }


async def test_the_detail_of_a_stock_nobody_holds_now_is_zeros_for_the_latest_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 10))
    await _quarter(db_session, filer, Q2, held(BRAVO, 10))
    await _publish(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}")

    assert body["period"] == "2024-06-30"
    assert (body["holder_count"], body["total_shares"], body["total_value_usd"]) == (
        0,
        "0.0000",
        "0.00",
    )
    assert (body["net_shares"], body["net_value_usd"], body["exits"]) == (
        "-100.0000",
        "-1000.00",
        1,
    )


async def test_the_detail_with_nothing_published_names_no_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _security(db_session, ALPHA)

    body = await _get(client, f"/v1/stocks/{ALPHA}")

    assert body["period"] is None
    assert body["coverage"] is None
    assert body["holder_count"] is None


async def test_an_option_only_security_is_found_and_held_by_nobody(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fund")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 10, put_call="Call"))
    await _publish(db_session)

    assert (await _get(client, f"/v1/stocks/{BRAVO}"))["holder_count"] == 0
    assert (await _get(client, f"/v1/stocks/{BRAVO}/owners"))["data"] == []
    assert (await _get(client, f"/v1/stocks/{BRAVO}/ownership-history"))["data"] == []


# --- the owners -----------------------------------------------------------------


async def test_owners_are_the_latest_periods_holders_largest_first_with_their_change(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_quarter_of_trading(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}/owners")

    # The exiter is not an owner.
    assert [(r["slug"], r["shares"], r["action"]) for r in body["data"]] == [
        ("adder", "150.0000", "add"),
        ("newcomer", "40.0000", "new"),
        ("holder", "30.0000", "hold"),
        ("opener", "20.0000", "new"),
    ]
    assert body["data"][0] == {
        "slug": "adder",
        "display_name": "ADDER",
        "shares": "150.0000",
        "value_usd": "1500.00",
        "weight_pct": "93.750000",
        "shares_delta": "50.0000",
        "shares_delta_pct": "50.000000",
        "weight_delta": "2.840909",
        "action": "add",
    }
    assert body["data"][1]["shares_delta_pct"] is None
    assert body["meta"]["period"] == "2024Q2"
    assert body["meta"]["coverage"] == {"filers_reported": 5, "filers_tracked": 5}


async def test_owners_for_an_earlier_period(client: AsyncClient, db_session: AsyncSession) -> None:
    await _a_quarter_of_trading(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}/owners", period="2024Q1")

    assert [r["slug"] for r in body["data"]] == ["adder", "exiter", "holder"]
    assert body["meta"]["period"] == "2024Q1"


async def test_owners_404_for_a_period_nobody_published(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_quarter_of_trading(db_session)

    response = await client.get(f"/v1/stocks/{ALPHA}/owners", params={"period": "2023Q4"})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == (
        "Nothing is published for 2023Q4. The latest quarter published is 2024Q2."
    )


async def test_owners_page_through_ties_without_a_repeat_or_a_skip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    slugs = [f"fund-{n}" for n in range(7)]
    for slug in slugs:
        await _quarter(db_session, await _fund(db_session, slug), Q1, held(ALPHA, 10))
    await _quarter(db_session, await _fund(db_session, "big"), Q1, held(ALPHA, 99))
    await _publish(db_session)

    rows = await _walk(client, f"/v1/stocks/{ALPHA}/owners", limit=2)

    # Ties on value go to the newer filer first, every time.
    assert [r["slug"] for r in rows] == ["big", *reversed(slugs)]


# --- the history ----------------------------------------------------------------


async def test_history_follows_the_latest_top_five_back_with_the_rest_as_other(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    f1, f2, f3, f4, f5, f6 = [await _fund(db_session, f"f{n}") for n in range(1, 7)]
    await _quarter(db_session, f1, Q1, held(ALPHA, 10))
    await _quarter(db_session, f3, Q1, held(BRAVO, 5))  # published, without ALPHA
    await _quarter(db_session, f4, Q1, held(ALPHA, 30))
    await _quarter(db_session, f5, Q1, held(ALPHA, 20))
    await _quarter(db_session, f6, Q1, held(ALPHA, 1000))  # Q1's largest, Q3's smallest
    # Nothing at all is published for Q2.
    for filer, shares in ((f1, 60), (f2, 50), (f3, 40), (f4, 30), (f5, 20), (f6, 10)):
        await _quarter(db_session, filer, Q3, held(ALPHA, shares))
    await _publish(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}/ownership-history")
    points = body["data"]

    assert [p["period"] for p in points] == ["2024-03-31", "2024-06-30", "2024-09-30"]
    for point in points:
        assert [h["slug"] for h in point["top_holders"]] == ["f1", "f2", "f3", "f4", "f5"]

    q1, q2, q3 = points
    # f2 had published nothing yet: a gap. f3 had, without ALPHA: a zero.
    assert [h["shares"] for h in q1["top_holders"]] == [
        "10.0000",
        None,
        "0.0000",
        "30.0000",
        "20.0000",
    ]
    assert (q1["holder_count"], q1["total_shares"]) == (4, "1060.0000")
    assert q1["other"] == {"holder_count": 1, "shares": "1000.0000", "value_usd": "10000.00"}

    assert q2["holder_count"] is None
    assert q2["total_shares"] is None
    assert q2["other"] is None
    assert all(h["shares"] is None for h in q2["top_holders"])

    assert [h["shares"] for h in q3["top_holders"]] == [
        "60.0000",
        "50.0000",
        "40.0000",
        "30.0000",
        "20.0000",
    ]
    assert q3["top_holders"][0]["value_usd"] == "600.00"
    assert (q3["holder_count"], q3["total_shares"], q3["total_value_usd"]) == (
        6,
        "210.0000",
        "2100.00",
    )
    assert q3["other"] == {"holder_count": 1, "shares": "10.0000", "value_usd": "100.00"}
    assert body["page"] is None


async def test_history_of_a_stock_everyone_left_runs_to_the_latest_period_in_zeros(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    first, second = await _fund(db_session, "first"), await _fund(db_session, "second")
    await _quarter(db_session, first, Q1, held(ALPHA, 10), held(BRAVO, 1))
    await _quarter(db_session, second, Q2, held(ALPHA, 30))
    await _quarter(db_session, first, Q2, held(ALPHA, 20), held(BRAVO, 1))
    await _quarter(db_session, first, Q3, held(BRAVO, 1))
    await _quarter(db_session, second, Q3, held(BRAVO, 1))
    await _publish(db_session)

    points = (await _get(client, f"/v1/stocks/{ALPHA}/ownership-history"))["data"]

    assert [p["period"] for p in points] == ["2024-03-31", "2024-06-30", "2024-09-30"]
    # Chosen from Q2, the last quarter anyone held it.
    assert [h["slug"] for h in points[-1]["top_holders"]] == ["second", "first"]
    assert points[-1]["holder_count"] == 0
    assert [h["shares"] for h in points[-1]["top_holders"]] == ["0.0000", "0.0000"]
    assert points[-1]["other"] == {"holder_count": 0, "shares": "0.0000", "value_usd": "0.00"}
    assert [h["shares"] for h in points[0]["top_holders"]] == [None, "10.0000"]
