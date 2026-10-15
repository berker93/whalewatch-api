"""Cluster buy detection and ``/v1/insiders/clusters``, against a real Postgres.

The fixtures are hand-built: purchases on known days by known insiders, so
each test knows which episodes exist. What matters is what would make the
signal lie: one insider counted three times for buying three days running, a
payroll ESPP purchase read as thirty opinions, a 10% owner's fund counted as
an officer, a month of buying split into a row per day, a 2021 cluster missed
because the window looked back from today, a window one day too wide.

The oracle at the end defines an episode differently from the SQL, as the
connected components of windows that share a purchase, and checks the two
agree on random histories.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filing, InsiderCluster, InsiderTransaction
from app.derived.insider_cluster import MIN_INSIDERS, WINDOWS, ClusterRebuild, detect_clusters
from tests.integration.test_insiders_endpoint import (
    LENNAR,
    NVDA_CUSIP,
    NVIDIA,
    _form4,
    _get,
    _insider,
    _lennar,
    _line,
    _security,
    _walk,
)

APPLE = "0000320193"
START = date(2026, 3, 2)


def day(offset: int) -> date:
    return START + timedelta(days=offset)


async def _buy(
    session: AsyncSession,
    insider_id: int,
    *offsets: int,
    issuer_cik: str = NVIDIA,
    security_id: int | None = None,
    shares: str = "100",
    price: str | None = "10",
    plan: bool = False,
    footnotes: dict[str, str] | None = None,
    derivative: bool = False,
    **roles: Any,
) -> str:
    """One Form 4 of purchases on ``offsets`` days from :data:`START`, by a
    director unless ``roles`` says otherwise."""
    extra: dict[str, Any] = {"is_derivative": derivative}
    if footnotes is not None:
        extra["footnotes"] = footnotes
    return await _form4(
        session,
        insider_id,
        *(
            _line("P", day(offset), shares=shares, price=price, plan=plan) | extra
            for offset in offsets
        ),
        issuer_cik=issuer_cik,
        security_id=security_id,
        **(roles or {"is_director": True}),
    )


async def _directors(session: AsyncSession, count: int) -> list[int]:
    return [await _insider(session, f"DIRECTOR {n}") for n in range(count)]


async def _episodes(
    session: AsyncSession, *, window: int = 30, min_insiders: int = 3
) -> list[tuple[str, date, date, int, int]]:
    """``(issuer_cik, window_start, window_end, insider_count, transaction_count)``."""
    rows = await session.execute(
        select(
            InsiderCluster.issuer_cik,
            InsiderCluster.window_start,
            InsiderCluster.window_end,
            InsiderCluster.insider_count,
            InsiderCluster.transaction_count,
        )
        .where(
            InsiderCluster.window_days == window,
            InsiderCluster.min_insiders == min_insiders,
        )
        .order_by(InsiderCluster.issuer_cik, InsiderCluster.window_start)
    )
    return [tuple(row) for row in rows]


# --- the window ---------------------------------------------------------------


async def test_three_directors_buying_within_a_month_are_one_cluster(
    db_session: AsyncSession,
) -> None:
    nvda = await _security(db_session, NVDA_CUSIP, "NVDA")
    a, b, c = await _directors(db_session, 3)
    await _buy(db_session, a, 0, security_id=nvda, shares="100", price="10")
    await _buy(db_session, b, 9, security_id=nvda, shares="200", price="10")
    await _buy(db_session, c, 20, security_id=nvda, shares="300", price=None)

    rebuilt = await detect_clusters(db_session)

    cluster = (
        await db_session.scalars(
            select(InsiderCluster).where(
                InsiderCluster.window_days == 30, InsiderCluster.min_insiders == 3
            )
        )
    ).one()
    assert (cluster.issuer_cik, cluster.window_start, cluster.window_end) == (
        NVIDIA,
        day(0),
        day(20),
    )
    assert (cluster.insider_count, cluster.transaction_count) == (3, 3)
    assert cluster.security_id == nvda
    assert cluster.issuer_name == f"ISSUER {NVIDIA}"
    # The line with no price adds its shares, and nothing to the value.
    assert (cluster.total_shares, cluster.total_value) == (Decimal(600), Decimal(3000))
    ids = await db_session.scalars(
        select(InsiderTransaction.id).order_by(InsiderTransaction.transaction_date)
    )
    assert cluster.transaction_ids == list(ids)
    assert rebuilt.episodes == 1
    assert rebuilt.lines["counted"] == 3
    assert [episode.window_end for episode in rebuilt.added] == [day(20)]


async def test_one_insider_buying_three_days_running_is_one_insider(
    db_session: AsyncSession,
) -> None:
    a, b = await _directors(db_session, 2)
    await _buy(db_session, a, 0, 1, 2)
    await _buy(db_session, b, 3)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == []
    assert await _episodes(db_session, min_insiders=2) == [(NVIDIA, day(0), day(3), 2, 4)]


async def test_the_window_is_the_day_and_the_days_before_it_inclusive(
    db_session: AsyncSession,
) -> None:
    """30 days apart is inside a 30-day window; 31 is not."""
    a, b, c, d, e, f = await _directors(db_session, 6)
    await _buy(db_session, a, 0)
    await _buy(db_session, b, 15)
    await _buy(db_session, c, 30)
    await _buy(db_session, d, 100, issuer_cik=APPLE)
    await _buy(db_session, e, 115, issuer_cik=APPLE)
    await _buy(db_session, f, 131, issuer_cik=APPLE)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [(NVIDIA, day(0), day(30), 3, 3)]


async def test_a_cluster_years_ago_is_found_as_surely_as_one_this_year(
    db_session: AsyncSession,
) -> None:
    """Rolling, not a lookback from today, and across the yearly partitions."""
    insiders = await _directors(db_session, 3)
    for offset, insider in enumerate(insiders):
        await _form4(
            db_session,
            insider,
            _line("P", date(2021, 12, 30) + timedelta(days=offset)),
            is_director=True,
        )

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [(NVIDIA, date(2021, 12, 30), date(2022, 1, 1), 3, 3)]


# --- episodes -----------------------------------------------------------------


async def test_overlapping_windows_are_one_episode(db_session: AsyncSession) -> None:
    """Days 0, 10, 20 qualify; so do 10, 20, 35 and 20, 35, 48. Each window
    shares a purchase with the one before, so it is one episode of five."""
    insiders = await _directors(db_session, 5)
    for insider, offset in zip(insiders, (0, 10, 20, 35, 48), strict=True):
        await _buy(db_session, insider, offset)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [(NVIDIA, day(0), day(48), 5, 5)]


async def test_windows_that_share_no_purchase_are_two_episodes(db_session: AsyncSession) -> None:
    """Days 0, 5, 10, then 40, 45, 50. The window ending on 40 holds day 10's
    purchase too, but two insiders do not qualify it. The first that does
    after day 10 ends on 50 and starts on 40, sharing nothing with the first."""
    insiders = await _directors(db_session, 6)
    for insider, offset in zip(insiders, (0, 5, 10, 40, 45, 50), strict=True):
        await _buy(db_session, insider, offset)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [
        (NVIDIA, day(0), day(10), 3, 3),
        (NVIDIA, day(40), day(50), 3, 3),
    ]


async def test_a_burst_at_one_issuer_does_not_join_another_issuers(
    db_session: AsyncSession,
) -> None:
    insiders = await _directors(db_session, 6)
    for insider in insiders[:3]:
        await _buy(db_session, insider, 0)
    for insider in insiders[3:]:
        await _buy(db_session, insider, 1, issuer_cik=APPLE)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [
        (APPLE, day(1), day(1), 3, 3),
        (NVIDIA, day(0), day(0), 3, 3),
    ]


async def test_every_window_and_threshold_is_written(db_session: AsyncSession) -> None:
    """Five insiders over 50 days: in 14 days never more than two at once."""
    insiders = await _directors(db_session, 5)
    for insider, offset in zip(insiders, (0, 12, 25, 38, 50), strict=True):
        await _buy(db_session, insider, offset)

    rebuilt = await detect_clusters(db_session)

    assert set(rebuilt.written) == {(w, m) for w in WINDOWS for m in MIN_INSIDERS}
    assert rebuilt.written[14, 2] == 1 and rebuilt.written[14, 3] == 0
    assert rebuilt.written[30, 3] == 1 and rebuilt.written[30, 4] == 0
    assert rebuilt.written[60, 5] == 1
    assert await _episodes(db_session, window=14, min_insiders=2) == [
        (NVIDIA, day(0), day(50), 5, 5)
    ]


# --- what counts ----------------------------------------------------------------


async def test_purchases_that_say_nothing_are_not_counted(db_session: AsyncSession) -> None:
    """Two directors' real purchases, and one of each kind that does not count."""
    real = await _directors(db_session, 2)
    for insider in real:
        await _buy(db_session, insider, 0)
    owner, planned, espp, drip, options, seller = [
        await _insider(db_session, name)
        for name in ("FUND LP", "PLANNED", "ESPP", "DRIP", "OPTIONS", "SELLER")
    ]
    await _buy(db_session, owner, 1, is_ten_percent_owner=True)
    await _buy(db_session, planned, 1, plan=True)
    await _buy(
        db_session,
        espp,
        1,
        footnotes={
            "F1": "Shares purchased by the administrator of the Employee Stock Purchase Plan"
        },
        is_officer=True,
    )
    await _buy(
        db_session,
        drip,
        1,
        footnotes={"F2": "Acquired through the issuer's dividend reinvestment plan."},
    )
    await _buy(db_session, options, 1, derivative=True)
    await _form4(db_session, seller, _line("S", day(1)), is_director=True)

    rebuilt = await detect_clusters(db_session)

    assert await _episodes(db_session) == []
    assert dict(rebuilt.lines) == {
        "counted": 2,
        "role": 1,
        "10b5_1": 1,
        "plan": 2,
        "derivative": 1,
    }


