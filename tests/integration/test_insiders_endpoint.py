"""``/v1/insiders/transactions`` and ``/v1/stocks/{ticker}/insiders``, against a real Postgres.

The lines are written straight into the tables, as the loader leaves them, so
each test says exactly which codes, roles, values and dates it has. What
matters is what a reader could be misled by: a grant or a tax withholding in a
feed that says it shows trades, a default nobody is told about, a gift that
``min_value`` lets through, a dual-class issuer's lines missing from both of
its stocks, a page boundary inside one day's trades.
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count
from typing import Any

from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pagination import PageParams
from app.api.routers.insiders import transactions_query
from app.db.models import (
    Filing,
    Insider,
    InsiderTransaction,
    ReportingOwner,
    SecCompanyTicker,
    Security,
    SecurityAlias,
)
from tests.integration.test_insider_schema import partition_indexes

NVIDIA = "0001045810"
LENNAR = "0000920760"
NVDA_CUSIP = "67066G104"
LEN_CUSIP = "526057104"
LENB_CUSIP = "526057302"

DAY_1 = date(2026, 9, 1)
DAY_2 = date(2026, 9, 2)
DAY_3 = date(2026, 9, 3)

_accessions = count(1)
_ciks = count(1)


async def _security(session: AsyncSession, cusip: str, ticker: str) -> int:
    security_id = await session.scalar(
        insert(Security)
        .values(cusip=cusip, name=f"ISSUER {ticker}", ticker=ticker, resolution_source="manual")
        .returning(Security.id)
    )
    assert security_id is not None
    return security_id


async def _insider(session: AsyncSession, name: str) -> int:
    insider_id = await session.scalar(
        insert(Insider).values(cik=f"{next(_ciks):010d}", name=name).returning(Insider.id)
    )
    assert insider_id is not None
    return insider_id


async def _form4(
    session: AsyncSession,
    insider_id: int,
    *lines: dict[str, Any],
    issuer_cik: str = NVIDIA,
    security_id: int | None = None,
    issuer_ticker: str | None = "NVDA",
    **roles: Any,
) -> str:
    """One Form 4 by ``insider_id``, with ``lines`` (from :func:`_line`), as loaded."""
    accession = f"0009999999-26-{next(_accessions):06d}"
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=accession,
            cik=issuer_cik,
            form_type="4",
            filed_at=datetime(2026, 9, 4, 21, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    await session.execute(
        insert(ReportingOwner).values(filing_id=filing_id, insider_id=insider_id, **roles)
    )
    await session.execute(
        insert(InsiderTransaction).values(
            [
                {
                    "filing_id": filing_id,
                    "insider_id": insider_id,
                    "line_number": number,
                    "issuer_cik": issuer_cik,
                    "issuer_name": f"ISSUER {issuer_cik}",
                    "issuer_ticker": issuer_ticker,
                    "security_id": security_id,
                    "security_title": "Common Stock",
                    "is_derivative": False,
                    **line,
                }
                for number, line in enumerate(lines, start=1)
            ]
        )
    )
    return accession


def _line(
    code: str,
    day: date = DAY_1,
    *,
    shares: str = "100",
    price: str | None = "10",
    plan: bool = False,
) -> dict[str, Any]:
    return {
        "transaction_code": code,
        "transaction_date": day,
        "acquired_disposed": "A" if code in ("P", "A", "M") else "D",
        "shares": Decimal(shares),
        "price_per_share": None if price is None else Decimal(price),
        "shares_owned_after": Decimal(1000),
        "is_10b5_1": plan,
    }


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _walk(client: AsyncClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Every page, two rows at a time; the rows in the order they came."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        extra: dict[str, str] = {"cursor": cursor} if cursor else {}
        body = await _get(client, path, limit=2, **params, **extra)
        rows += body["data"]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return rows


def _codes(rows: list[dict[str, Any]]) -> list[str]:
    return [row["code"] for row in rows]


async def _a_mixed_week(session: AsyncSession) -> int:
    """Every kind of line, linked to NVDA: what the default must leave out."""
    nvda = await _security(session, NVDA_CUSIP, "NVDA")
    ceo = await _insider(session, "HUANG JEN HSUN")
    director = await _insider(session, "STEVENS MARK A")
    await _form4(
        session,
        ceo,
        _line("S", DAY_1, shares="500", price="140", plan=True),
        _line("F", DAY_1, shares="200", price="140"),
        _line("A", DAY_2, shares="1000", price=None),
        _line("M", DAY_2, shares="300", price="3.97"),
        security_id=nvda,
        is_officer=True,
        is_director=True,
        officer_title="President and CEO",
    )
    await _form4(
        session,
        director,
        _line("P", DAY_3, shares="1000", price="120"),
        _line("G", DAY_3, shares="50", price=None),
        security_id=nvda,
        is_director=True,
    )
    return nvda


# --- the default --------------------------------------------------------------


async def test_the_default_is_open_market_trades_and_meta_says_so(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    body = await _get(client, "/v1/insiders/transactions")

    assert _codes(body["data"]) == ["P", "S"]
    assert body["meta"]["codes"] == [
        {"code": "P", "label": "Open-market or private purchase"},
        {"code": "S", "label": "Open-market or private sale"},
    ]
    assert body["meta"]["codes_default"] is True
    assert body["meta"]["period"] is None


async def test_code_all_lifts_the_default_and_meta_says_that_too(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    body = await _get(client, "/v1/insiders/transactions", code="all")

    assert sorted(_codes(body["data"])) == ["A", "F", "G", "M", "P", "S"]
    assert body["meta"]["codes"] is None
    assert body["meta"]["codes_default"] is False


async def test_codes_chosen_are_stated_even_when_they_are_the_default_ones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    chosen = await _get(client, "/v1/insiders/transactions", code="s,P")
    others = await _get(client, "/v1/insiders/transactions", code=["a", "m"])

    assert _codes(chosen["data"]) == ["P", "S"]
    assert chosen["meta"]["codes_default"] is False
    assert sorted(_codes(others["data"])) == ["A", "M"]
    assert [c["code"] for c in others["meta"]["codes"]] == ["A", "M"]


# --- a row --------------------------------------------------------------------


async def test_a_row_names_the_issuer_the_insider_the_role_and_the_code(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    sale = (await _get(client, "/v1/insiders/transactions", code="S"))["data"][0]

    assert sale | {"accession_no": None, "insider_cik": None} == {
        "accession_no": None,
        "line_number": 1,
        "transaction_date": str(DAY_1),
        "filed_at": "2026-09-04T21:00:00Z",
        "issuer_cik": NVIDIA,
        "issuer_name": f"ISSUER {NVIDIA}",
        "issuer_ticker": "NVDA",
        "cusip": NVDA_CUSIP,
        "ticker": "NVDA",
        "current_ticker": "NVDA",
        "delisted_by": None,
        "insider_cik": None,
        "insider_name": "HUANG JEN HSUN",
        "roles": ["director", "officer"],
        "officer_title": "President and CEO",
        "other_role": None,
        "code": "S",
        "code_label": "Open-market or private sale",
        "acquired_disposed": "D",
        "is_derivative": False,
        "security_title": "Common Stock",
        "shares": "500.0000",
        "price_per_share": "140.00000000",
        "value_usd": "70000.00",
        "shares_owned_after": "1000.0000",
        "is_10b5_1": True,
        "footnotes": None,
    }


async def test_the_ticker_is_the_one_traded_under_on_the_trade_day(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    meta = await _security(db_session, "30303M102", "META")
    for ticker, valid_from, valid_to in (("FB", None, DAY_2), ("META", DAY_2, None)):
        await db_session.execute(
            insert(SecurityAlias).values(
                security_id=meta,
                ticker=ticker,
                valid_from=valid_from,
                valid_to=valid_to,
                source="manual",
            )
        )
    insider = await _insider(db_session, "ZUCKERBERG MARK")
    await _form4(
        db_session,
        insider,
        _line("S", DAY_1),
        _line("S", DAY_3),
        security_id=meta,
        is_ten_percent_owner=True,
    )

    rows = (await _get(client, "/v1/insiders/transactions"))["data"]

    assert [(r["transaction_date"], r["ticker"], r["current_ticker"]) for r in rows] == [
        (str(DAY_3), "META", "META"),
        (str(DAY_1), "FB", "META"),
    ]
    assert rows[0]["roles"] == ["ten_percent_owner"]


async def test_an_unknown_code_is_served_without_a_label(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    insider = await _insider(db_session, "SOMEONE")
    await _form4(db_session, insider, _line("Y"))

    [row] = (await _get(client, "/v1/insiders/transactions", code="Y"))["data"]

    assert (row["code"], row["code_label"], row["cusip"]) == ("Y", None, None)


# --- the filters --------------------------------------------------------------


async def test_role_matches_any_role_checked(client: AsyncClient, db_session: AsyncSession) -> None:
    await _a_mixed_week(db_session)

    directors = await _get(client, "/v1/insiders/transactions", role="director")
    officers = await _get(client, "/v1/insiders/transactions", role="officer")
    either = await _get(client, "/v1/insiders/transactions", role="officer,ten_percent_owner")
    owners = await _get(client, "/v1/insiders/transactions", role="ten_percent_owner")

    assert _codes(directors["data"]) == ["P", "S"]
    assert _codes(officers["data"]) == ["S"]
    assert _codes(either["data"]) == ["S"]
    assert owners["data"] == []


async def test_min_value_leaves_out_lines_with_no_value(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A grant and a gift have no price, so no value, and are not worth at
    least anything: min_value=0 is every line that has a value."""
    await _a_mixed_week(db_session)

    priced = await _get(client, "/v1/insiders/transactions", code="all", min_value="0")
    large = await _get(client, "/v1/insiders/transactions", code="all", min_value="70000")

    assert sorted(_codes(priced["data"])) == ["F", "M", "P", "S"]
    assert sorted(_codes(large["data"])) == ["P", "S"]


