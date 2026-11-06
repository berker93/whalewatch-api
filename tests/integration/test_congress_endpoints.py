"""CON-7, against a real Postgres: ``/v1/congress/transactions``, a member's page,
and ``/v1/stocks/{ticker}/congress``.

Members, names, filings and lines are written straight into the tables, as the
seed, the index runs and the PTR readers leave them, so each test says exactly
which dates, bands and statuses it has. What matters is what a reader could be
misled by: a lag that is not the one it says, a trade counted twice because a
Senate amendment restates it, a line nobody could read shown as a trade, a
member's party today put on a trade they made in another, a midpoint passed off
as an amount, a member left out without a word because their filing is a scan.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import admin_store
from app.api.deps import get_redis
from app.core.config import Settings
from app.db.models import (
    CongressFiledName,
    CongressFiling,
    CongressMember,
    CongressMemberTerm,
    CongressTransaction,
    Security,
)
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND
from tests.integration.test_admin import admin_in, make_admin

FEED = "/v1/congress/transactions"
NVDA_CUSIP = "67066G104"

_docs = count(20030000)


async def _member(
    session: AsyncSession,
    bioguide: str,
    first: str,
    last: str,
    *terms: tuple[str, date, date, str],
    chamber: str = "house",
    state: str = "CA",
) -> int:
    """A member, with ``terms`` of ``(chamber, start, end, party)`` in ``state``.
    Their row carries the latest term's."""
    latest = max(terms, key=lambda term: term[1])
    member_id = await session.scalar(
        insert(CongressMember)
        .values(
            bioguide_id=bioguide,
            full_name=f"{first} {last}",
            first_name=first,
            last_name=last,
            chamber=latest[0],
            state=state,
            district=11 if latest[0] == "house" else None,
            party=latest[3],
            current=True,
            source_key="raw/congress-legislators/legislators-current/x.json",
        )
        .returning(CongressMember.id)
    )
    assert member_id is not None
    for term_chamber, start, end, party in terms:
        await session.execute(
            insert(CongressMemberTerm).values(
                member_id=member_id,
                chamber=term_chamber,
                start_date=start,
                end_date=end,
                state=state,
                district=11 if term_chamber == "house" else None,
                party=party,
            )
        )
    return member_id


async def _name(
    session: AsyncSession,
    first: str,
    last: str,
    member_id: int | None,
    *,
    chamber: str = "house",
    status: str | None = None,
) -> int:
    name_id = await session.scalar(
        insert(CongressFiledName)
        .values(
            chamber=chamber,
            state="CA" if chamber == "house" else None,
            name_key=f"{first} {last}".lower(),
            first_name=first,
            last_name=last,
            status=status or ("matched" if member_id is not None else "unmatched"),
            member_id=member_id,
            method=None if member_id is None else "exact",
            confidence=None if member_id is None else 1.0,
        )
        .returning(CongressFiledName.id)
    )
    assert name_id is not None
    return name_id


async def _filing(
    session: AsyncSession,
    name_id: int | None,
    filed: date,
    *,
    first: str = "Nancy",
    last: str = "Pelosi",
    chamber: str = "house",
    report_date: date | None = None,
    amendment: int | None = None,
    read: bool = True,
) -> tuple[int, str]:
    doc_id = str(next(_docs))
    filing_id = await session.scalar(
        insert(CongressFiling)
        .values(
            chamber=chamber,
            doc_id=doc_id,
            report_type="ptr",
            prefix="Hon.",
            first_name=first,
            last_name=last,
            office="CA11" if chamber == "house" else "Senator",
            filing_date=filed,
            year=filed.year,
            report_date=report_date,
            amendment=amendment,
            source_key="raw/house-clerk/index/2026/x.zip",
            document_url=f"https://disclosures-clerk.house.gov/{doc_id}.pdf",
            parser_version=1 if read else None,
            parsed_at=datetime(2026, 11, 4, tzinfo=UTC) if read else None,
            extraction="text" if read else None,
            filed_name_id=name_id,
        )
        .returning(CongressFiling.id)
    )
    assert filing_id is not None
    return filing_id, doc_id


