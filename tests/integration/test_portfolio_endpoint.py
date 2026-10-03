"""``/v1/investors/{slug}/portfolio``, ``/activity`` and ``/history``, against a real Postgres.

Every test publishes the way an operator does, ``recompute`` then
``refresh-views``, unless it is testing what happens between the two. The
cases that matter are the ones where a figure could come from the wrong
place: the universe's latest period instead of the filer's, another filer's
filing date, a first held period reset by a gap, an option line added into
the weights, a first period read as a quarter of buying. Then paging under
every sort and order, with ties at page boundaries.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import STALE_WHILE_REVALIDATE, invalidate
from app.api.deps import get_redis
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views
from tests.fake_redis import FakeRedis

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"
DELTA = "44444D404"

_ciks = count(1)
_accessions = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal
    price: Decimal
    put_call: str | None
    sshprnamt_type: str


def held(
    cusip: str, shares: int, *, price: int = 10, put_call: str | None = None, prn: bool = False
) -> _Held:
    return _Held(cusip, Decimal(shares), Decimal(price), put_call, "PRN" if prn else "SH")


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
    *positions: _Held,
    suspect: bool = False,
    days_later: int = 45,
    amends: AmendmentKind | None = None,
    unclassified: bool = False,
) -> datetime:
    """A 13F-HR for ``period`` holding ``positions``, as loaded; or with
    ``amends``, a 13F-HR/A of that kind; or with ``unclassified``, a 13F-HR/A
    whose cover page gave no ``amendmentType``. Returns when it was filed."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    filed_at = datetime(period.year, period.month, period.day, 16, tzinfo=UTC) + timedelta(
        days=days_later
    )
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-{period:%y}-{next(_accessions):06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR/A" if amends or unclassified else "13F-HR",
            amendment_kind=amends,
            period_of_report=period,
            filed_at=filed_at,
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
    for position in positions:
        await session.execute(
            pg_insert(Security)
            .values(cusip=position.cusip, name=f"ISSUER {position.cusip}")
            .on_conflict_do_nothing(index_elements=[Security.cusip])
        )
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id)
                .where(Security.cusip == position.cusip)
                .scalar_subquery(),
                filer_id=filer_id,
                period_of_report=period,
                cusip=position.cusip,
                value_usd=position.shares * position.price,
                shares=position.shares,
                sshprnamt_type=position.sshprnamt_type,
                put_call=position.put_call,
            )
        )
    return filed_at


async def _publish(session: AsyncSession) -> None:
    """``recompute --all``, then ``refresh-views``."""
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _get(client: AsyncClient, path: str, **params: Any) -> dict[str, Any]:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


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


def _at(filed: datetime) -> str:
    return filed.isoformat().replace("+00:00", "Z")


# --- the portfolio: one row ------------------------------------------------------


async def test_a_position_carries_its_change_and_its_first_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    # ALPHA added to, BRAVO exited (so not in the portfolio), CHARLIE opened.
    filed = await _quarter(db_session, filer, Q2, held(ALPHA, 120), held(CHARLIE, 30))
    await _publish(db_session)

    body = await _get(client, "/v1/investors/berkshire/portfolio")

    assert body["data"] == [
        {
            "cusip": ALPHA,
            "ticker": None,
            "issuer_name": f"ISSUER {ALPHA}",
            "put_call": None,
            "shares": "120.0000",
            "value_usd": "1200.00",
            "weight_pct": "80.000000",
            "shares_delta": "20.0000",
            "shares_delta_pct": "20.000000",
            "weight_delta": "13.333333",
            "action": "add",
            "first_period": "2024-03-31",
        },
        {
            "cusip": CHARLIE,
            "ticker": None,
            "issuer_name": f"ISSUER {CHARLIE}",
            "put_call": None,
            "shares": "30.0000",
            "value_usd": "300.00",
            "weight_pct": "20.000000",
            # Counted from nothing, with no percentage of nothing.
            "shares_delta": "30.0000",
            "shares_delta_pct": None,
            "weight_delta": "20.000000",
            "action": "new",
            "first_period": "2024-06-30",
        },
    ]
    assert body["meta"]["period"] == "2024Q2"
    assert body["meta"]["period_end"] == "2024-06-30"
    assert body["meta"]["latest_filing_at"] == _at(filed)
    assert body["page"] == {"limit": 50, "next_cursor": None}


