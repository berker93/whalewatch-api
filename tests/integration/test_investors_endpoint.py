"""``GET /v1/investors`` and ``GET /v1/investors/{slug}``, against a real Postgres.

Every test publishes the way an operator does, ``recompute`` then
``refresh-views``, and reads through the app. The cases that matter are the
ones where a filer's figures could come from the wrong place: the top holding
of an older period, a sparkline that skips a gap instead of showing it, a
withheld filing's date, or a live aggregate where the view was asked for.
Then paging under every sort, with ties at page boundaries, and the promise
that each request is one query.
"""

import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.routers.investors import LATEST
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, Security
from app.db.queries.top_holding import top_holdings, top_holdings_ranked
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
    price: Decimal


def held(cusip: str, shares: int, *, price: int = 10) -> _Held:
    return _Held(cusip, Decimal(shares), Decimal(price))


async def _fund(
    session: AsyncSession,
    slug: str,
    *,
    display_name: str | None = None,
    name: str | None = None,
    manager_name: str | None = None,
    category: str | None = None,
    ciks: int = 1,
) -> int:
    """A made-up filer, with ``ciks`` CIKs of its own, oldest first."""
    filer_id = await session.scalar(
        insert(Filer)
        .values(
            name=name or slug.upper(),
            slug=slug,
            display_name=display_name,
            manager_name=manager_name,
            category=category,
        )
        .returning(Filer.id)
    )
    assert filer_id is not None
    for priority in range(ciks):
        await session.execute(
            insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=priority)
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
) -> datetime:
    """A 13F-HR for ``period`` holding ``positions``, as loaded; or with
    ``amends``, a 13F-HR/A of that kind. Returns when it was filed."""
    cik = await session.scalar(
        select(FilerCik.cik)
        .where(FilerCik.filer_id == filer_id)
        .order_by(FilerCik.priority.desc())
        .limit(1)
    )
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
            form_type="13F-HR/A" if amends else "13F-HR",
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
                sshprnamt_type="SH",
            )
        )
    return filed_at


