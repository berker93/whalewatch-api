"""SEC-8: the price estimate, the returns since filing and the splits, on the wire.

Three funds hold ALPHA through a 10-for-1 in Q2 2024, all three exact to the
share, so it is detected with high confidence. ALPHA is $100 in Q1, $11 in Q2
and $12 in Q3, as their filings value it, and closes at $13 in October. So
the Q1 basis in today's shares is $10, and a Q1 holder is up 30% since filing,
not down 87%.

The returns' arithmetic is :mod:`tests.integration.test_estimated_return`'s.
This is about what reaches each endpoint, and that nothing that was there
before has moved.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Final

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Security, SecurityQuote
from tests.integration.test_portfolio_endpoint import (
    ALPHA,
    BRAVO,
    _fund,
    _get,
    _publish,
    _quarter,
    held,
)

Q1: Final = date(2024, 3, 31)
Q2: Final = date(2024, 6, 30)
Q3: Final = date(2024, 9, 30)
CLOSE_DAY: Final = date(2024, 10, 15)

FUNDS: Final = ("ark", "bay", "cove")

NEW_FIELDS: Final = (
    "estimated_return_pct",
    "estimated_return_as_of",
    "return_to_last_quarter_pct",
    "last_quarter_period",
    "price_estimate",
    "is_split",
    "split_ratio",
)


async def _through_a_split(session: AsyncSession, *, quote: bool = True) -> None:
    """ALPHA's 10-for-1, and BRAVO, which ``ark`` holds in Q1 and Q3 and not Q2,
    with a call on ALPHA in ``ark``'s Q3."""
    for n, slug in enumerate(FUNDS, start=1):
        fund = await _fund(session, slug)
        extra_q1 = [held(BRAVO, 10, price=50)] if slug == "ark" else []
        extra_q3 = (
            [held(BRAVO, 10, price=60), held(ALPHA, 500, price=12, put_call="Call")]
            if slug == "ark"
            else []
        )
        await _quarter(session, fund, Q1, held(ALPHA, 100 * n, price=100), *extra_q1)
        await _quarter(session, fund, Q2, held(ALPHA, 1_000 * n, price=11))
        await _quarter(session, fund, Q3, held(ALPHA, 1_000 * n, price=12), *extra_q3)
    await _publish(session)
    if quote:
        alpha = await session.scalar(select(Security.id).where(Security.cusip == ALPHA))
        await session.execute(
            insert(SecurityQuote).values(
                security_id=alpha,
                date=CLOSE_DAY,
                close=Decimal(13),
                ticker="ALF",
                source="yfinance",
            )
        )


def _estimate(period: str, price: str, observations: int = 3) -> dict[str, Any]:
    return {
        "price": price,
        "period": period,
        "confidence": "high",
        "observation_count": observations,
        "basis": "implied_from_filings",
    }


def _new_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in NEW_FIELDS}


def _row(rows: list[dict[str, Any]], cusip: str, **match: Any) -> dict[str, Any]:
    [row] = [r for r in rows if r["cusip"] == cusip and all(r[k] == v for k, v in match.items())]
    return row


# --- the portfolio ---------------------------------------------------------------


async def test_a_position_before_the_split_is_up_since_filing_in_todays_shares(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, "/v1/investors/ark/portfolio", period="2024Q1"))["data"]

    assert _new_fields(_row(rows, ALPHA)) == {
        # $13 against $100 / 10, not against $100.
        "estimated_return_pct": "30.00",
        "estimated_return_as_of": "2024-10-15",
        # Q3's $12 against the same $10.
        "return_to_last_quarter_pct": "20.00",
        "last_quarter_period": "2024Q3",
        # As filed, in Q1's shares.
        "price_estimate": _estimate("2024Q1", "100.00000000"),
        # Q1 is the first period: nothing to be across.
        "is_split": False,
        "split_ratio": None,
    }