async def test_an_unresolved_security_is_named_with_a_null_ticker_and_a_resolved_one_tickered(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "named")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    await db_session.execute(update(Security).where(Security.cusip == ALPHA).values(ticker="ALF"))
    await _publish(db_session)

    rows = (await _get(client, "/v1/investors/named/portfolio"))["data"]

    assert [(row["cusip"], row["ticker"], row["issuer_name"]) for row in rows] == [
        (ALPHA, "ALF", f"ISSUER {ALPHA}"),
        (BRAVO, None, f"ISSUER {BRAVO}"),
    ]
    # Unresolved is not a lesser row: every figure is there.
    assert rows[1]["value_usd"] == "500.00"
    assert rows[1]["weight_pct"] == "33.333333"


async def test_first_period_is_the_first_ever_not_reset_by_a_quarter_unheld(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "round-trip")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 10))
    await _quarter(db_session, filer, Q2, held(BRAVO, 10))
    await _quarter(db_session, filer, Q3, held(ALPHA, 100), held(BRAVO, 10), held(CHARLIE, 5))
    await _publish(db_session)

    rows = (await _get(client, "/v1/investors/round-trip/portfolio"))["data"]
    by_cusip = {row["cusip"]: row for row in rows}

    # Sold out in Q2 and bought back in Q3: new again, but first held in Q1.
    assert by_cusip[ALPHA]["action"] == "new"
    assert by_cusip[ALPHA]["first_period"] == "2024-03-31"
    assert by_cusip[BRAVO]["first_period"] == "2024-03-31"
    assert by_cusip[CHARLIE]["first_period"] == "2024-09-30"


async def test_first_period_is_never_after_the_period_asked_for(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "steady")
    for period in (Q1, Q2, Q3):
        await _quarter(db_session, filer, period, held(ALPHA, 100))
    await _publish(db_session)

    for label in ("2024Q1", "2024Q2", "2024Q3"):
        (row,) = (await _get(client, "/v1/investors/steady/portfolio", period=label))["data"]
        assert row["first_period"] == "2024-03-31"


async def test_meta_dates_the_portfolio_by_this_filers_filing_not_the_universes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    early = await _fund(db_session, "early")
    late = await _fund(db_session, "late")
    filed = await _quarter(db_session, early, Q1, held(ALPHA, 1), days_later=20)
    await _quarter(db_session, late, Q1, held(ALPHA, 1), days_later=44)
    await _publish(db_session)

    meta = (await _get(client, "/v1/investors/early/portfolio"))["meta"]

    assert meta["latest_filing_at"] == _at(filed)
    assert meta["coverage"] == {"filers_reported": 2, "filers_tracked": 2}


async def test_meta_warns_of_an_amendment_left_out_on_every_page(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "vague")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    await _quarter(db_session, filer, Q1, held(CHARLIE, 10), days_later=90, unclassified=True)
    await _quarter(db_session, filer, Q2, held(ALPHA, 100))
    await _publish(db_session)
    path = "/v1/investors/vague/portfolio"

    first = await _get(client, path, period="2024Q1", limit=1)
    second = await _get(client, path, period="2024Q1", limit=1, cursor=first["page"]["next_cursor"])

    (caveat,) = first["meta"]["caveats"]
    assert "13F-HR/A with no amendmentType" in caveat
    assert second["meta"]["caveats"] == first["meta"]["caveats"]
    # Left out, as the caveat says: the period is the original alone.
    assert [row["cusip"] for row in first["data"] + second["data"]] == [ALPHA, BRAVO]
    # A period with one filing has nothing to warn of.
    assert (await _get(client, path))["meta"]["caveats"] == []