async def _lines(session: AsyncSession, filing_id: int, *lines: dict[str, Any]) -> None:
    await session.execute(
        insert(CongressTransaction).values(
            [
                {"filing_id": filing_id, "line": number, **line}
                for number, line in enumerate(lines, 1)
            ]
        )
    )


def _trade(
    traded: date,
    *,
    notified: date | None = None,
    kind: str = "purchase",
    band: tuple[int | None, int | None] = (1_001, 15_000),
    exact: str | None = None,
    asset: str = "Apple Inc. - Common Stock",
    security_id: int | None = None,
    asset_class: str = "equity",
    status: str = "parsed",
    filing_status: str | None = "new",
    owner: str | None = None,
) -> dict[str, Any]:
    low, high = (None, None) if exact is not None else band
    return {
        "parse_status": status,
        "extraction": "text",
        "owner": owner,
        "asset_name": asset,
        "asset_type": "OP" if asset_class == "stock_option" else "ST",
        "transaction_type": kind,
        "transaction_date": traded,
        "notification_date": traded if notified is None else notified,
        "amount_low": low,
        "amount_high": high,
        "amount_exact": None if exact is None else Decimal(exact),
        "filing_status": filing_status,
        "asset_class": asset_class,
        "security_id": security_id,
        "resolution_method": None if security_id is None else "ticker",
        "resolution_confidence": None if security_id is None else Decimal("1.000"),
        "resolved_at": datetime(2026, 11, 4, tzinfo=UTC),
    }


def _placeholder() -> dict[str, Any]:
    return {"parse_status": "placeholder", "extraction": "none", "problems": ["a scan"]}


async def _security(session: AsyncSession, cusip: str, ticker: str) -> int:
    security_id = await session.scalar(
        insert(Security)
        .values(cusip=cusip, name=f"ISSUER {ticker}", ticker=ticker, resolution_source="manual")
        .returning(Security.id)
    )
    assert security_id is not None
    return security_id


async def _pelosi(session: AsyncSession) -> tuple[int, int]:
    """Pelosi, a member since 1987 and a Democrat throughout: her id and her name's."""
    member_id = await _member(
        session,
        "P000197",
        "Nancy",
        "Pelosi",
        ("house", date(2023, 1, 3), date(2025, 1, 3), "Democrat"),
        ("house", date(2025, 1, 3), date(2027, 1, 3), "Democrat"),
    )
    return member_id, await _name(session, "Nancy", "Pelosi", member_id)


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _walk(client: AsyncClient, path: str, **params: Any) -> list[dict[str, Any]]:
    """Every page, two rows at a time."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        extra: dict[str, str] = {"cursor": cursor} if cursor else {}
        body = await _get(client, path, limit=2, **params, **extra)
        rows += body["data"]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return rows


def _assets(rows: list[dict[str, Any]]) -> list[str]:
    return [row["asset_name"] for row in rows]


# --- the lags ---------------------------------------------------------------------


async def test_each_row_says_how_late_it_was_disclosed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Due 30 days after the member was notified, and never more than 45 after the
    trade: notified the same day, 30 days; notified late, whichever comes first."""
    _, name = await _pelosi(db_session)
    filing, _ = await _filing(db_session, name, date(2026, 8, 30))
    await _lines(
        db_session,
        filing,
        _trade(date(2026, 6, 20), asset="Seventy-one days"),
        _trade(date(2026, 7, 1), notified=date(2026, 7, 10), asset="Due 30 after notice"),
        _trade(date(2026, 7, 1), notified=date(2026, 7, 25), asset="Due 45 after trade"),
        _trade(date(2026, 8, 1), notified=date(2026, 8, 25), asset="In time"),
    )

    rows = {row["asset_name"]: row for row in (await _get(client, FEED))["data"]}

    late = rows["Seventy-one days"]
    assert late["notification_lag_days"] == 0
    assert late["disclosure_lag_days"] == 71
    assert late["disclosure_deadline"] == "2026-07-20"
    assert (late["days_late"], late["late"]) == (41, True)
    notice = rows["Due 30 after notice"]
    assert notice["notification_lag_days"] == 9
    assert notice["disclosure_deadline"] == "2026-08-09"
    assert (notice["days_late"], notice["late"]) == (21, True)
    trade = rows["Due 45 after trade"]
    assert trade["disclosure_deadline"] == "2026-08-15"
    assert (trade["days_late"], trade["late"]) == (15, True)
    in_time = rows["In time"]
    assert in_time["notification_lag_days"] == 24
    assert in_time["disclosure_lag_days"] == 29
    assert in_time["disclosure_deadline"] == "2026-09-15"
    assert (in_time["days_late"], in_time["late"]) == (0, False)