async def test_a_position_across_the_split_is_a_hold_that_says_so(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, "/v1/investors/ark/portfolio", period="2024Q2"))["data"]

    row = _row(rows, ALPHA)
    assert (row["action"], row["shares_delta"]) == ("hold", "0.0000")
    assert _new_fields(row) == {
        "estimated_return_pct": "18.18",
        "estimated_return_as_of": "2024-10-15",
        "return_to_last_quarter_pct": "9.09",
        "last_quarter_period": "2024Q3",
        "price_estimate": _estimate("2024Q2", "11.00000000"),
        "is_split": True,
        "split_ratio": "10.0000000000",
    }


async def test_the_latest_period_has_no_later_quarter_and_an_option_line_no_estimate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, "/v1/investors/ark/portfolio", include_options="true"))["data"]

    stock = _row(rows, ALPHA, put_call=None)
    assert _new_fields(stock) == {
        "estimated_return_pct": "8.33",
        "estimated_return_as_of": "2024-10-15",
        "return_to_last_quarter_pct": None,
        "last_quarter_period": None,
        "price_estimate": _estimate("2024Q3", "12.00000000"),
        "is_split": False,
        "split_ratio": None,
    }
    call = _row(rows, ALPHA, put_call="Call")
    assert all(call[name] is None for name in NEW_FIELDS)