# --- the portfolio: which period -------------------------------------------------


async def test_the_default_is_the_filers_latest_period_not_the_universes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    behind = await _fund(db_session, "behind")
    ahead = await _fund(db_session, "ahead")
    await _quarter(db_session, behind, Q1, held(ALPHA, 100))
    await _quarter(db_session, ahead, Q1, held(ALPHA, 1))
    await _quarter(db_session, ahead, Q2, held(ALPHA, 2))
    await _publish(db_session)

    body = await _get(client, "/v1/investors/behind/portfolio")

    assert body["meta"]["period"] == "2024Q1"
    assert body["data"][0]["shares"] == "100.0000"


async def test_the_default_steps_over_a_withheld_latest_filing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "careful")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _quarter(db_session, filer, Q2, held(ALPHA, 999), suspect=True)
    await _publish(db_session)

    body = await _get(client, "/v1/investors/careful/portfolio")

    assert body["meta"]["period"] == "2024Q1"

    response = await client.get("/v1/investors/careful/portfolio", params={"period": "2024Q2"})
    assert response.status_code == 404
    assert "2024Q2" in response.json()["detail"]
    assert "Its latest is 2024Q1" in response.json()["detail"]


async def test_a_period_can_be_named_either_way(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "either")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _quarter(db_session, filer, Q2, held(ALPHA, 200))
    await _publish(db_session)

    by_label = await _get(client, "/v1/investors/either/portfolio", period="2024Q1")
    by_date = await _get(client, "/v1/investors/either/portfolio", period="2024-03-31")

    assert by_label["data"] == by_date["data"]
    assert by_label["data"][0]["shares"] == "100.0000"
    assert by_label["meta"]["period"] == by_date["meta"]["period"] == "2024Q1"


@pytest.mark.parametrize(
    ("period", "message"),
    [
        ("2024Q5", "quarters are 1 to 4"),
        ("2024-03-30", "not a quarter end"),
        ("2024-06-31", "not a day of the calendar"),
        ("Q1-2024", "2026Q1 or as 2026-03-31"),
        ("latest", "2026Q1 or as 2026-03-31"),
    ],
)
async def test_a_period_that_is_not_one_is_a_422_saying_why(
    client: AsyncClient, db_session: AsyncSession, period: str, message: str
) -> None:
    filer = await _fund(db_session, "strict")
    await _quarter(db_session, filer, Q1, held(ALPHA, 1))
    await _publish(db_session)

    response = await client.get("/v1/investors/strict/portfolio", params={"period": period})

    assert response.status_code == 422
    assert message in response.json()["errors"][0]["msg"]


async def test_404s_for_an_unknown_investor_or_period_or_nothing_published(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "sparse")
    await _fund(db_session, "empty")
    await _quarter(db_session, filer, Q1, held(ALPHA, 1))
    await _quarter(db_session, filer, Q3, held(ALPHA, 1))
    await _publish(db_session)

    cases = [
        ("/v1/investors/nobody/portfolio", {}, "No investor 'nobody'"),
        ("/v1/investors/empty/portfolio", {}, "Nothing is published for 'empty'"),
        # A gap: not filed. An empty portfolio would read as a sale of everything.
        ("/v1/investors/sparse/portfolio", {"period": "2024Q2"}, "Its latest is 2024Q3"),
        ("/v1/investors/sparse/portfolio", {"period": "2025Q1"}, "for 2025Q1"),
    ]
    for path, params, message in cases:
        response = await client.get(path, params=params)
        assert response.status_code == 404, (path, params)
        assert message in response.json()["detail"], (path, params)