async def _publish(session: AsyncSession) -> None:
    """``recompute --all``, then ``refresh-views``."""
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _list(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/v1/investors", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _row(client: AsyncClient, slug: str) -> dict[str, Any]:
    rows = [row for row in (await _list(client, limit=200))["data"] if row["slug"] == slug]
    assert len(rows) == 1, f"{slug} listed {len(rows)} times"
    row: dict[str, Any] = rows[0]
    return row


async def _walk(client: AsyncClient, **params: Any) -> list[str]:
    """Every page, start to end; the slugs in the order they came."""
    slugs: list[str] = []
    cursor = None
    while True:
        body = await _list(client, **params, **({"cursor": cursor} if cursor else {}))
        slugs += [row["slug"] for row in body["data"]]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return slugs


@contextmanager
def _queries(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every query sent to Postgres in the block, transaction control left out."""
    sent: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            sent.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


# --- one row ---------------------------------------------------------------------


async def test_a_row_is_the_filers_latest_published_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(
        db_session,
        "berkshire",
        display_name="Berkshire Hathaway",
        manager_name="Warren Buffett",
        category="value",
    )
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    filed = await _quarter(db_session, filer, Q2, held(ALPHA, 100, price=12), held(CHARLIE, 30))
    await _publish(db_session)

    body = await _list(client)

    assert body["meta"]["period"] is None
    assert body["page"] == {"limit": 50, "next_cursor": None}
    assert body["data"] == [
        {
            "slug": "berkshire",
            "display_name": "Berkshire Hathaway",
            "manager_name": "Warren Buffett",
            "category": "value",
            "latest_period": "2024-06-30",
            "last_filed_at": filed.isoformat().replace("+00:00", "Z"),
            # Strings, as every numeric is: see app.api.schemas.types.
            "portfolio_value_usd": "1500.00",
            "position_count": 2,
            "top_holding": {
                "cusip": ALPHA,
                "ticker": None,
                "issuer_name": f"ISSUER {ALPHA}",
                "weight_pct": "80.000000",
            },
            "sparkline": [None, None, None, None, None, None, "1500.00", "1500.00"],
        }
    ]


async def test_a_filer_without_a_display_name_is_shown_by_its_edgar_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _fund(db_session, "uncurated", name="SOME CAPITAL LLC")

    assert (await _row(client, "uncurated"))["display_name"] == "SOME CAPITAL LLC"


async def test_a_filer_with_nothing_published_is_listed_with_its_figures_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    published = await _fund(db_session, "published")
    withheld = await _fund(db_session, "withheld")
    await _fund(db_session, "never-filed")
    await _quarter(db_session, published, Q1, held(ALPHA, 1))
    await _quarter(db_session, withheld, Q1, held(ALPHA, 1_000), suspect=True)
    await _publish(db_session)

    rows = (await _list(client))["data"]

    # Last when sorted by value, which they have none of.
    assert rows[0]["slug"] == "published"
    for row in rows[1:]:
        assert row["latest_period"] is None
        assert row["last_filed_at"] is None
        assert row["portfolio_value_usd"] is None
        assert row["position_count"] is None
        assert row["top_holding"] is None
        assert row["sparkline"] == []


# --- the top holding -------------------------------------------------------------


async def test_the_top_holding_is_the_latest_periods_not_an_earlier_ones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "rotator")
    await _quarter(db_session, filer, Q1, held(ALPHA, 1_000), held(BRAVO, 10))
    await _quarter(db_session, filer, Q2, held(ALPHA, 10), held(BRAVO, 1_000))
    await _publish(db_session)

    assert (await _row(client, "rotator"))["top_holding"]["cusip"] == BRAVO


async def test_a_tie_for_top_holding_goes_the_same_way_every_time(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "even")
    # Inserted in the opposite order to their ids, so the heap's order is not
    # the answer by accident.
    await _quarter(db_session, filer, Q1, held(CHARLIE, 5), held(BRAVO, 5), held(ALPHA, 5))
    await _publish(db_session)
    first = await db_session.scalar(
        select(Security.cusip)
        .where(Security.cusip.in_([ALPHA, BRAVO, CHARLIE]))
        .order_by(Security.id)
        .limit(1)
    )

    assert (await _row(client, "even"))["top_holding"]["cusip"] == first


async def test_distinct_on_and_row_number_pick_the_same_top_holdings(
    db_session: AsyncSession,
) -> None:
    """The two spellings in app.db.queries.top_holding, over a universe with ties."""
    rng = random.Random(7)
    cusips = [f"{n:05d}X{n % 10}0{n % 10}" for n in range(40)]
    for f in range(12):
        filer = await _fund(db_session, f"fund-{f}")
        for period in (Q1, Q2, Q3):
            picked = rng.sample(cusips, rng.randint(1, 15))
            # Drawn from three sizes, so most portfolios tie at the top.
            await _quarter(
                db_session,
                filer,
                period,
                *(held(cusip, rng.choice([10, 20, 20])) for cusip in picked),
            )
    await _publish(db_session)

    latest = select(LATEST.c.filer_id, LATEST.c.period_of_report).subquery("periods")
    distinct_on = set((await db_session.execute(top_holdings(latest))).tuples())
    ranked = set((await db_session.execute(top_holdings_ranked(latest))).tuples())

    assert len(distinct_on) == 12
    assert distinct_on == ranked


# --- the sparkline ---------------------------------------------------------------


async def test_the_sparkline_is_eight_quarters_with_the_gaps_left_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "patchy")
    # Ten quarters, 2022Q3 to 2024Q4. 2023Q2 was never filed and 2023Q4 was
    # withheld. The first two are older than the sparkline goes.
    periods = [
        date(2022, 9, 30),
        date(2022, 12, 31),
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        date(2023, 12, 31),
        Q1,
        Q2,
        Q3,
        Q4,
    ]
    for shares, period in enumerate(periods, start=1):
        if period == date(2023, 6, 30):
            continue
        await _quarter(
            db_session, filer, period, held(ALPHA, shares), suspect=period == date(2023, 12, 31)
        )
    await _publish(db_session)

    row = await _row(client, "patchy")

    assert row["latest_period"] == "2024-12-31"
    #            2023Q1   2023Q2  2023Q3   2023Q4  2024Q1   2024Q2   2024Q3   2024Q4
    assert row["sparkline"] == [
        "30.00",
        None,
        "50.00",
        None,
        "70.00",
        "80.00",
        "90.00",
        "100.00",
    ]
    assert row["sparkline"][-1] == row["portfolio_value_usd"]


# --- when it was filed -----------------------------------------------------------


async def test_last_filed_at_is_the_newest_filing_behind_the_latest_period(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "amender")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _quarter(db_session, filer, Q2, held(ALPHA, 100))
    amended = await _quarter(
        db_session, filer, Q2, held(BRAVO, 10), days_later=90, amends=AmendmentKind.NEW_HOLDINGS
    )
    await _publish(db_session)

    row = await _row(client, "amender")

    assert row["latest_period"] == "2024-06-30"
    assert row["last_filed_at"] == amended.isoformat().replace("+00:00", "Z")
    assert row["position_count"] == 2


async def test_a_withheld_filing_is_not_the_latest_period_or_its_filing_date(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "careful")
    filed = await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _quarter(db_session, filer, Q2, held(ALPHA, 100), suspect=True)
    await _publish(db_session)

    row = await _row(client, "careful")

    assert row["latest_period"] == "2024-03-31"
    assert row["last_filed_at"] == filed.isoformat().replace("+00:00", "Z")


# --- reading the view ------------------------------------------------------------


async def test_the_list_is_as_of_the_last_refresh_of_the_views(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "fresh")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)
    await _quarter(db_session, filer, Q2, held(ALPHA, 300))
    # Published to position_snapshot, but not yet to the views.
    await recompute(db_session, EVERYTHING)

    assert (await _row(client, "fresh"))["latest_period"] == "2024-03-31"
    assert (await _row(client, "fresh"))["portfolio_value_usd"] == "1000.00"

    await refresh_views(db_session)

    assert (await _row(client, "fresh"))["latest_period"] == "2024-06-30"
    assert (await _row(client, "fresh"))["portfolio_value_usd"] == "3000.00"


# --- sorting, filtering, paging --------------------------------------------------


@pytest.fixture
async def universe(db_session: AsyncSession) -> dict[str, int]:
    """Fourteen filers, slug to id. Most tie with another on value, on position
    count or on name, and two have nothing published."""
    spec: list[tuple[str, str, str, list[int]]] = [
        # slug, display name, category, shares of each position (at $10)
        ("a", "Zeta Partners", "value", [100]),
        ("b", "Alpha Capital", "value", [50, 50]),
        ("c", "Alpha Capital", "growth", [100]),
        ("d", "Mu Fund", "growth", [10, 10, 10, 10]),
        ("e", "Beta", "quant", [25, 25, 25, 25]),
        ("f", "Alpha Capital", "quant", [40, 30]),
        ("g", "Kappa", "value", [70]),
        ("h", "Kappa", "macro", [5, 5, 5]),
        ("i", "Omega", "activist", [100]),
        ("j", "Gamma", "activist", [1, 2, 3, 4, 5, 6]),
        ("k", "Delta", "value", [33, 33, 34]),
        ("l", "Epsilon", "growth", [99]),
        ("m", "Theta", "value", []),
        ("n", "Iota", "macro", []),
    ]
    ids: dict[str, int] = {}
    for slug, display_name, category, shares in spec:
        ids[slug] = await _fund(db_session, slug, display_name=display_name, category=category)
        if shares:
            positions = [held(f"{n:05d}Y{n}0{n}", s) for n, s in enumerate(shares, start=1)]
            await _quarter(db_session, ids[slug], Q1, *positions)
    await _publish(db_session)
    return ids


async def test_by_value_largest_first_ties_newest_filer_first(
    client: AsyncClient, universe: dict[str, int]
) -> None:
    slugs = await _walk(client, sort="value", limit=4)

    # The fixture inserts in slug order, so ids rise with the slug, and a tie
    # goes to the later letter. a, b, c, e, i and k hold $1,000 each, l $990,
    # f and g $700, then d, j and h. m and n have nothing published.
    assert slugs == ["k", "i", "e", "c", "b", "a", "l", "g", "f", "d", "j", "h", "n", "m"]
    assert universe["k"] > universe["a"]


async def test_every_sort_returns_every_filer_once_in_its_order(
    client: AsyncClient, universe: dict[str, int], db_session: AsyncSession
) -> None:
    rows = (await _list(client, limit=200))["data"]
    by_slug = {row["slug"]: row for row in rows}
    value = {s: Decimal(r["portfolio_value_usd"] or -1) for s, r in by_slug.items()}
    positions = {
        s: r["position_count"] if r["position_count"] is not None else -1
        for s, r in by_slug.items()
    }
    name = {s: r["display_name"] for s, r in by_slug.items()}

    for limit in (1, 3, 5, 200):
        assert await _walk(client, sort="value", limit=limit) == sorted(
            universe, key=lambda s: (-value[s], -universe[s])
        )
        assert await _walk(client, sort="positions", limit=limit) == sorted(
            universe, key=lambda s: (-positions[s], -universe[s])
        )
        assert await _walk(client, sort="name", limit=limit) == sorted(
            universe, key=lambda s: (name[s], s)
        )


async def test_a_cursor_from_one_sort_is_refused_by_another(
    client: AsyncClient, universe: dict[str, int]
) -> None:
    cursor = (await _list(client, sort="value", limit=2))["page"]["next_cursor"]

    response = await client.get("/v1/investors", params={"sort": "name", "cursor": cursor})

    assert response.status_code == 400
    assert "different listing or sort order" in response.json()["detail"]
    assert response.json()["code"] == "invalid_cursor"


async def test_category_narrows_the_list(client: AsyncClient, universe: dict[str, int]) -> None:
    rows = (await _list(client, category="activist"))["data"]

    assert {row["slug"] for row in rows} == {"i", "j"}


async def test_an_unknown_category_or_sort_is_a_422(
    client: AsyncClient, universe: dict[str, int]
) -> None:
    for params in ({"category": "momentum"}, {"sort": "turnover"}, {"q": ""}):
        response = await client.get("/v1/investors", params=params)
        assert response.status_code == 422, params


async def test_q_matches_our_name_edgar_name_or_manager_ignoring_case(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _fund(
        db_session,
        "berkshire",
        display_name="Berkshire Hathaway",
        name="BERKSHIRE HATHAWAY INC",
        manager_name="Warren Buffett",
    )
    await _fund(db_session, "scion", display_name="Scion", name="SCION ASSET MANAGEMENT, LLC")
    await _fund(db_session, "pct", display_name="100% Capital", name="ONE_HUNDRED LLC")

    async def found(q: str) -> set[str]:
        return {row["slug"] for row in (await _list(client, q=q))["data"]}

    assert await found("hathaway") == {"berkshire"}
    assert await found("BUFFETT") == {"berkshire"}
    assert await found("asset management") == {"scion"}
    # Not wildcards: % matches a percent sign and _ an underscore.
    assert await found("%") == {"pct"}
    assert await found("E_H") == {"pct"}
    assert await found("nobody") == set()


async def test_filters_and_paging_combine(client: AsyncClient, universe: dict[str, int]) -> None:
    slugs = await _walk(client, category="value", sort="name", limit=2)

    # Alpha Capital, Delta, Kappa, Theta, Zeta Partners
    assert slugs == ["b", "k", "g", "m", "a"]


# --- one investor ----------------------------------------------------------------


async def test_one_investor_adds_concentration_turnover_periods_and_ciks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "detailed", display_name="Detailed", ciks=2)
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    # ALPHA held, and up in price. BRAVO exited: 50 shares at its last price,
    # $500. CHARLIE opened: $300. Turnover is ($500 + $300) / 2 / $1,500.
    await _quarter(db_session, filer, Q2, held(ALPHA, 100, price=12), held(CHARLIE, 30))
    await _publish(db_session)
    ciks = list(
        (
            await db_session.scalars(
                select(FilerCik.cik).where(FilerCik.filer_id == filer).order_by(FilerCik.priority)
            )
        ).all()
    )

    response = await client.get("/v1/investors/detailed")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["first_period"] == "2024-03-31"
    assert body["latest_period"] == "2024-06-30"
    assert body["top10_weight_pct"] == "100.000000"
    assert body["turnover_pct"] == "26.666667"
    assert body["ciks"] == ciks
    # Everything the list says, too.
    listed = await _row(client, "detailed")
    assert {key: body[key] for key in listed} == listed


async def test_one_investor_with_nothing_published(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _fund(db_session, "new-here")

    body = (await client.get("/v1/investors/new-here")).json()

    assert body["first_period"] is None
    assert body["latest_period"] is None
    assert body["turnover_pct"] is None
    assert body["sparkline"] == []
    assert len(body["ciks"]) == 1


async def test_an_unknown_slug_is_a_404(client: AsyncClient, db_session: AsyncSession) -> None:
    response = await client.get("/v1/investors/no-such-fund")

    assert response.status_code == 404
    assert "no-such-fund" in response.json()["detail"]


# --- one query -------------------------------------------------------------------


async def test_each_request_is_one_query(
    client: AsyncClient, universe: dict[str, int], migrated_engine: AsyncEngine
) -> None:
    requests: list[tuple[str, dict[str, str | int]]] = [
        ("/v1/investors", {}),
        ("/v1/investors", {"limit": 3, "sort": "positions", "category": "value", "q": "a"}),
        ("/v1/investors/a", {}),
        ("/v1/investors/nope", {}),
    ]
    for path, params in requests:
        with _queries(migrated_engine) as sent:
            await client.get(path, params=params)
        assert len(sent) == 1, (path, params, sent)