async def test_a_lag_that_would_not_be_the_fact_it_claims_is_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An amending line's filing is not the first to disclose the trade; a
    notification before the trade, or a trade after the filing, is a typo."""
    _, name = await _pelosi(db_session)
    filing, _ = await _filing(db_session, name, date(2026, 8, 30))
    await _lines(
        db_session,
        filing,
        _trade(date(2025, 1, 7), filing_status="amended", asset="Amending"),
        _trade(date(2026, 8, 1), notified=date(2026, 7, 1), asset="Notified before"),
        _trade(date(2026, 9, 2), asset="Traded after filing"),
    )

    rows = {row["asset_name"]: row for row in (await _get(client, FEED))["data"]}

    amending = rows["Amending"]
    assert amending["filing_status"] == "amended"
    for field in ("disclosure_lag_days", "disclosure_deadline", "days_late", "late"):
        assert amending[field] is None, field
    assert amending["notification_lag_days"] == 0
    before = rows["Notified before"]
    assert before["notification_lag_days"] is None
    assert before["disclosure_lag_days"] == 29
    assert before["disclosure_deadline"] == "2026-09-15"  # from the trade alone
    assert rows["Traded after filing"]["disclosure_lag_days"] is None
    assert rows["Traded after filing"]["late"] is None


# --- which lines -----------------------------------------------------------------


async def test_only_lines_read_surely_are_trades(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, name = await _pelosi(db_session)
    filing, _ = await _filing(db_session, name, date(2026, 8, 30))
    await _lines(
        db_session,
        filing,
        _trade(date(2026, 8, 1), asset="Parsed"),
        _trade(date(2026, 8, 1), asset="In review", status="needs_review"),
        _trade(date(2026, 8, 1), asset="Rejected", status="rejected"),
        _trade(date(2026, 8, 1), asset="Deleted", filing_status="deleted"),
    )
    scan, _ = await _filing(db_session, name, date(2026, 8, 31))
    await _lines(db_session, scan, _placeholder())

    body = await _get(client, FEED)

    assert _assets(body["data"]) == ["Parsed"]
    assert body["meta"]["summary"]["transactions"] == 1


async def test_a_senate_amendment_replaces_the_report_it_restates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both list the trade; it is in the rows once, as the amendment has it. An
    amendment with nothing readable replaces nothing."""
    member_id = await _member(
        db_session,
        "W000437",
        "Roger",
        "Wicker",
        ("senate", date(2025, 1, 3), date(2031, 1, 3), "Republican"),
        chamber="senate",
        state="MS",
    )
    name = await _name(db_session, "Roger", "Wicker", member_id, chamber="senate")
    day = date(2026, 3, 1)

    async def report(filed: date, amendment: int | None = None) -> tuple[int, str]:
        return await _filing(
            db_session,
            name,
            filed,
            first="Roger",
            last="Wicker",
            chamber="senate",
            report_date=day,
            amendment=amendment,
        )

    original, _ = await report(date(2026, 3, 20))
    await _lines(db_session, original, _trade(date(2026, 3, 2), asset="As first filed"))
    amended, amended_doc = await report(date(2026, 4, 2), amendment=1)
    await _lines(db_session, amended, _trade(date(2026, 3, 2), asset="As amended"))
    unread, _ = await report(date(2026, 5, 1), amendment=2)
    await _lines(db_session, unread, _placeholder())

    rows = (await _get(client, FEED))["data"]

    assert _assets(rows) == ["As amended"]
    assert rows[0]["doc_id"] == amended_doc
    assert rows[0]["amendment"] == 1
    assert rows[0]["disclosure_lag_days"] is None  # the original disclosed it