async def test_the_portfolio_is_live_while_the_views_are_stale(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fresh")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)
    filed = await _quarter(db_session, filer, Q2, held(ALPHA, 300))
    # Published to position_snapshot, but not yet to the views.
    await recompute(db_session, EVERYTHING)

    portfolio = await _get(client, "/v1/investors/fresh/portfolio")
    history = await _get(client, "/v1/investors/fresh/history")

    assert portfolio["meta"]["period"] == "2024Q2"
    # Dated from the snapshot, which the rows come from, not the stale view.
    assert portfolio["meta"]["latest_filing_at"] == _at(filed)
    assert [point["period"] for point in history["data"]] == ["2024-03-31"]


# --- the portfolio: options ------------------------------------------------------


async def test_options_are_left_out_by_default_and_never_weighed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "hedged")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _quarter(
        db_session,
        filer,
        Q2,
        held(ALPHA, 100),
        held(BRAVO, 100),
        held(ALPHA, 1_000, put_call="Call"),
        held(ALPHA, 5, put_call="Put"),
        # Principal, not shares: not a row either way.
        held(BRAVO, 70, put_call="Call", prn=True),
    )
    # The addition's call counts, summed into the original's.
    await _quarter(
        db_session,
        filer,
        Q2,
        held(ALPHA, 500, put_call="Call"),
        days_later=90,
        amends=AmendmentKind.NEW_HOLDINGS,
    )
    await _publish(db_session)

    stock = (await _get(client, "/v1/investors/hedged/portfolio"))["data"]
    everything = (await _get(client, "/v1/investors/hedged/portfolio", include_options="true"))[
        "data"
    ]

    assert [(row["cusip"], row["put_call"]) for row in stock] == [(BRAVO, None), (ALPHA, None)]
    assert [(row["cusip"], row["put_call"]) for row in everything] == [
        (BRAVO, None),
        (ALPHA, None),
        (ALPHA, "Call"),
        (ALPHA, "Put"),
    ]
    # The stock rows are the same rows: an option adds nothing to the total.
    assert everything[:2] == stock
    call, put = everything[2:]
    assert call["shares"] == "1500.0000"
    assert call["value_usd"] == "15000.00"
    for option in (call, put):
        assert option["weight_pct"] is None
        assert option["action"] is None
        assert option["shares_delta"] is None
        assert option["first_period"] is None


# --- the portfolio: sorting and paging -------------------------------------------


@pytest.fixture
async def book(db_session: AsyncSession) -> dict[tuple[str, str | None], int]:
    """A Q2 portfolio tied on every sort key, and two option lines.

    ``(cusip, put_call)`` to the security id, which breaks the ties.
    """
    cusips = [f"{n:05d}Z{n}0{n}" for n in range(1, 9)]
    s1, s2, s3, s4, s5, s6, s7, s8 = cusips
    filer = await _fund(db_session, "tied")
    await _quarter(
        db_session,
        filer,
        Q1,
        held(s1, 100),
        held(s2, 100),
        held(s3, 50),
        held(s4, 10),
        held(s5, 40),
        held(s8, 100),
    )
    await _quarter(
        db_session,
        filer,
        Q2,
        held(s1, 100),  # hold, $1,000
        held(s2, 150),  # +50%, $1,500
        held(s3, 75, price=20),  # +50%, $1,500
        held(s4, 5, price=200),  # -50%, $1,000
        held(s5, 20, price=50),  # -50%, $1,000
        held(s6, 100),  # new, $1,000
        held(s7, 50, price=20),  # new, $1,000
        # s8 exited.
        held(s1, 10, put_call="Call"),  # $100
        held(s6, 100, put_call="Put"),  # $1,000, tied with five stock rows
    )
    await _publish(db_session)
    found = await db_session.execute(
        select(Security.cusip, Security.id).where(Security.cusip.in_(cusips))
    )
    ids = {cusip: security_id for cusip, security_id in found.tuples()}
    return {
        **{(cusip, None): ids[cusip] for cusip in cusips[:7]},
        (s1, "Call"): ids[s1],
        (s6, "Put"): ids[s6],
    }


_FROM_NOTHING = Decimal(10) ** 22