async def test_an_officer_who_is_not_a_director_counts(db_session: AsyncSession) -> None:
    a, b, c = await _directors(db_session, 3)
    await _buy(db_session, a, 0)
    await _buy(db_session, b, 1, is_officer=True, officer_title="CFO")
    await _buy(db_session, c, 2, is_officer=True, is_ten_percent_owner=True)

    await detect_clusters(db_session)

    assert await _episodes(db_session) == [(NVIDIA, day(0), day(2), 3, 3)]


async def test_lines_in_two_classes_or_unlinked_leave_the_cluster_unlinked(
    db_session: AsyncSession,
) -> None:
    len_a, len_b = await _lennar(db_session)
    a, b, c = await _directors(db_session, 3)
    await _buy(db_session, a, 0, issuer_cik=LENNAR, security_id=len_a)
    await _buy(db_session, b, 1, issuer_cik=LENNAR, security_id=len_b)
    await _buy(db_session, c, 2, issuer_cik=LENNAR, security_id=len_a)

    await detect_clusters(db_session)

    assert (
        await db_session.scalar(
            select(InsiderCluster.security_id).where(InsiderCluster.issuer_cik == LENNAR)
        )
        is None
    )


# --- rebuilt whole --------------------------------------------------------------


async def test_a_second_run_writes_the_same_and_reports_nothing_new(
    db_session: AsyncSession,
) -> None:
    insiders = await _directors(db_session, 3)
    for offset, insider in enumerate(insiders):
        await _buy(db_session, insider, offset)
    await detect_clusters(db_session)
    first = await _episodes(db_session)

    again = await detect_clusters(db_session)

    assert await _episodes(db_session) == first
    assert (again.added, again.removed) == ((), ())


