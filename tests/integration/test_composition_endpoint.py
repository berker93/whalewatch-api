"""``/v1/investors/{slug}/composition``, against a real Postgres.

The cases that matter are the rollup's: which positions are summed into
``other``, that its count and weight are the rest of the portfolio's, that a
lone small position is not hidden in it, and that it comes last even when it
is the largest block. Then that options never become blocks, and that the
period, meta and cache are the portfolio's.
"""

from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cache import STALE_WHILE_REVALIDATE
from app.api.deps import get_redis
from tests.fake_redis import FakeRedis
from tests.integration.test_portfolio_endpoint import (
    ALPHA,
    BRAVO,
    CHARLIE,
    DELTA,
    Q1,
    Q2,
    _at,
    _fund,
    _get,
    _publish,
    _quarter,
    held,
)

PATH = "/v1/investors/{slug}/composition"


def _blocks(body: dict[str, Any]) -> list[tuple[str, str | None, int]]:
    return [(row["kind"], row["cusip"], row["position_count"]) for row in body["data"]]


async def test_a_block_carries_what_a_treemap_labels_and_colours_it_by(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100), held(BRAVO, 50))
    filed = await _quarter(db_session, filer, Q2, held(ALPHA, 120), held(CHARLIE, 30))
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="berkshire"))

    assert body["data"] == [
        {
            "kind": "security",
            "cusip": ALPHA,
            "ticker": None,
            "current_ticker": None,
            "issuer_name": f"ISSUER {ALPHA}",
            "value_usd": "1200.00",
            "weight_pct": "80.000000",
            "weight_delta": "13.333333",
            "action": "add",
            "confidence": "low",
            "position_count": 1,
        },
        {
            "kind": "security",
            "cusip": CHARLIE,
            "ticker": None,
            "current_ticker": None,
            "issuer_name": f"ISSUER {CHARLIE}",
            "value_usd": "300.00",
            "weight_pct": "20.000000",
            "weight_delta": "20.000000",
            "action": "new",
            "confidence": "low",
            "position_count": 1,
        },
    ]
    assert body["meta"]["period"] == "2024Q2"
    assert body["meta"]["latest_filing_at"] == _at(filed)
    assert body["meta"]["coverage"] == {"filers_reported": 1, "filers_tracked": 1}
    assert body["page"] is None


async def test_positions_under_the_weight_are_one_other_block_last_with_their_count(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "tail")
    # Of $100,350: BRAVO 0.20%, CHARLIE 0.10%, DELTA 0.05%, all under 0.25%.
    await _quarter(
        db_session,
        filer,
        Q1,
        held(ALPHA, 10_000),
        held(BRAVO, 20),
        held(CHARLIE, 10),
        held(DELTA, 5),
    )
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="tail"))
    portfolio = await _get(client, "/v1/investors/tail/portfolio")

    assert _blocks(body) == [("security", ALPHA, 1), ("other", None, 3)]
    tail = [row for row in portfolio["data"] if row["cusip"] != ALPHA]
    assert body["data"][-1] == {
        "kind": "other",
        "cusip": None,
        "ticker": None,
        "current_ticker": None,
        "issuer_name": None,
        "value_usd": "350.00",
        # The positions' own weights, summed: the blocks add up as the rows do.
        "weight_pct": str(sum(Decimal(row["weight_pct"]) for row in tail)),
        "weight_delta": None,
        "action": None,
        "confidence": None,
        "position_count": 3,
    }


async def test_other_comes_last_even_when_it_is_the_largest_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "quant")
    # Twenty positions of 4.5% and one of 10%: under min_weight=5, the twenty
    # are 90% of the book, and still after the one shown.
    cusips = [f"{n:05d}X{n % 10}0{n % 10}" for n in range(20)]
    await _quarter(
        db_session,
        filer,
        Q1,
        held(ALPHA, 1_000),
        *(held(cusip, 450) for cusip in cusips),
    )
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="quant"), min_weight="5")

    assert _blocks(body) == [("security", ALPHA, 1), ("other", None, 20)]
    assert body["data"][-1]["value_usd"] == "90000.00"
    assert body["data"][-1]["weight_pct"] == "90.000000"