def _keys(row: dict[str, Any], sort: str, security_id: int) -> tuple[Any, ...]:
    """The documented order, worked out from the response alone."""
    value = Decimal(row["value_usd"])
    tail = (value, security_id, row["put_call"] or "")
    if sort == "weight":
        return (Decimal(row["weight_pct"]) if row["weight_pct"] else Decimal(-1), *tail)
    if sort == "value":
        return tail
    if sort == "shares":
        return (Decimal(row["shares"]), *tail)
    if row["action"] is None:
        change = -_FROM_NOTHING
    elif row["shares_delta_pct"] is None:
        change = _FROM_NOTHING
    else:
        change = Decimal(row["shares_delta_pct"])
    return (change, *tail)


@pytest.mark.parametrize("sort", ["weight", "value", "shares", "change"])
@pytest.mark.parametrize("order", ["desc", "asc"])
async def test_every_sort_and_order_returns_every_row_once_in_its_order(
    client: AsyncClient, book: dict[tuple[str, str | None], int], sort: str, order: str
) -> None:
    path = "/v1/investors/tied/portfolio"
    rows = (await _get(client, path, include_options="true", limit=200))["data"]
    expected = sorted(
        rows,
        key=lambda row: _keys(row, sort, book[(row["cusip"], row["put_call"])]),
        reverse=order == "desc",
    )

    assert len(rows) == 9
    for limit in (1, 2, 4, 200):
        walked = await _walk(
            client, path, sort=sort, order=order, include_options="true", limit=limit
        )
        assert walked == expected, (sort, order, limit)


async def test_the_default_order_is_weight_largest_first_with_options_last(
    client: AsyncClient, book: dict[tuple[str, str | None], int]
) -> None:
    rows = (await _get(client, "/v1/investors/tied/portfolio", include_options="true"))["data"]

    weights = [row["weight_pct"] for row in rows]
    assert weights[:2] == ["18.750000", "18.750000"]
    assert weights[-2:] == [None, None]


async def test_change_puts_new_positions_first_and_the_deepest_cuts_last(
    client: AsyncClient, book: dict[tuple[str, str | None], int]
) -> None:
    rows = (await _get(client, "/v1/investors/tied/portfolio", sort="change"))["data"]

    assert [row["action"] for row in rows] == ["new", "new", "add", "add", "hold", "trim", "trim"]
    ascending = (await _get(client, "/v1/investors/tied/portfolio", sort="change", order="asc"))[
        "data"
    ]
    assert ascending == rows[::-1]


async def test_a_cursor_from_one_sort_or_order_is_refused_by_another(
    client: AsyncClient, book: dict[tuple[str, str | None], int]
) -> None:
    path = "/v1/investors/tied/portfolio"
    cursor = (await _get(client, path, sort="value", limit=2))["page"]["next_cursor"]

    for params in ({"sort": "shares"}, {"sort": "value", "order": "asc"}):
        response = await client.get(path, params={**params, "cursor": cursor})
        assert response.status_code == 400, params
        assert "different listing or sort order" in response.json()["detail"]


async def test_an_unknown_sort_or_order_is_a_422(
    client: AsyncClient, book: dict[tuple[str, str | None], int]
) -> None:
    for params in ({"sort": "turnover"}, {"order": "up"}, {"include_options": "maybe"}):
        response = await client.get("/v1/investors/tied/portfolio", params=params)
        assert response.status_code == 422, params


# --- activity --------------------------------------------------------------------


@pytest.fixture
async def busy(db_session: AsyncSession) -> None:
    """Four quarters of a filer that opens, adds, trims, holds and exits."""
    filer = await _fund(db_session, "busy")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    # ALPHA +20, BRAVO exited ($500 at its last price), CHARLIE opened ($300).
    await _quarter(db_session, filer, Q2, held(ALPHA, 120), held(CHARLIE, 30))
    # ALPHA held, CHARLIE trimmed by 10 at $20, DELTA opened ($50).
    await _quarter(
        db_session, filer, Q3, held(ALPHA, 120), held(CHARLIE, 20, price=20), held(DELTA, 5)
    )
    # Nothing in Q4: a gap, not a sale.
    await _publish(db_session)