async def test_a_cluster_whose_lines_are_gone_is_removed(db_session: AsyncSession) -> None:
    """As when a 4/A supersedes a Form 4: its lines are deleted."""
    insiders = await _directors(db_session, 3)
    accessions = [await _buy(db_session, insider, n) for n, insider in enumerate(insiders)]
    await detect_clusters(db_session)

    await db_session.execute(
        delete(InsiderTransaction).where(
            InsiderTransaction.filing_id
            == select(Filing.id).where(Filing.accession_no == accessions[0]).scalar_subquery()
        )
    )
    rebuilt = await detect_clusters(db_session)

    assert await _episodes(db_session) == []
    assert rebuilt.removed == ((NVIDIA, day(0)),)


# --- against an oracle ----------------------------------------------------------


@dataclass(frozen=True)
class _Buy:
    insider: int
    offset: int


def _oracle(buys: list[_Buy], window: int, minimum: int) -> list[tuple[int, int, int, int]]:
    """Episodes as ``(start, end, insiders, lines)`` offsets, by another definition.

    Every purchase day anchors the set of purchases within ``window`` days
    before it. Sets with ``minimum`` distinct insiders qualify, and those
    sharing a purchase are joined, transitively: the connected components.
    An episode is every purchase between its component's first and last.
    """
    qualifying: list[frozenset[int]] = []
    for anchor in sorted({buy.offset for buy in buys}):
        members = frozenset(
            index for index, buy in enumerate(buys) if anchor - window <= buy.offset <= anchor
        )
        if len({buys[index].insider for index in members}) >= minimum:
            qualifying.append(members)
    components: list[set[int]] = []
    for members in qualifying:
        joined = [component for component in components if component & members]
        merged = set(members).union(*joined)
        components = [component for component in components if component not in joined]
        components.append(merged)
    episodes = []
    for component in components:
        start = min(buys[index].offset for index in component)
        end = max(buys[index].offset for index in component)
        inside = [buy for buy in buys if start <= buy.offset <= end]
        episodes.append((start, end, len({buy.insider for buy in inside}), len(inside)))
    return sorted(episodes)