async def test_the_date_range_is_inclusive_at_both_ends(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    rows = (
        await _get(
            client,
            "/v1/insiders/transactions",
            code="all",
            **{"from": str(DAY_2), "to": str(DAY_2)},
        )
    )["data"]
    since = (await _get(client, "/v1/insiders/transactions", **{"from": str(DAY_2)}))["data"]

    assert sorted(_codes(rows)) == ["A", "M"]
    assert _codes(since) == ["P"]


async def test_exclude_10b5_1_drops_planned_trades(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    rows = (await _get(client, "/v1/insiders/transactions", exclude_10b5_1="true"))["data"]

    assert _codes(rows) == ["P"]


async def test_a_parameter_that_cannot_be_answered_is_a_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    for params in (
        {"code": "PS"},
        {"code": "all,P"},
        {"code": ","},
        {"role": "ceo"},
        {"min_value": "-1"},
        {"sort": "size"},
        {"from": str(DAY_3), "to": str(DAY_1)},
    ):
        response = await client.get("/v1/insiders/transactions", params=params)
        assert response.status_code == 422, params


# --- sorting and paging -------------------------------------------------------


async def test_value_sorts_largest_first_with_valueless_lines_last(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    largest = await _get(client, "/v1/insiders/transactions", code="all", sort="value")
    smallest = await _get(
        client, "/v1/insiders/transactions", code="all", sort="value", order="asc"
    )

    assert [r["value_usd"] for r in largest["data"]] == [
        "120000.00",
        "70000.00",
        "28000.00",
        "1191.00",
        # The gift, then the grant: no value, so newest first.
        None,
        None,
    ]
    assert [r["code"] for r in smallest["data"]] == ["A", "G", "M", "F", "S", "P"]


async def test_a_walk_returns_every_row_once_in_order_across_one_days_trades(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Seven tranches on one day, two lines a page: every boundary but the
    first is inside the day, where the id decides."""
    insider = await _insider(db_session, "SELLER")
    await _form4(db_session, insider, *(_line("S", DAY_2) for _ in range(7)), _line("S", DAY_1))

    for sort in ("date", "value"):
        for order in ("desc", "asc"):
            rows = await _walk(client, "/v1/insiders/transactions", sort=sort, order=order)
            keys = [(r["transaction_date"], r["line_number"]) for r in rows]
            expected = sorted(keys, reverse=order == "desc")
            assert keys == expected, (sort, order)


async def test_a_cursor_is_refused_by_another_listing_or_sort(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)
    cursor = (await _get(client, "/v1/insiders/transactions", code="all", limit=1))["page"][
        "next_cursor"
    ]

    by_value = await client.get(
        "/v1/insiders/transactions", params={"cursor": cursor, "sort": "value"}
    )
    on_stock = await client.get("/v1/stocks/NVDA/insiders", params={"cursor": cursor})

    assert (by_value.status_code, on_stock.status_code) == (400, 400)


# --- one stock ----------------------------------------------------------------


async def _lennar(session: AsyncSession) -> tuple[int, int]:
    """Two classes under one CIK: a line linked to each, and one the loader
    could not choose a class for."""
    len_a = await _security(session, LEN_CUSIP, "LEN")
    len_b = await _security(session, LENB_CUSIP, "LEN.B")
    await session.execute(
        insert(SecCompanyTicker).values(
            [
                {
                    "ticker": "LEN",
                    "cik": LENNAR,
                    "name": "LENNAR CORP",
                    "seen_at": datetime.now(UTC),
                },
                {
                    "ticker": "LEN-B",
                    "cik": LENNAR,
                    "name": "LENNAR CORP",
                    "seen_at": datetime.now(UTC),
                },
            ]
        )
    )
    insider = await _insider(session, "MILLER STUART")
    for day, security_id, symbol in (
        (DAY_1, len_a, "LEN"),
        (DAY_2, len_b, "LEN.B"),
        (DAY_3, None, "LEN, LEN.B"),
    ):
        await _form4(
            session,
            insider,
            _line("S", day),
            issuer_cik=LENNAR,
            security_id=security_id,
            issuer_ticker=symbol,
            is_director=True,
        )
    return len_a, len_b


async def test_a_stocks_insiders_include_its_issuers_unlinked_lines(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _lennar(db_session)
    await _a_mixed_week(db_session)

    class_a = (await _get(client, "/v1/stocks/LEN/insiders"))["data"]
    class_b = (await _get(client, "/v1/stocks/len-b/insiders"))["data"]
    nvidia = (await _get(client, "/v1/stocks/NVDA/insiders"))["data"]

    assert [(r["transaction_date"], r["cusip"]) for r in class_a] == [
        (str(DAY_3), None),
        (str(DAY_1), LEN_CUSIP),
    ]
    assert [(r["transaction_date"], r["cusip"]) for r in class_b] == [
        (str(DAY_3), None),
        (str(DAY_2), LENB_CUSIP),
    ]
    assert {r["issuer_cik"] for r in nvidia} == {NVIDIA}


async def test_ticker_on_the_feed_is_the_stock_pages_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _lennar(db_session)
    await _a_mixed_week(db_session)

    for ticker in ("LEN", "LEN.B", "NVDA", NVDA_CUSIP):
        feed = await _get(client, "/v1/insiders/transactions", ticker=ticker, code="all")
        page = await _get(client, f"/v1/stocks/{ticker}/insiders", code="all")
        assert feed["data"] == page["data"], ticker
        assert feed["meta"]["codes"] is None


async def test_an_unknown_stock_is_a_404_with_suggestions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_mixed_week(db_session)

    feed = await client.get("/v1/insiders/transactions", params={"ticker": "NVD"})
    page = await client.get("/v1/stocks/NVD/insiders")

    assert (feed.status_code, page.status_code) == (404, 404)
    assert [s["ticker"] for s in page.json()["error"]["detail"]["suggestions"]] == ["NVDA"]


# --- the plan -----------------------------------------------------------------


async def test_the_default_feed_reads_the_open_market_index_in_order(
    db_session: AsyncSession,
) -> None:
    """The codes are literals, so the partial index's predicate is implied
    whatever the plan, and its order is the keyset's, so nothing is sorted.
    With sequential scans and sorts off, so that a near-empty table still
    shows which plan the planner can use rather than which is cheapest on
    no rows."""
    await _a_mixed_week(db_session)
    statement, _ = transactions_query(PageParams())
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_sort = off"))

    plan = "\n".join((await db_session.execute(text(f"EXPLAIN {sql}"))).scalars().all())
    indexes = await partition_indexes(
        db_session, "ix_insider_transaction_transaction_date_open_market"
    )

    # One index scan per partition, merged in order by a Merge Append, never
    # sorted. The ticker's subquery sorts a security's few aliases; the lines
    # never are.
    assert any(f" using {index} " in plan for index in indexes)
    assert re.search(r"->  Sort .*\n\s+Sort Key: insider_transaction", plan) is None