async def test_activity_is_newest_period_first_then_largest_trade(
    client: AsyncClient, busy: None
) -> None:
    rows = (await _get(client, "/v1/investors/busy/activity"))["data"]

    assert [
        (row["period"], row["cusip"], row["action"], row["traded_value_usd"]) for row in rows
    ] == [
        ("2024-09-30", CHARLIE, "trim", "200.00"),
        ("2024-09-30", DELTA, "new", "50.00"),
        ("2024-06-30", BRAVO, "exit", "500.00"),
        ("2024-06-30", CHARLIE, "new", "300.00"),
        ("2024-06-30", ALPHA, "add", "200.00"),
    ]
    exit_row = rows[2]
    assert exit_row == {
        "cusip": BRAVO,
        "ticker": None,
        "issuer_name": f"ISSUER {BRAVO}",
        "period": "2024-06-30",
        "prev_period": "2024-03-31",
        "action": "exit",
        "shares": "0.0000",
        "shares_delta": "-50.0000",
        "shares_delta_pct": "-100.000000",
        "value_usd": "0.00",
        "traded_value_usd": "500.00",
        "weight_pct": "0.000000",
        "weight_delta": "-33.333333",
    }


async def test_activity_leaves_out_holds_and_the_first_period_unless_asked_for_holds(
    client: AsyncClient, busy: None
) -> None:
    rows = (await _get(client, "/v1/investors/busy/activity", action="hold"))["data"]

    # Q1's two positions are "new" in the table, and not activity.
    assert [(row["period"], row["cusip"], row["traded_value_usd"]) for row in rows] == [
        ("2024-09-30", ALPHA, "0.00")
    ]


@pytest.mark.parametrize(
    "params",
    [
        [("action", "new,exit")],
        [("action", "new"), ("action", "exit")],
        [("action", " exit , new ")],
    ],
)
async def test_action_is_comma_separated_or_repeated(
    client: AsyncClient, busy: None, params: list[tuple[str, str | int | float | bool | None]]
) -> None:
    response = await client.get("/v1/investors/busy/activity", params=params)

    assert response.status_code == 200, response.text
    assert [(row["period"], row["cusip"]) for row in response.json()["data"]] == [
        ("2024-09-30", DELTA),
        ("2024-06-30", BRAVO),
        ("2024-06-30", CHARLIE),
    ]


async def test_from_and_to_bound_the_periods_both_included_in_either_spelling(
    client: AsyncClient, busy: None
) -> None:
    only_q3 = await _get(client, "/v1/investors/busy/activity", **{"from": "2024Q3"})
    only_q2 = await _get(
        client, "/v1/investors/busy/activity", **{"from": "2024-06-30", "to": "2024Q2"}
    )
    up_to_q2 = await _get(client, "/v1/investors/busy/activity", to="2024-06-30")

    assert {row["period"] for row in only_q3["data"]} == {"2024-09-30"}
    assert {row["period"] for row in only_q2["data"]} == {"2024-06-30"}
    assert up_to_q2["data"] == only_q2["data"]
    assert only_q3["meta"]["period"] is None


async def test_activity_pages_through_every_change_once(client: AsyncClient, busy: None) -> None:
    everything = (
        await _get(client, "/v1/investors/busy/activity", action="new,add,trim,hold,exit")
    )["data"]

    for limit in (1, 2, 3):
        walked = await _walk(
            client, "/v1/investors/busy/activity", action="new,add,trim,hold,exit", limit=limit
        )
        assert walked == everything, limit