async def test_the_episodes_agree_with_an_oracle_on_random_histories(
    db_session: AsyncSession,
) -> None:
    rng = random.Random(613)
    insiders = await _directors(db_session, 8)
    buys = [_Buy(insider=rng.randrange(8), offset=rng.randrange(400)) for _ in range(70)]
    for buy in buys:
        await _buy(db_session, insiders[buy.insider], buy.offset)

    rebuilt: ClusterRebuild = await detect_clusters(db_session)

    for window in WINDOWS:
        for minimum in MIN_INSIDERS:
            got = [
                ((start - START).days, (end - START).days, count, lines)
                for _, start, end, count, lines in await _episodes(
                    db_session, window=window, min_insiders=minimum
                )
            ]
            assert got == _oracle(buys, window, minimum), (window, minimum)
            assert rebuilt.written[window, minimum] == len(got)
    # Not a vacuous agreement: sets with several episodes, and sets that differ.
    assert max(rebuilt.written.values()) > 1 and len(set(rebuilt.written.values())) > 2


# --- the endpoint ---------------------------------------------------------------


async def _three_clusters(session: AsyncSession) -> int:
    """NVDA in March (3 insiders, $3,000) and May (4, $40,000), Apple in April (3, $90,000)."""
    nvda = await _security(session, NVDA_CUSIP, "NVDA")
    insiders = await _directors(session, 4)
    for n, insider in enumerate(insiders[:3]):
        await _buy(session, insider, n, security_id=nvda)
    for n, insider in enumerate(insiders):
        await _buy(session, insider, 60 + n, security_id=nvda, price="100", shares="100")
    for n, insider in enumerate(insiders[:3]):
        await _buy(session, insider, 30 + n, issuer_cik=APPLE, price="300", shares="100")
    await detect_clusters(session)
    return nvda


async def test_the_default_is_30_days_and_3_insiders_latest_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _three_clusters(db_session)

    body = await _get(client, "/v1/insiders/clusters")

    assert [(row["issuer_cik"], row["window_start"]) for row in body["data"]] == [
        (NVIDIA, str(day(60))),
        (APPLE, str(day(30))),
        (NVIDIA, str(day(0))),
    ]
    assert body["meta"]["window"] == 30 and body["meta"]["min_insiders"] == 3
    assert any("10b5-1" in rule for rule in body["meta"]["rules"])
    assert body["meta"]["refreshed_at"] is not None