async def test_a_filer_not_known_to_be_a_member_is_shown_flagged(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _pelosi(db_session)
    stranger = await _name(db_session, "Zebulon", "Quartermaine", None)
    filing, _ = await _filing(
        db_session, stranger, date(2026, 8, 30), first="Zebulon", last="Quartermaine"
    )
    await _lines(db_session, filing, _trade(date(2026, 8, 1)))
    unlinked, _ = await _filing(db_session, None, date(2026, 8, 29), first="Ann", last="Other")
    await _lines(db_session, unlinked, _trade(date(2026, 8, 1)))

    body = await _get(client, FEED)

    assert [(row["filer_name"], row["member"], row["member_match"]) for row in body["data"]] == [
        ("Hon. Zebulon Quartermaine", None, "unmatched"),
        ("Hon. Ann Other", None, "unlinked"),
    ]
    assert body["meta"]["summary"]["unmatched_filers"] == 2
    assert body["meta"]["summary"]["members"] == 0
    assert (await _get(client, FEED, party="democrat"))["data"] == []


async def test_a_trade_carries_the_party_the_member_was_in_that_day(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    member_id = await _member(
        db_session,
        "V000133",
        "Jefferson",
        "Van Drew",
        ("house", date(2019, 1, 3), date(2021, 1, 3), "Democrat"),
        ("house", date(2021, 1, 3), date(2027, 1, 3), "Republican"),
        state="NJ",
    )
    name = await _name(db_session, "Jefferson", "Van Drew", member_id)
    filing, _ = await _filing(db_session, name, date(2026, 8, 30), last="Van Drew")
    await _lines(
        db_session,
        filing,
        _trade(date(2020, 6, 1), asset="As a Democrat"),
        _trade(date(2026, 8, 1), asset="As a Republican"),
    )

    rows = (await _get(client, FEED, sort="traded"))["data"]

    assert [(row["asset_name"], row["member"]["party"]) for row in rows] == [
        ("As a Republican", "Republican"),
        ("As a Democrat", "Democrat"),
    ]
    assert _assets((await _get(client, FEED, party="Democrat"))["data"]) == ["As a Democrat"]


# --- the filters and the order -----------------------------------------------------


@pytest.fixture
async def a_season(db_session: AsyncSession) -> dict[str, int]:
    """Pelosi and a senator, each with a few trades of each kind, NVDA among them."""
    nvda = await _security(db_session, NVDA_CUSIP, "NVDA")
    pelosi, pelosi_name = await _pelosi(db_session)
    senator = await _member(
        db_session,
        "T000278",
        "Tommy",
        "Tuberville",
        ("senate", date(2021, 1, 3), date(2027, 1, 3), "Republican"),
        chamber="senate",
        state="AL",
    )
    senator_name = await _name(db_session, "Tommy", "Tuberville", senator, chamber="senate")
    house, _ = await _filing(db_session, pelosi_name, date(2026, 7, 10))
    await _lines(
        db_session,
        house,
        _trade(
            date(2026, 6, 20),
            asset="NVDA calls",
            security_id=nvda,
            asset_class="stock_option",
            band=(1_000_001, 5_000_000),
            owner="SP",
        ),
        _trade(
            date(2026, 6, 21),
            asset="NVDA shares",
            security_id=nvda,
            kind="sale",
            band=(5_000_001, 25_000_000),
        ),
        _trade(
            date(2026, 6, 22),
            asset="Muni",
            asset_class="municipal_bond",
            kind="partial_sale",
            band=(50_001, 100_000),
        ),
    )
    senate, _ = await _filing(
        db_session,
        senator_name,
        date(2026, 9, 1),
        first="Tommy",
        last="Tuberville",
        chamber="senate",
        report_date=date(2026, 9, 1),
    )
    await _lines(
        db_session,
        senate,
        _trade(date(2026, 5, 1), asset="Late NVDA", security_id=nvda, band=(15_001, 50_000)),
        _trade(date(2026, 8, 20), asset="Exact figure", exact="710.52"),
        _trade(date(2026, 8, 21), asset="Huge", band=(50_000_001, None), kind="exchange"),
    )
    return {"nvda": nvda, "pelosi": pelosi}


async def test_the_default_order_is_latest_disclosed(
    client: AsyncClient, a_season: dict[str, int]
) -> None:
    assert _assets(await _walk(client, FEED)) == [
        "Huge",
        "Exact figure",
        "Late NVDA",
        "Muni",
        "NVDA shares",
        "NVDA calls",
    ]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("traded", ["Huge", "Exact figure", "Muni", "NVDA shares", "NVDA calls", "Late NVDA"]),
        ("amount", ["Huge", "NVDA shares", "NVDA calls", "Muni", "Late NVDA", "Exact figure"]),
        ("lag", ["Late NVDA", "NVDA calls", "NVDA shares", "Muni", "Exact figure", "Huge"]),
    ],
)
async def test_every_sort_pages_through_every_line_once(
    client: AsyncClient, a_season: dict[str, int], sort: str, expected: list[str]
) -> None:
    assert _assets(await _walk(client, FEED, sort=sort)) == expected
    assert _assets(await _walk(client, FEED, sort=sort, order="asc")) == expected[::-1]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"chamber": "senate"}, ["Huge", "Exact figure", "Late NVDA"]),
        ({"member": "p000197"}, ["Muni", "NVDA shares", "NVDA calls"]),
        ({"party": "republican"}, ["Huge", "Exact figure", "Late NVDA"]),
        ({"ticker": "NVDA"}, ["Late NVDA", "NVDA shares", "NVDA calls"]),
        ({"type": "sale"}, ["Muni", "NVDA shares"]),
        ({"type": "partial_sale"}, ["Muni"]),
        ({"type": "purchase,exchange"}, ["Huge", "Exact figure", "Late NVDA", "NVDA calls"]),
        ({"asset_class": "stock_option"}, ["NVDA calls"]),
        ({"min_amount": 1_000_001}, ["Huge", "NVDA shares", "NVDA calls"]),
        (
            {"min_amount": 700},
            ["Huge", "Exact figure", "Late NVDA", "Muni", "NVDA shares", "NVDA calls"],
        ),
        ({"from": "2026-06-21", "to": "2026-08-20"}, ["Exact figure", "Muni", "NVDA shares"]),
        ({"disclosed_from": "2026-08-01"}, ["Huge", "Exact figure", "Late NVDA"]),
        ({"late": "true"}, ["Late NVDA"]),
        ({"late": "false", "chamber": "senate"}, ["Huge", "Exact figure"]),
    ],
)
async def test_the_filters(
    client: AsyncClient, a_season: dict[str, int], params: dict[str, Any], expected: list[str]
) -> None:
    assert _assets(await _walk(client, FEED, **params)) == expected


