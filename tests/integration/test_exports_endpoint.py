"""The ``.csv`` and ``.xlsx`` exports, against a real Postgres.

Each file must be its listing: the same rows a walk of every page returns, in
the same order. Beyond that, what matters is what a spreadsheet would get
wrong or hide: a filing date from the wrong filing, a number Excel reads as a
date or a formula, a header with no period, an export on a plan without it.
A workbook must have the CSV's rows, and say on its own sheets what the CSV's
``#`` lines say.
"""

import csv
import io
from datetime import UTC, datetime
from typing import Any, Final

import openpyxl
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.entitlements import utc_today
from app.api.routers.exports import NO_COVERAGE
from app.api.xlsx_export import MEDIA_TYPE as XLSX
from app.db.models import Security, UserActivityDay
from tests.integration.accounts import make_user, sign_in, subscribe
from tests.integration.test_insiders_endpoint import (
    DAY_1,
    DAY_2,
    NVDA_CUSIP,
    _form4,
    _insider,
    _line,
    _security,
)
from tests.integration.test_portfolio_endpoint import (
    ALPHA,
    BRAVO,
    CHARLIE,
    Q1,
    Q2,
    Q3,
    _at,
    _fund,
    _publish,
    _quarter,
    _walk,
    held,
)

#: The provenance every file ends with.
PROVENANCE = ["as_of_period", "filed_at", "confidence", "observation_count"]


@pytest.fixture
async def pro(client: AsyncClient, db_session: AsyncSession) -> int:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro")
    await sign_in(client)
    return user_id


@pytest.fixture
async def free(client: AsyncClient, db_session: AsyncSession) -> int:
    user_id = await make_user(db_session)
    await sign_in(client)
    return user_id