async def test_every_position_under_the_weight_is_one_block(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "even")
    # Twelve of 8.33%, every one under 10%.
    await _quarter(
        db_session, filer, Q1, *(held(f"{n:05d}E{n % 10}0{n % 10}", 100) for n in range(12))
    )
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="even"), min_weight="10")

    assert _blocks(body) == [("other", None, 12)]
    assert abs(Decimal(body["data"][0]["weight_pct"]) - 100) < Decimal("0.00001")


async def test_a_lone_small_position_is_shown_not_summed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "lone")
    # BRAVO is 0.1%: an "other" of one would hide it for nothing.
    await _quarter(db_session, filer, Q1, held(ALPHA, 9_990), held(BRAVO, 10))
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="lone"))

    assert _blocks(body) == [("security", ALPHA, 1), ("security", BRAVO, 1)]


async def test_min_weight_moves_the_cut_and_is_bounded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "tail")
    await _quarter(
        db_session,
        filer,
        Q1,
        held(ALPHA, 10_000),
        held(BRAVO, 20),
        held(CHARLIE, 10),
        held(DELTA, 5),
    )
    await _publish(db_session)
    path = PATH.format(slug="tail")

    # 0.08%: BRAVO (0.20%) and CHARLIE (0.10%) are shown; DELTA alone is under.
    body = await _get(client, path, min_weight="0.08")
    assert _blocks(body) == [
        ("security", ALPHA, 1),
        ("security", BRAVO, 1),
        ("security", CHARLIE, 1),
        ("security", DELTA, 1),
    ]
    for value in ("0.01", "10.5", "lots"):
        response = await client.get(path, params={"min_weight": value})
        assert response.status_code == 422, value


async def test_options_are_never_blocks_nor_weighed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "hedged")
    await _quarter(
        db_session,
        filer,
        Q1,
        held(ALPHA, 100),
        held(BRAVO, 100),
        held(ALPHA, 100_000, put_call="Call"),
        held(CHARLIE, 5, put_call="Put"),
    )
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="hedged"))

    assert [(row["cusip"], row["weight_pct"]) for row in body["data"]] == [
        # Equal value: ordered by security, which ALPHA was loaded first as.
        (ALPHA, "50.000000"),
        (BRAVO, "50.000000"),
    ]


async def test_the_period_is_the_portfolios_and_so_are_its_404s(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "sparse")
    other = await _fund(db_session, "later")
    await _quarter(db_session, filer, Q1, held(ALPHA, 1))
    await _quarter(db_session, other, Q2, held(ALPHA, 1))
    await _publish(db_session)

    # The filer's latest, not the universe's.
    assert (await _get(client, PATH.format(slug="sparse")))["meta"]["period"] == "2024Q1"
    assert (await _get(client, PATH.format(slug="sparse"), period="2024-03-31"))["data"]
    for slug, params, message in [
        ("nobody", {}, "No investor 'nobody'"),
        ("sparse", {"period": "2024Q2"}, "Its latest is 2024Q1"),
    ]:
        response = await client.get(PATH.format(slug=slug), params=params)
        assert response.status_code == 404, (slug, params)
        assert message in response.json()["error"]["message"]


async def test_meta_carries_the_portfolios_caveats(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(db_session, "vague")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _quarter(db_session, filer, Q1, held(CHARLIE, 10), days_later=90, unclassified=True)
    await _publish(db_session)

    body = await _get(client, PATH.format(slug="vague"))

    (caveat,) = body["meta"]["caveats"]
    assert "13F-HR/A with no amendmentType" in caveat


async def test_a_closed_quarter_is_cached_for_a_day_under_the_callers_tier(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> None:
    redis = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    filer = await _fund(db_session, "berkshire")
    await _quarter(db_session, filer, Q1, held(ALPHA, 100))
    await _publish(db_session)
    path = PATH.format(slug="berkshire")

    first = await client.get(path, params={"period": "2024Q1"})
    second = await client.get(path, params={"period": "2024-03-31", "min_weight": "0.25"})
    third = await client.get(path, params={"period": "2024Q1", "min_weight": "1"})

    assert [r.headers["x-cache"] for r in (first, second, third)] == ["MISS", "HIT", "MISS"]
    assert first.headers["cache-control"] == (
        f"private, max-age=86400, stale-while-revalidate={STALE_WHILE_REVALIDATE}"
    )
    # ww:v1:portfolio.read_composition:{tier}:{hash}
    assert all(key.count(":") == 4 for key in redis.values if "read_composition" in key)