@pytest.mark.parametrize(
    "params",
    [
        {"type": "bought"},
        {"member": "Pelosi"},
        {"asset_class": "stocks"},
        {"chamber": "both"},
        {"from": "2026-07-01", "to": "2026-06-01"},
        {"disclosed_from": "2026-07-01", "disclosed_to": "2026-06-01"},
        {"min_amount": -1},
        {"sort": "midpoint"},
    ],
)
async def test_a_filter_that_means_nothing_is_a_422(
    client: AsyncClient, params: dict[str, Any]
) -> None:
    response = await client.get(FEED, params=params)
    assert response.status_code == 422, response.text


async def test_an_unknown_ticker_is_a_404_with_suggestions(client: AsyncClient) -> None:
    response = await client.get(FEED, params={"ticker": "NOPE"})
    assert response.status_code == 404
    assert (await client.get("/v1/stocks/NOPE/congress")).status_code == 404


# --- the summary ------------------------------------------------------------------


async def test_the_summary_is_ranges_over_every_page(
    client: AsyncClient, a_season: dict[str, int]
) -> None:
    """Band ends added up; an exact figure on both sides, rounded outwards; an open
    band leaves no most, and says so. Never a midpoint."""
    body = await _get(client, FEED, limit=1)

    summary = body["meta"]["summary"]
    assert len(body["data"]) == 1
    assert (summary["transactions"], summary["filings"], summary["members"]) == (6, 2, 2)
    assert summary["by_type"] == [
        {
            "type": "purchase",
            "transactions": 3,
            "amount_low": 1_000_001 + 15_001 + 710,
            "amount_high": 5_000_000 + 50_000 + 711,
            "open_ended": 0,
        },
        {
            "type": "sale",
            "transactions": 2,
            "amount_low": 5_000_001 + 50_001,
            "amount_high": 25_000_000 + 100_000,
            "open_ended": 0,
        },
        {
            "type": "exchange",
            "transactions": 1,
            "amount_low": 50_000_001,
            "amount_high": None,
            "open_ended": 1,
        },
    ]
    assert (summary["first_transaction_date"], summary["last_transaction_date"]) == (
        "2026-05-01",
        "2026-08-21",
    )
    assert (summary["late"], summary["lag_known"]) == (1, 6)
    # 11, 12, 18, 19, 20, 123: the lower middle, a real line's.
    assert summary["median_disclosure_lag_days"] == 18
    assert summary["max_disclosure_lag_days"] == 123