def _file(response: Response) -> tuple[list[str], list[dict[str, str]]]:
    """The ``#`` lines, without the ``# ``, and the rows by column name."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    text = response.content.decode("utf-8")
    assert text.startswith("﻿"), "no BOM: Excel would misread accented names"
    lines = text[1:].split("\r\n")
    comments = [line.removeprefix("# ") for line in lines if line.startswith("#")]
    table = "\r\n".join(line for line in lines if not line.startswith("#"))
    return comments, list(csv.DictReader(io.StringIO(table)))


def _named(response: Response) -> str:
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    return disposition.split('filename="', 1)[1].rstrip('"')


def _header(comments: list[str], key: str) -> str:
    [line] = [c for c in comments if c.startswith(f"{key}: ")]
    return line.removeprefix(f"{key}: ")


async def _two_quarters(session: AsyncSession) -> dict[Any, Any]:
    """BERKSHIRE in Q1 and Q2, each filed on its own day."""
    filer = await _fund(session, "berkshire")
    q1 = await _quarter(session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50), days_later=40)
    q2 = await _quarter(
        session,
        filer,
        Q2,
        held(ALPHA, 1_234_567, price=1000),
        held(CHARLIE, 30),
        days_later=44,
    )
    await _publish(session)
    return {"filer": filer, Q1: q1, Q2: q2}


# --- who may --------------------------------------------------------------------


async def test_signed_out_is_refused_before_anything_is_read(client: AsyncClient) -> None:
    response = await client.get("/v1/investors/nobody/portfolio.csv")
    assert response.status_code == 402
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == "plan_limit"
    assert (error["detail"]["limit"], error["detail"]["allowed"]) == ("export_enabled", 0)
    assert error["detail"]["plan"]["slug"] == "pro"


EXPORTS: Final = [
    "/v1/investors/berkshire/portfolio",
    "/v1/investors/berkshire/activity",
    f"/v1/stocks/{ALPHA}/owners",
    "/v1/flows",
    "/v1/insiders/transactions",
]


@pytest.mark.parametrize("path", [f"{p}.{ext}" for p in EXPORTS for ext in ("csv", "xlsx")])
async def test_free_is_refused_every_export(
    client: AsyncClient, db_session: AsyncSession, free: int, path: str
) -> None:
    await _two_quarters(db_session)
    response = await client.get(path)
    assert response.status_code == 402, response.text
    assert response.json()["error"]["detail"]["limit"] == "export_enabled"


async def test_an_unknown_investor_is_a_404_not_an_empty_file(
    client: AsyncClient, pro: int
) -> None:
    response = await client.get("/v1/investors/nobody/portfolio.csv")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


async def test_each_export_is_counted_for_the_account(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    await _two_quarters(db_session)
    for path in ("/v1/investors/berkshire/portfolio.csv", "/v1/flows.csv"):
        assert (await client.get(path)).status_code == 200
    exports = await db_session.scalar(
        select(UserActivityDay.data_exports).where(UserActivityDay.user_id == pro)
    )
    assert exports == 2


# --- holdings -------------------------------------------------------------------


async def test_holdings_are_the_portfolio_walked_to_the_end(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)

    response = await client.get("/v1/investors/berkshire/portfolio.csv", params={"sort": "shares"})
    comments, rows = _file(response)

    assert _named(response) == "berkshire_2024Q2_holdings.csv"
    walked = await _walk(client, "/v1/investors/berkshire/portfolio", sort="shares", limit=1)
    assert [row["cusip"] for row in rows] == [row["cusip"] for row in walked] == [ALPHA, CHARLIE]
    assert list(rows[0])[-6:] == [*PROVENANCE, "is_split_adjusted", "split_ratio"]

    alpha = rows[0]
    # Plain digits: no exponent, no separators, never a float's rounding.
    assert alpha["value_usd"] == "1234567000.00"
    assert alpha["shares"] == "1234567.0000"
    assert alpha["shares_delta"] == "1234467.0000"
    assert alpha["action"] == "add"
    assert alpha["first_period"] == "2024-03-31"
    assert alpha["as_of_period"] == "2024Q2"
    assert alpha["filed_at"] == _at(filed[Q2])
    assert alpha["is_split_adjusted"] == "false"
    # One filer's own prices: an implied price, and how sure of it.
    assert alpha["implied_price"] == "1000.00000000"
    assert (alpha["confidence"], alpha["observation_count"]) == ("low", "1")

    assert (
        comments[0]
        == "WhaleWatch export: BERKSHIRE holdings in 2024Q2 (the quarter ended 2024-06-30)"
    )
    assert "45 days" in _header(comments, "Disclaimer")
    assert _header(comments, "Source").startswith("SEC EDGAR Form 13F-HR")
    assert _header(comments, "Page").endswith("/investors/berkshire?period=2024Q2")
    assert _header(comments, "Generated").endswith("Z")
    assert _header(comments, "EDGAR").startswith("https://www.sec.gov/")
    assert _header(comments, "Coverage") == "1 of 1 investors have filed for 2024Q2."


async def test_holdings_narrowed_as_the_table_narrows_them(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    """The action chips and the search box, on the server: the browser need
    not filter the file it was sent."""
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    await _quarter(
        db_session,
        filer,
        Q2,
        held(ALPHA, 200),
        held(CHARLIE, 30),
        held(ALPHA, 5, put_call="Call"),
    )
    await db_session.execute(
        update(Security)
        .where(Security.cusip == ALPHA)
        .values(ticker="ALF", resolution_source="manual")
    )
    await _publish(db_session)
    path = "/v1/investors/berkshire/portfolio.csv"

    async def held_rows(**params: str) -> list[tuple[str, str]]:
        _, rows = _file(await client.get(path, params={"include_options": "true", **params}))
        return [(row["cusip"], row["put_call"]) for row in rows]

    # An option line has no change, so any action leaves it out.
    assert await held_rows(action="add") == [(ALPHA, "")]
    assert await held_rows(action="new,add") == [(ALPHA, ""), (CHARLIE, "")]
    assert await held_rows(action="trim") == []
    # The ticker as of the period, or the company, in any case.
    assert await held_rows(q="alf") == [(ALPHA, ""), (ALPHA, "Call")]
    assert await held_rows(q="issuer 33333c") == [(CHARLIE, "")]
    assert await held_rows(q="alf", action="add") == [(ALPHA, "")]
    # A wildcard is a character, not a pattern.
    assert await held_rows(q="%") == []

    response = await client.get(path, params={"action": "add", "q": "alf"})
    comments, _ = _file(response)
    assert _header(comments, "Actions") == "add"
    assert _header(comments, "Matching") == "Ticker or company contains 'alf'"
    about = _about(_workbook(await client.get(path.replace(".csv", ".xlsx"), params={"q": "alf"})))
    assert about["Matching"] == "Ticker or company contains 'alf'"
    assert "Actions" not in about

    # No exits among the holdings: those are the quarter's activity.
    refused = await client.get(path, params={"action": "exit"})
    assert refused.status_code == 422, refused.text
    assert "activity" in refused.text
    assert (await client.get(path, params={"q": ""})).status_code == 422


async def test_holdings_for_an_earlier_period(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)
    response = await client.get(
        "/v1/investors/berkshire/portfolio.csv", params={"period": "2024Q1"}
    )
    _, rows = _file(response)
    assert _named(response) == "berkshire_2024Q1_holdings.csv"
    assert {row["cusip"] for row in rows} == {ALPHA, BRAVO}
    assert {row["filed_at"] for row in rows} == {_at(filed[Q1])}


# --- activity -------------------------------------------------------------------


async def test_each_change_carries_its_own_periods_filing(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)
    q3 = await _quarter(db_session, filed["filer"], Q3, held(CHARLIE, 60), days_later=30)
    await _publish(db_session)

    response = await client.get("/v1/investors/berkshire/activity.csv")
    comments, rows = _file(response)

    assert _named(response) == "berkshire_2024Q2-2024Q3_activity.csv"
    walked = await _walk(client, "/v1/investors/berkshire/activity", limit=1)
    assert [(r["cusip"], r["period"]) for r in rows] == [(r["cusip"], r["period"]) for r in walked]
    by_period = {row["period"]: row["filed_at"] for row in rows}
    assert by_period == {"2024-06-30": _at(filed[Q2]), "2024-09-30": _at(q3)}
    assert {row["as_of_period"] for row in rows} == {"2024Q2", "2024Q3"}
    assert _header(comments, "Actions") == "add; exit; new; trim"


async def test_activity_of_one_quarter_is_named_by_it(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    await _two_quarters(db_session)
    response = await client.get("/v1/investors/berkshire/activity.csv", params={"action": "exit"})
    _, rows = _file(response)
    assert _named(response) == "berkshire_2024Q2_activity.csv"
    assert [(row["cusip"], row["action"]) for row in rows] == [(BRAVO, "exit")]


# --- owners ---------------------------------------------------------------------


async def test_each_holder_carries_its_own_filing(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)
    other = await _fund(db_session, "pershing")
    pershing = await _quarter(db_session, other, Q2, held(ALPHA, 10), days_later=20)
    await _publish(db_session)

    response = await client.get(f"/v1/stocks/{ALPHA}/owners.csv")
    comments, rows = _file(response)

    assert _named(response) == f"{ALPHA.lower()}_2024Q2_holders.csv"
    walked = await _walk(client, f"/v1/stocks/{ALPHA}/owners", limit=1)
    assert [row["slug"] for row in rows] == [row["slug"] for row in walked]
    assert {row["slug"]: row["filed_at"] for row in rows} == {
        "berkshire": _at(filed[Q2]),
        "pershing": _at(pershing),
    }
    assert {row["as_of_period"] for row in rows} == {"2024Q2"}
    assert f"CUSIP {ALPHA}" in comments[0]


# --- the screener ---------------------------------------------------------------


async def test_the_screener_file_is_its_filtered_listing(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)

    params = {"sort": "gross_bought", "direction": "buy"}
    response = await client.get("/v1/flows.csv", params=params)
    comments, rows = _file(response)

    assert _named(response) == "screener_2024Q2_flows.csv"
    walked = await _walk(client, "/v1/flows", limit=1, **params)
    assert [row["cusip"] for row in rows] == [row["cusip"] for row in walked]
    assert [row["cusip"] for row in rows] == [ALPHA, CHARLIE]
    assert rows[0]["gross_bought_usd"] == walked[0]["gross_bought_usd"]
    # The newest filing the aggregates include; no split column, aggregates have none.
    assert {row["filed_at"] for row in rows} == {_at(filed[Q2])}
    assert "is_split_adjusted" not in rows[0]
    assert list(rows[0])[-4:] == PROVENANCE
    assert _header(comments, "Page").endswith("/flows?sort=gross_bought&direction=buy")
    assert _header(comments, "Coverage") == "1 of 1 investors have filed for 2024Q2."
    assert _header(comments, "Aggregates as of") != "unrecorded"


async def test_the_screener_refuses_a_sector_as_its_listing_does(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    await _two_quarters(db_session)
    response = await client.get("/v1/flows.csv", params={"sector": "Technology"})
    assert response.status_code == 422


# --- insiders -------------------------------------------------------------------


async def test_the_insider_file_is_its_feed(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    nvda = await _security(db_session, NVDA_CUSIP, "NVDA")
    huang = await _insider(db_session, "HUANG JEN")
    # Anyone can file a name: a spreadsheet must show it, not evaluate it.
    mischief = await _insider(db_session, '=HYPERLINK("http://evil.example","x")')
    await _form4(
        db_session,
        huang,
        _line("S", DAY_2, shares="1000", price="120.5"),
        security_id=nvda,
        is_officer=True,
        is_director=True,
        officer_title="CEO",
    )
    await _form4(
        db_session,
        mischief,
        _line("P", DAY_1),
        _line("A", DAY_1),
        security_id=nvda,
        is_ten_percent_owner=True,
    )

    response = await client.get("/v1/insiders/transactions.csv", params={"ticker": "NVDA"})
    comments, rows = _file(response)

    assert _named(response) == f"nvda_to-{utc_today()}_trades.csv"
    walked = await _walk_insiders(client, ticker="NVDA")
    assert [row["accession_no"] for row in rows] == [row["accession_no"] for row in walked]
    # The grant is left out by default, as the feed leaves it out, and says so.
    assert [row["code"] for row in rows] == ["S", "P"]
    assert _header(comments, "Codes").endswith("(the default)")
    assert "two business days" in _header(comments, "Disclaimer")

    sale, purchase = rows
    assert sale["roles"] == "director;officer"
    assert sale["value_usd"] == "120500.00"
    assert sale["as_of_period"] == DAY_2.isoformat()
    assert sale["filed_at"] == "2026-09-04T21:00:00Z"
    # A reported price, not an implied one: nothing to be confident of.
    assert (sale["confidence"], sale["observation_count"]) == ("", "")
    assert purchase["insider_name"] == '\'=HYPERLINK("http://evil.example","x")'
    assert list(sale)[-4:] == PROVENANCE


async def _walk_insiders(client: AsyncClient, **params: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = await client.get(
            "/v1/insiders/transactions",
            params={**params, "limit": 1, **({"cursor": cursor} if cursor else {})},
        )
        body = response.json()
        rows += body["data"]
        if (cursor := body["page"]["next_cursor"]) is None:
            return rows


async def test_an_empty_listing_is_a_file_with_only_its_header(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    _, rows = _file(
        await client.get("/v1/insiders/transactions.csv", params={"min_value": str(10**12)})
    )
    assert rows == []


# --- workbooks ------------------------------------------------------------------


def _workbook(response: Response) -> openpyxl.Workbook:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == XLSX
    assert response.headers["cache-control"] == "no-store"
    assert int(response.headers["content-length"]) == len(response.content)
    return openpyxl.load_workbook(io.BytesIO(response.content))


def _data(book: openpyxl.Workbook) -> list[dict[str, Any]]:
    header, *rows = book["Data"].iter_rows(values_only=True)
    return [dict(zip(header, row, strict=True)) for row in rows]


def _about(book: openpyxl.Workbook) -> dict[str, Any]:
    return {label: value for label, value in book["About"].iter_rows(values_only=True)}


def _methodology(book: openpyxl.Workbook) -> list[str]:
    """The sheet's headings: the bold first-column cells with nothing beside them."""
    return [a for a, b in book["Methodology"].iter_rows(values_only=True, max_col=2) if a and not b]