async def test_a_cluster_names_its_stock_and_who_bought(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _three_clusters(db_session)

    may = (await _get(client, "/v1/insiders/clusters"))["data"][0]

    assert may | {"insiders": None} == {
        "issuer_cik": NVIDIA,
        "issuer_name": f"ISSUER {NVIDIA}",
        "cusip": NVDA_CUSIP,
        "ticker": "NVDA",
        "current_ticker": "NVDA",
        "delisted_by": None,
        "window_start": str(day(60)),
        "window_end": str(day(63)),
        "insider_count": 4,
        "transaction_count": 4,
        "total_shares": "400.0000",
        "total_value": "40000.00",
        "insiders": None,
    }
    assert [insider["insider_name"] for insider in may["insiders"]] == [
        f"DIRECTOR {n}" for n in range(4)
    ]
    assert may["insiders"][0] | {"insider_cik": None} == {
        "insider_cik": None,
        "insider_name": "DIRECTOR 0",
        "roles": ["director"],
        "officer_title": None,
        "first_date": str(day(60)),
        "last_date": str(day(60)),
        "transaction_count": 1,
        "shares": "100.0000",
        "value_usd": "10000.00",
    }


async def test_window_and_min_insiders_choose_the_set(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _three_clusters(db_session)

    four = await _get(client, "/v1/insiders/clusters", min_insiders=4)
    wide = await _get(client, "/v1/insiders/clusters", window=90, min_insiders=3)

    assert [row["window_start"] for row in four["data"]] == [str(day(60))]
    # 90 days joins NVDA's March and May into one episode.
    assert [
        (row["issuer_cik"], row["window_start"], row["window_end"]) for row in wide["data"]
    ] == [
        (NVIDIA, str(day(0)), str(day(63))),
        (APPLE, str(day(30)), str(day(32))),
    ]


async def test_a_window_not_detected_ahead_of_time_is_a_422(client: AsyncClient) -> None:
    window = await client.get("/v1/insiders/clusters", params={"window": 45})
    minimum = await client.get("/v1/insiders/clusters", params={"min_insiders": 1})
    backwards = await client.get(
        "/v1/insiders/clusters", params={"from": "2026-06-01", "to": "2026-05-01"}
    )

    assert (window.status_code, minimum.status_code, backwards.status_code) == (422, 422, 422)
    assert "14, 30, 60, 90" in window.text


async def test_sorts_by_value_or_insiders(client: AsyncClient, db_session: AsyncSession) -> None:
    await _three_clusters(db_session)

    by_value = await _get(client, "/v1/insiders/clusters", sort="value")
    by_insiders = await _get(client, "/v1/insiders/clusters", sort="insiders", order="asc")

    assert [row["total_value"] for row in by_value["data"]] == ["90000.00", "40000.00", "3000.00"]
    assert [row["insider_count"] for row in by_insiders["data"]] == [3, 3, 4]


async def test_a_walk_returns_every_cluster_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _three_clusters(db_session)

    for sort in ("date", "value", "insiders"):
        walked = await _walk(client, "/v1/insiders/clusters", sort=sort)
        assert len({(row["issuer_cik"], row["window_start"]) for row in walked}) == 3


async def test_ticker_and_dates_narrow_the_clusters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _three_clusters(db_session)

    nvda = await _get(client, "/v1/insiders/clusters", ticker="NVDA")
    april = await _get(
        client, "/v1/insiders/clusters", **{"from": str(day(31)), "to": str(day(45))}
    )

    assert {row["issuer_cik"] for row in nvda["data"]} == {NVIDIA}
    assert len(nvda["data"]) == 2
    assert [row["issuer_cik"] for row in april["data"]] == [APPLE]


async def test_a_stocks_clusters_include_its_issuers_unlinked_ones(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    len_a, _ = await _lennar(db_session)
    a, b, c = await _directors(db_session, 3)
    await _buy(db_session, a, 0, issuer_cik=LENNAR, security_id=len_a)
    await _buy(db_session, b, 1, issuer_cik=LENNAR, security_id=None)
    await _buy(db_session, c, 2, issuer_cik=LENNAR, security_id=len_a)
    await detect_clusters(db_session)

    class_b = await _get(client, "/v1/insiders/clusters", ticker="LEN.B")

    assert [(row["issuer_cik"], row["cusip"]) for row in class_b["data"]] == [(LENNAR, None)]