# --- coverage --------------------------------------------------------------------


async def test_coverage_counts_what_the_rows_cannot_include(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    sec = await _security(db_session, NVDA_CUSIP, "NVDA")
    _, name = await _pelosi(db_session)
    stranger = await _name(db_session, "Zebulon", "Quartermaine", None)
    read, _ = await _filing(db_session, name, date(2026, 8, 30))
    await _lines(
        db_session,
        read,
        _trade(date(2026, 8, 1), security_id=sec),
        _trade(date(2026, 8, 1), asset="Something", asset_class="unknown"),
        _trade(date(2026, 8, 1), status="needs_review"),
    )
    scan, _ = await _filing(db_session, name, date(2026, 9, 2))
    await _lines(db_session, scan, _placeholder())
    in_review, _ = await _filing(db_session, stranger, date(2026, 9, 3), last="Quartermaine")
    await _lines(db_session, in_review, _trade(date(2026, 8, 1), status="needs_review"))
    await _filing(db_session, name, date(2026, 9, 4), read=False)

    body = await _get(client, FEED)

    house, senate = body["meta"]["congress_coverage"]
    assert house == {
        "chamber": "house",
        "filings": 4,
        "filings_read": 1,
        "filings_unreadable": 2,
        "filings_pending": 1,
        "lines_in_review": 2,
        "filings_unmatched_filer": 1,
        "assets_resolved_pct": "50.0",
        "first_filing_date": "2026-08-30",
        "latest_filing_date": "2026-09-04",
        "last_read_at": "2026-11-04T00:00:00Z",
    }
    assert (senate["chamber"], senate["filings"], senate["assets_resolved_pct"]) == (
        "senate",
        0,
        None,
    )
    only_senate = await _get(client, FEED, chamber="senate")
    assert [each["chamber"] for each in only_senate["meta"]["congress_coverage"]] == ["senate"]


# --- a member's page ----------------------------------------------------------------


async def test_a_members_page_is_who_they_are_what_they_traded_and_what_is_missing(
    client: AsyncClient, db_session: AsyncSession, a_season: dict[str, int]
) -> None:
    """A member whose filing is a scan is not left out without a word: it is listed,
    with the link to the original."""
    name = await _name(db_session, "Nancy P", "Pelosi", a_season["pelosi"])
    scan, scan_doc = await _filing(db_session, name, date(2026, 9, 15))
    await _lines(db_session, scan, _placeholder())
    _, pending_doc = await _filing(db_session, name, date(2026, 9, 20), read=False)

    body = await _get(client, "/v1/congress/members/p000197")

    assert _assets(body["data"]) == ["Muni", "NVDA shares", "NVDA calls"]
    meta = body["meta"]
    assert meta["member"]["bioguide_id"] == "P000197"
    assert meta["member"]["name"] == "Nancy Pelosi"
    assert [term["start_date"] for term in meta["member"]["terms"]] == [
        "2025-01-03",
        "2023-01-03",
    ]
    assert [(f["doc_id"], f["reason"]) for f in meta["unread_filings"]] == [
        (pending_doc, "pending"),
        (scan_doc, "unreadable"),
    ]
    assert meta["unread_filings"][1]["document_url"].endswith(f"{scan_doc}.pdf")
    assert meta["summary"]["transactions"] == 3
    [house, senate] = meta["congress_coverage"]
    assert (house["filings"], house["filings_read"], house["filings_unreadable"]) == (3, 1, 1)
    assert senate["filings"] == 0
    owners = {row["asset_name"]: row["owner"] for row in body["data"]}
    assert owners == {"Muni": "self", "NVDA shares": "self", "NVDA calls": "spouse"}


async def test_a_members_page_takes_the_feeds_filters(
    client: AsyncClient, a_season: dict[str, int]
) -> None:
    body = await _get(client, "/v1/congress/members/P000197", ticker="NVDA", type="sale")
    assert _assets(body["data"]) == ["NVDA shares"]
    assert body["meta"]["summary"]["transactions"] == 1


async def test_an_unknown_member_is_a_404(client: AsyncClient) -> None:
    response = await client.get("/v1/congress/members/X999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert (await client.get("/v1/congress/members/P00019")).status_code == 422


# --- a stock's ------------------------------------------------------------------


async def test_a_stocks_congress_lines_are_its_shares_and_options_on_it(
    client: AsyncClient, a_season: dict[str, int]
) -> None:
    body = await _get(client, "/v1/stocks/nvda/congress")

    assert [
        (row["asset_name"], row["asset_class"], row["cusip"], row["ticker"]) for row in body["data"]
    ] == [
        ("Late NVDA", "equity", NVDA_CUSIP, "NVDA"),
        ("NVDA shares", "equity", NVDA_CUSIP, "NVDA"),
        ("NVDA calls", "stock_option", NVDA_CUSIP, "NVDA"),
    ]
    summary = body["meta"]["summary"]
    assert [
        (each["type"], each["amount_low"], each["amount_high"]) for each in summary["by_type"]
    ] == [
        ("purchase", 1_015_002, 5_050_000),
        ("sale", 5_000_001, 25_000_000),
    ]
    assert len(body["meta"]["congress_coverage"]) == 2
    equity = await _get(client, "/v1/stocks/NVDA/congress", asset_class="equity", chamber="house")
    assert _assets(equity["data"]) == ["NVDA shares"]


# --- the cache ------------------------------------------------------------------


@pytest.fixture
async def admin(
    app: FastAPI,
    client: AsyncClient,
    db_session: AsyncSession,
    session_redis: FakeRedis,
    settings: Settings,
) -> int:
    app.state.admin_sessions = admin_store(session_redis, settings)  # type: ignore[arg-type]
    admin_id = await make_admin(db_session)
    assert (await admin_in(client)).status_code == 200
    return admin_id


async def test_a_line_accepted_in_review_is_in_the_feed_at_once(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    """The feed is cached; the admin's decision drops it."""
    cache = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: cache
    _, name = await _pelosi(db_session)
    filing, _ = await _filing(db_session, name, date(2026, 8, 30))
    await _lines(db_session, filing, _trade(date(2026, 8, 1), status="needs_review"))
    assert (await _get(client, FEED))["data"] == []
    line_id = await db_session.scalar(
        select(CongressTransaction.id).where(CongressTransaction.filing_id == filing)
    )

    response = await client.post(
        f"/admin/congress/review/{line_id}", json={"decision": "accept"}, headers=FRONTEND
    )

    assert response.status_code == 200, response.text
    assert len((await _get(client, FEED))["data"]) == 1