async def test_the_holdings_workbook_is_the_csv_with_its_caveats_on_board(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)
    params = {"sort": "shares"}

    response = await client.get("/v1/investors/berkshire/portfolio.xlsx", params=params)
    book = _workbook(response)
    _, csv_rows = _file(await client.get("/v1/investors/berkshire/portfolio.csv", params=params))

    assert _named(response) == "berkshire_2024Q2_holdings.xlsx"
    assert book.sheetnames == ["Data", "Methodology", "About"]
    rows = _data(book)
    assert [r["cusip"] for r in rows] == [r["cusip"] for r in csv_rows] == [ALPHA, CHARLIE]
    assert list(rows[0]) == list(csv_rows[0])

    alpha = rows[0]
    assert alpha["value_usd"] == 1234567000
    assert alpha["shares"] == 1234567
    assert alpha["is_split_adjusted"] is False
    assert alpha["implied_price"] == 1000
    assert alpha["filed_at"] == filed[Q2].astimezone(UTC).replace(tzinfo=None)
    assert book["Data"]["A2"].data_type == "s"

    about = _about(book)
    assert about["Export"] == (
        "WhaleWatch export: BERKSHIRE holdings in 2024Q2 (the quarter ended 2024-06-30)"
    )
    assert about["Period"] == "2024Q2 (the quarter ended 2024-06-30)"
    assert about["Coverage"] == "1 of 1 investors have filed for 2024Q2."
    assert about["EDGAR"].startswith("https://www.sec.gov/")
    assert about["Page"].endswith("/investors/berkshire?period=2024Q2")
    assert "45 days" in about["Disclaimer"]
    assert isinstance(about["Generated at"], datetime)
    assert {"Implied price", "Confidence scale", "Split adjustment"} <= set(_methodology(book))

    exports = await db_session.scalar(
        select(UserActivityDay.data_exports).where(UserActivityDay.user_id == pro)
    )
    assert exports == 2