async def test_with_no_quote_the_estimated_return_is_null_and_the_sec_derived_one_is_not(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Production: QUOTE_PROVIDER=null writes no quotes."""
    await _through_a_split(db_session, quote=False)

    rows = (await _get(client, "/v1/investors/ark/portfolio", period="2024Q1"))["data"]

    row = _row(rows, ALPHA)
    assert (row["estimated_return_pct"], row["estimated_return_as_of"]) == (None, None)
    assert (row["return_to_last_quarter_pct"], row["last_quarter_period"]) == ("20.00", "2024Q3")
    assert row["price_estimate"] == _estimate("2024Q1", "100.00000000")


# --- the activity and the owners --------------------------------------------------


async def test_activity_labels_the_change_across_the_split(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, "/v1/investors/bay/activity", action="hold"))["data"]

    assert [
        (row["period"], row["action"], row["is_split"], row["split_ratio"]) for row in rows
    ] == [
        ("2024-09-30", "hold", False, None),
        ("2024-06-30", "hold", True, "10.0000000000"),
    ]
    # The estimate is at each row's own period.
    assert [row["price_estimate"]["period"] for row in rows] == ["2024Q3", "2024Q2"]
    assert rows[1]["estimated_return_pct"] == "18.18"


async def test_every_owner_has_the_stocks_estimate_and_its_own_split(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, f"/v1/stocks/{ALPHA}/owners", period="2024Q2"))["data"]

    assert [row["slug"] for row in rows] == ["cove", "bay", "ark"]
    assert {(row["is_split"], row["split_ratio"], row["estimated_return_pct"]) for row in rows} == {
        (True, "10.0000000000", "18.18")
    }
    assert all(row["price_estimate"] == _estimate("2024Q2", "11.00000000") for row in rows)


# --- implied prices and splits ----------------------------------------------------


async def test_implied_prices_are_a_quarter_a_row_with_todays_shares_beside_them(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    body = await _get(client, f"/v1/stocks/{ALPHA}/implied-prices")

    assert body["data"] == [
        {
            "period": "2024-03-31",
            "price": "100.00000000",
            "split_adjusted_price": "10.00000000",
            "confidence": "high",
            "observation_count": 3,
            "unpriced_count": 0,
            "agreeing_count": 3,
        },
        {
            "period": "2024-06-30",
            "price": "11.00000000",
            "split_adjusted_price": "11.00000000",
            "confidence": "high",
            "observation_count": 3,
            "unpriced_count": 0,
            "agreeing_count": 3,
        },
        {
            "period": "2024-09-30",
            "price": "12.00000000",
            "split_adjusted_price": "12.00000000",
            "confidence": "high",
            "observation_count": 3,
            "unpriced_count": 0,
            "agreeing_count": 3,
        },
    ]
    assert body["page"] is None


async def test_a_quarter_nobody_held_the_stock_is_a_gap_of_nulls(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, f"/v1/stocks/{BRAVO}/implied-prices"))["data"]

    assert [(row["period"], row["price"], row["confidence"]) for row in rows] == [
        ("2024-03-31", "50.00000000", "low"),
        ("2024-06-30", None, None),
        ("2024-09-30", "60.00000000", "low"),
    ]
    assert rows[1]["observation_count"] is None


async def test_splits_are_listed_for_the_stock_that_split_and_empty_for_one_that_did_not(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    assert (await _get(client, f"/v1/stocks/{ALPHA}/splits"))["data"] == [
        {
            "period": "2024-06-30",
            "numerator": 10,
            "denominator": 1,
            "ratio": "10.0000000000",
            "confidence": "high",
            "holders": 3,
            "holders_agreeing": 3,
            "holders_exact": 3,
        }
    ]
    assert (await _get(client, f"/v1/stocks/{BRAVO}/splits"))["data"] == []


async def test_both_find_the_stock_by_ticker_as_the_other_stock_endpoints_do(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)
    await db_session.execute(
        update(Security)
        .where(Security.cusip == ALPHA)
        .values(ticker="ALF", resolution_source="manual")
    )

    assert len((await _get(client, "/v1/stocks/alf/implied-prices"))["data"]) == 3
    assert len((await _get(client, "/v1/stocks/alf/splits"))["data"]) == 1
    for path in ("/v1/stocks/NOPE/implied-prices", "/v1/stocks/NOPE/splits"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# --- data sources ----------------------------------------------------------------


async def test_data_sources_say_the_latest_close_is_absent_without_quotes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session, quote=False)

    rows = (await _get(client, "/v1/meta/data-sources"))["data"]

    by_key = {row["key"]: row for row in rows}
    assert len(by_key) == len(rows)
    assert by_key["latest_close"] == {
        "key": "latest_close",
        "kind": "absent",
        "provider": None,
        "description": by_key["latest_close"]["description"],
        "fields": ["estimated_return_pct", "estimated_return_as_of"],
    }
    assert by_key["implied_price"]["kind"] == "sec_derived"
    assert "price_estimate" in by_key["implied_price"]["fields"]
    assert by_key["splits"]["fields"] == [
        "is_split",
        "split_ratio",
        "GET /v1/stocks/{ticker}/splits",
    ]
    assert by_key["sector"]["kind"] == "absent"
    # The SEC-derived first.
    kinds = [row["kind"] for row in rows]
    assert kinds[: kinds.count("sec_derived")] == ["sec_derived"] * kinds.count("sec_derived")


async def test_data_sources_name_the_vendor_once_a_quote_is_loaded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _through_a_split(db_session)

    rows = (await _get(client, "/v1/meta/data-sources"))["data"]

    [close] = [row for row in rows if row["key"] == "latest_close"]
    assert (close["kind"], close["provider"]) == ("vendor", "yfinance")


# --- the contract ----------------------------------------------------------------


@pytest.mark.parametrize("schema", ["PortfolioPosition", "StockOwner", "Activity"])
def test_every_new_field_is_optional_and_nullable(app: FastAPI, schema: str) -> None:
    """06c: additive only. A client generated before SEC-8 is not sent a field
    it must handle, and one generated after must expect null in every one."""
    component = app.openapi()["components"]["schemas"][schema]

    assert not set(NEW_FIELDS) & set(component.get("required", []))
    for name in NEW_FIELDS:
        types = {
            option.get("type") or option.get("$ref")
            for option in component["properties"][name]["anyOf"]
        }
        assert "null" in types, name