async def test_activity_refuses_a_bad_range_or_action_and_an_unknown_investor(
    client: AsyncClient, busy: None
) -> None:
    cases = [
        ({"from": "2024Q3", "to": "2024Q2"}, 422, "from (2024Q3) is after to (2024Q2)"),
        ({"from": "2024-07-01"}, 422, "not a quarter end"),
        ({"action": "sold"}, 422, "unknown action 'sold'"),
        ({"action": ""}, 422, "unknown action ''"),
    ]
    for params, status, message in cases:
        response = await client.get("/v1/investors/busy/activity", params=params)
        assert response.status_code == status, params
        assert message in response.text, params

    response = await client.get("/v1/investors/nobody/activity")
    assert response.status_code == 404


# --- history ---------------------------------------------------------------------


async def test_history_is_every_quarter_oldest_first_with_the_gaps_left_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "patchy")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    # Q2 never filed, Q3 withheld.
    await _quarter(db_session, filer, Q3, held(ALPHA, 1), suspect=True)
    await _quarter(db_session, filer, Q4, held(ALPHA, 300))
    await _publish(db_session)

    body = await _get(client, "/v1/investors/patchy/history")

    assert body["data"] == [
        {
            "period": "2024-03-31",
            "portfolio_value_usd": "1500.00",
            "position_count": 2,
            "top10_weight_pct": "100.000000",
        },
        {
            "period": "2024-06-30",
            "portfolio_value_usd": None,
            "position_count": None,
            "top10_weight_pct": None,
        },
        {
            "period": "2024-09-30",
            "portfolio_value_usd": None,
            "position_count": None,
            "top10_weight_pct": None,
        },
        {
            "period": "2024-12-31",
            "portfolio_value_usd": "3000.00",
            "position_count": 1,
            "top10_weight_pct": "100.000000",
        },
    ]
    assert body["page"] is None
    assert body["meta"]["period"] is None


async def test_top_ten_weight_leaves_out_the_eleventh(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "wide")
    # One position of $1,000 and ten of $100: the top ten are the $1,000 and
    # nine of the others, and the tenth $100 is the eleventh position.
    positions = [held(f"{n:05d}W{n % 10}0{n % 10}", 10) for n in range(10)]
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), *positions)
    await _publish(db_session)

    (point,) = (await _get(client, "/v1/investors/wide/history"))["data"]

    assert point["position_count"] == 11
    assert point["portfolio_value_usd"] == "2000.00"
    assert point["top10_weight_pct"] == "95.000000"


async def test_history_of_a_filer_with_nothing_published_is_empty_and_of_nobody_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _fund(db_session, "empty")

    assert (await _get(client, "/v1/investors/empty/history"))["data"] == []
    assert (await client.get("/v1/investors/nobody/history")).status_code == 404


# --- caching ----------------------------------------------------------------------


async def test_a_closed_quarter_is_served_from_the_cache_until_it_is_invalidated(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> None:
    """Kept for a day, so a restatement published since is not seen until the
    publish drops the cache, which the CLI does and this test does by hand."""
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)
    path = "/v1/investors/berkshire/portfolio"

    first = await client.get(path, params={"period": "2024Q1"})
    await _quarter(
        db_session, filer, Q1, held(ALPHA, 300), amends=AmendmentKind.RESTATEMENT, days_later=60
    )
    await _publish(db_session)
    second = await client.get(path, params={"period": "2024-03-31"})
    await invalidate(redis)  # type: ignore[arg-type]
    third = await client.get(path, params={"period": "2024Q1"})

    assert [r.headers["x-cache"] for r in (first, second, third)] == ["MISS", "HIT", "MISS"]
    assert first.headers["cache-control"] == (
        f"public, max-age=86400, stale-while-revalidate={STALE_WHILE_REVALIDATE}"
    )
    assert second.content == first.content
    assert [row["shares"] for row in third.json()["data"]] == ["300.0000"]
    assert third.headers["etag"] != first.headers["etag"]


async def test_the_latest_portfolio_is_kept_for_five_minutes(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> None:
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)

    response = await client.get("/v1/investors/berkshire/portfolio")

    assert response.headers["cache-control"] == "public, max-age=300"
    assert list(redis.ttls.values()) == [300]