async def test_each_workbook_matches_its_csv(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    filed = await _two_quarters(db_session)
    await _quarter(db_session, filed["filer"], Q3, held(CHARLIE, 60), days_later=30)
    await _publish(db_session)

    for path, key in (
        ("/v1/investors/berkshire/activity", ("cusip", "period")),
        (f"/v1/stocks/{ALPHA}/owners", ("slug",)),
        ("/v1/flows", ("cusip",)),
    ):
        response = await client.get(f"{path}.xlsx")
        book = _workbook(response)
        _, csv_rows = _file(await client.get(f"{path}.csv"))
        rows = _data(book)
        assert [tuple(str(r[k])[:10] for k in key) for r in rows] == [
            tuple(r[k] for k in key) for r in csv_rows
        ], path
        assert _named(response).endswith(".xlsx")

    flows = _workbook(await client.get("/v1/flows.xlsx"))
    assert "Screener figures" in _methodology(flows)
    assert _about(flows)["Aggregates as of"] != "unrecorded"
    activity = _about(_workbook(await client.get("/v1/investors/berkshire/activity.xlsx")))
    # The newest quarter in the file's.
    assert activity["Coverage"] == "1 of 1 investors have filed for 2024Q3."
    assert activity["Actions"] == "add; exit; new; trim"


async def test_the_insider_workbook_shows_a_formula_as_text(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    nvda = await _security(db_session, NVDA_CUSIP, "NVDA")
    mischief = await _insider(db_session, '=HYPERLINK("http://evil.example","x")')
    await _form4(db_session, mischief, _line("P", DAY_1), security_id=nvda, is_director=True)

    response = await client.get("/v1/insiders/transactions.xlsx", params={"ticker": "NVDA"})
    book = _workbook(response)

    assert _named(response) == f"nvda_to-{utc_today()}_trades.xlsx"
    [line] = _data(book)
    # Text, not a formula, and not quoted as the CSV must.
    assert line["insider_name"] == '=HYPERLINK("http://evil.example","x")'
    assert line["cusip"] == NVDA_CUSIP
    assert (line["confidence"], line["observation_count"]) == (None, None)
    about = _about(book)
    assert about["Coverage"] == NO_COVERAGE
    assert about["Codes"].endswith("(the default)")
    assert "two business days" in about["Disclaimer"]
    headings = _methodology(book)
    assert "Transaction codes" in headings
    assert "Implied price" not in headings


async def test_an_empty_workbook_has_its_header_and_its_sheets(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    book = _workbook(
        await client.get("/v1/insiders/transactions.xlsx", params={"min_value": str(10**12)})
    )
    assert _data(book) == []
    assert book["Data"]["A1"].value == "accession_no"
    assert book.sheetnames == ["Data", "Methodology", "About"]
