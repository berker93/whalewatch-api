"""The materialised views: what each one holds, and that it is what its live query says.

The ``check_*`` tests are the AC's: each view, refreshed, against its live query
in :mod:`app.derived.views`, row for row, over a made-up universe of filers
with gaps, late starters, a suspect period and prices that move, and over
Berkshire's real quarters. The view's SQL is written out in its migration and
the live query is SQLAlchemy, so these are two texts held to one answer. A
change to either one alone fails here.

The rest pin down what the numbers mean, on positions small enough to work out
by hand: that a price move is not buying, that a filer's first period is not
a flow, what turnover is, and why the median weight is there beside the
average. Then the refresh itself: concurrent, under the recompute lock, and
possible only because every view has a unique index.
"""

import asyncio
import logging
import random
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from sqlalchemy import Row, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import Filer, FilerCik, Filing, Holding, Security
from app.derived.recompute import RECOMPUTE_LOCK, recompute
from app.derived.scope import EVERYTHING
from app.derived.views import (
    CONSENSUS_HOLDINGS,
    FILER_SUMMARY,
    MATERIALISED_VIEWS,
    QUARTER_FLOWS,
    MaterialisedView,
    refresh_views,
)
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import load_fixtures

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
Q1_2025 = date(2025, 3, 31)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"

BERKSHIRE = "0001067983"

_accessions = count(1)
_ciks = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal
    price: Decimal


def held(cusip: str, shares: int, *, price: int | Decimal = 10) -> _Held:
    """``shares`` of ``cusip``, at ``price`` dollars a share."""
    return _Held(cusip, Decimal(shares), Decimal(price))


async def _fund(session: AsyncSession, slug: str = "a-fund") -> int:
    """A made-up filer, with a CIK of its own that its filings are filed under."""
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=1)
    )
    return filer_id


async def _quarter(
    session: AsyncSession,
    filer_id: int,
    period: date,
    *positions: _Held,
    suspect: bool = False,
) -> None:
    """A made-up 13F-HR for ``period`` holding ``positions``, inserted as loaded."""
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
            parse_status="suspect" if suspect else "ok",
            parse_notes=(
                [{"kind": "entry_count", "severity": "error", "detail": "made up"}]
                if suspect
                else None
            ),
        )
        .returning(Filing.id)
    )
    assert filing_id is not None
    for position in positions:
        await session.execute(
            pg_insert(Security)
            .values(cusip=position.cusip)
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


async def _publish(session: AsyncSession, *, include_suspect: bool = False) -> None:
    """``recompute --all``, then ``refresh-views``: what an operator runs once a period is in."""
    await recompute(session, EVERYTHING, include_suspect=include_suspect)
    await refresh_views(session)


async def _consensus(session: AsyncSession, period: date) -> dict[str, Row[Any]]:
    """One period of ``mv_consensus_holdings``, by CUSIP."""
    return await _by_cusip(session, CONSENSUS_HOLDINGS, period)


async def _flows(session: AsyncSession, period: date) -> dict[str, Row[Any]]:
    """One period of ``mv_quarter_flows``, by CUSIP."""
    return await _by_cusip(session, QUARTER_FLOWS, period)


async def _by_cusip(session: AsyncSession, view: Any, period: date) -> dict[str, Row[Any]]:
    rows = await session.execute(
        select(Security.cusip, view)
        .join(Security, Security.id == view.c.security_id)
        .where(view.c.period_of_report == period)
    )
    return {row.cusip: row for row in rows}


async def _summary(session: AsyncSession, filer_id: int, period: date) -> Row[Any]:
    """One ``(filer, period)`` of ``mv_filer_summary``."""
    row = (
        await session.execute(
            select(FILER_SUMMARY).where(
                FILER_SUMMARY.c.filer_id == filer_id,
                FILER_SUMMARY.c.period_of_report == period,
            )
        )
    ).one_or_none()
    assert row is not None, f"no summary for filer {filer_id} in {period}"
    return row


def _flow(row: Row[Any]) -> tuple[object, ...]:
    """``(bought, sold, net, net_shares, new, exits, buyers, sellers)``."""
    return (
        row.bought_value_usd,
        row.sold_value_usd,
        row.net_value_usd,
        row.net_shares,
        row.new_positions,
        row.exits,
        row.buyer_count,
        row.seller_count,
    )


# --- check_*: each view against its live query --------------------------------------


async def _universe(session: AsyncSession) -> None:
    """Eight made-up filers over five quarters, and Berkshire's real filings.

    Prices move every quarter, and positions are opened, added to, trimmed,
    held with a few shares of drift, and sold out of. Filer 3 skips 2024Q3.
    Filer 5 starts in 2024Q3. Filer 6's 2024Q4 is suspect, so it is withheld
    or, with ``--include-suspect``, published and marked. Some portfolios have
    more than ten positions and some fewer, and a few positions tie on value.
    """
    rng = random.Random(14)
    cusips = [f"{n:06d}AB{n % 10}" for n in range(1, 17)]
    periods = (Q1, Q2, Q3, Q4, Q1_2025)
    prices = {
        (cusip, period): Decimal(rng.randint(500, 40_000)) / 100
        for cusip in cusips
        for period in periods
    }
    for n in range(8):
        fund = await _fund(session, f"fund-{n}")
        shares: dict[str, int] = {}
        for period in periods:
            if (n, period) == (3, Q3) or (n == 5 and period < Q3):
                continue
            kept = {cusip: count for cusip, count in shares.items() if rng.random() > 0.2}
            for cusip in kept:
                move = rng.choice(("hold", "drift", "add", "trim"))
                if move == "drift":
                    kept[cusip] = max(1, kept[cusip] + rng.choice((-3, 2)))
                elif move == "add":
                    kept[cusip] += rng.randint(1_000, 50_000)
                elif move == "trim":
                    kept[cusip] = max(1, kept[cusip] - rng.randint(1_000, 50_000))
            for cusip in rng.sample(cusips, rng.randint(0, 6)):
                kept.setdefault(cusip, rng.choice((40_000, rng.randint(10_000, 90_000))))
            shares = kept
            await _quarter(
                session,
                fund,
                period,
                *(held(cusip, count, price=prices[cusip, period]) for cusip, count in kept.items()),
                suspect=(n, period) == (6, Q4),
            )
    await _berkshire(session)


async def _berkshire(session: AsyncSession) -> None:
    """Berkshire's real quarters, through the parser, the guards and the loader."""
    filer_id = await session.scalar(
        insert(Filer)
        .values(name="berkshire-hathaway", slug="berkshire-hathaway")
        .returning(Filer.id)
    )
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=BERKSHIRE, priority=1))
    for fixture in load_fixtures():
        if fixture.slug.startswith("berkshire-"):
            cover, table = fixture.parse()
            await load_filing(
                session,
                accession_no=fixture.accession_no,
                filed_at=fixture.filed_at,
                primary_doc=cover,
                normalised=normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table),
            )


async def _check(session: AsyncSession, view: MaterialisedView) -> None:
    """The view, row for row, against its live query. Keyed, so a failure says which rows."""

    def keyed(rows: list[Row[Any]]) -> dict[tuple[Any, ...], dict[str, Any]]:
        return {tuple(row._mapping[key] for key in view.key): row._asdict() for row in rows}

    stored = keyed(list(await session.execute(select(view.handle))))
    live = keyed(list(await session.execute(view.live())))

    assert stored, f"{view.name} is empty, so agreeing with it proves nothing"
    assert stored.keys() == live.keys()
    disagreeing = {key: (stored[key], live[key]) for key in stored if stored[key] != live[key]}
    assert disagreeing == {}


def _view(name: str) -> MaterialisedView:
    [view] = [view for view in MATERIALISED_VIEWS if view.name == name]
    return view


@pytest.mark.parametrize("include_suspect", [False, True])
async def test_check_mv_consensus_holdings(db_session: AsyncSession, include_suspect: bool) -> None:
    await _universe(db_session)
    await _publish(db_session, include_suspect=include_suspect)

    await _check(db_session, _view("mv_consensus_holdings"))


@pytest.mark.parametrize("include_suspect", [False, True])
async def test_check_mv_quarter_flows(db_session: AsyncSession, include_suspect: bool) -> None:
    await _universe(db_session)
    await _publish(db_session, include_suspect=include_suspect)

    await _check(db_session, _view("mv_quarter_flows"))


@pytest.mark.parametrize("include_suspect", [False, True])
async def test_check_mv_filer_summary(db_session: AsyncSession, include_suspect: bool) -> None:
    await _universe(db_session)
    await _publish(db_session, include_suspect=include_suspect)

    await _check(db_session, _view("mv_filer_summary"))


async def test_the_universe_exercises_what_the_checks_are_for(db_session: AsyncSession) -> None:
    """The checks above are only as good as their data. This holds the universe
    to containing every case: each action, a suspect row, a first period, a
    portfolio of more than ten and one of fewer, and a turnover."""
    await _universe(db_session)
    await _publish(db_session, include_suspect=True)

    actions = set(
        (await db_session.execute(text("SELECT DISTINCT action FROM position_change"))).scalars()
    )
    summaries = list(await db_session.execute(select(FILER_SUMMARY)))

    assert actions == {"new", "add", "trim", "hold", "exit"}
    assert await db_session.scalar(select(func.bool_or(QUARTER_FLOWS.c.suspect)))
    assert any(row.turnover_pct is None for row in summaries)
    assert any(row.turnover_pct is not None and row.turnover_pct > 0 for row in summaries)
    assert any(row.position_count > 10 for row in summaries)
    assert any(row.position_count <= 10 for row in summaries)


# --- mv_consensus_holdings -------------------------------------------------------------


async def test_one_conviction_holder_and_thirty_token_ones_are_two_different_stories(
    db_session: AsyncSession,
) -> None:
    """The ticket's example. Alpha is 20% of one manager's book and 0.5% of
    thirty others'. The average weight says a typical holder has a real
    position in it. The median says thirty-one holders and one of them means
    it. The view carries both, so a reader can tell the two apart."""
    conviction = await _fund(db_session, "conviction")
    await _quarter(db_session, conviction, Q1, held(ALPHA, 20), held(BRAVO, 80))
    for n in range(30):
        token = await _fund(db_session, f"token-{n}")
        await _quarter(db_session, token, Q1, held(ALPHA, 1), held(BRAVO, 199))

    await _publish(db_session)

    alpha = (await _consensus(db_session, Q1))[ALPHA]
    assert alpha.holder_count == 31
    # (20 + 30 x 0.5) / 31 = 35 / 31
    assert alpha.avg_weight_pct == Decimal("1.129032")
    assert alpha.median_weight_pct == Decimal("0.500000")


async def test_holders_totals_and_rank_per_security_per_period(db_session: AsyncSession) -> None:
    """Alpha and Bravo are both $3,000 held, so they share rank 1 and Charlie
    is 3rd, not 2nd. Alpha's two weights, 25% and 66.666667%, have a
    midpoint with seven decimal places, which the median keeps exactly before
    rounding it half up, as the average is."""
    a = await _fund(db_session, "a-fund")
    b = await _fund(db_session, "b-fund")
    await _quarter(db_session, a, Q1, held(ALPHA, 100), held(BRAVO, 300))
    await _quarter(db_session, b, Q1, held(ALPHA, 200), held(CHARLIE, 100))
    await _quarter(db_session, a, Q2, held(ALPHA, 100))

    await _publish(db_session)

    q1 = await _consensus(db_session, Q1)
    assert {
        cusip: (row.holder_count, row.total_value_usd, row.total_shares, row.value_rank)
        for cusip, row in q1.items()
    } == {
        ALPHA: (2, Decimal(3_000), Decimal(300), 1),
        BRAVO: (1, Decimal(3_000), Decimal(300), 1),
        CHARLIE: (1, Decimal(1_000), Decimal(100), 3),
    }
    assert (q1[ALPHA].avg_weight_pct, q1[ALPHA].median_weight_pct) == (
        Decimal("45.833334"),
        Decimal("45.833334"),
    )
    assert (q1[BRAVO].avg_weight_pct, q1[BRAVO].median_weight_pct) == (Decimal(75), Decimal(75))
    q2 = await _consensus(db_session, Q2)
    assert q2.keys() == {ALPHA}
    assert (q2[ALPHA].holder_count, q2[ALPHA].value_rank) == (1, 1)


async def test_a_holder_published_with_a_suspect_filing_marks_the_stock_suspect(
    db_session: AsyncSession,
) -> None:
    a = await _fund(db_session, "a-fund")
    b = await _fund(db_session, "b-fund")
    await _quarter(db_session, a, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _quarter(db_session, b, Q1, held(ALPHA, 100), suspect=True)

    await _publish(db_session, include_suspect=True)

    q1 = await _consensus(db_session, Q1)
    assert (q1[ALPHA].holder_count, q1[ALPHA].suspect) == (2, True)
    assert (q1[BRAVO].holder_count, q1[BRAVO].suspect) == (1, False)


# --- mv_quarter_flows -------------------------------------------------------------


async def test_a_price_move_is_not_buying_and_trades_are_valued_at_the_period_end_price(
    db_session: AsyncSession,
) -> None:
    """Alpha doubles from $10 to $20 in Q2. The fund that held its 100 shares
    bought nothing, though its position is worth $1,000 more. The fund that
    went to 110 bought 10 shares, $200 at Q2's price, not the $1,200 its
    position grew by. The fund that halved its position sold 50 shares, $1,000
    at Q2's price, though its position is worth exactly what it was. Summed
    as value_delta, that is $2,200 of net buying in a quarter of net selling."""
    holder = await _fund(db_session, "holder")
    adder = await _fund(db_session, "adder")
    trimmer = await _fund(db_session, "trimmer")
    for fund in (holder, adder, trimmer):
        await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, holder, Q2, held(ALPHA, 100, price=20))
    await _quarter(db_session, adder, Q2, held(ALPHA, 110, price=20))
    await _quarter(db_session, trimmer, Q2, held(ALPHA, 50, price=20))

    await _publish(db_session)

    alpha = (await _flows(db_session, Q2))[ALPHA]
    # bought, sold, net, net shares, new, exits, buyers, sellers
    assert _flow(alpha) == (200, 1_000, -800, -40, 0, 0, 1, 1)


async def test_an_exit_sells_at_its_last_price_and_a_new_position_buys_its_value(
    db_session: AsyncSession,
) -> None:
    """Bravo has no price in Q2 for the fund that sold out of it, so the exit
    is valued at Q1's $10: what it had. Charlie is bought fresh in Q2 and buys
    its whole value."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 50))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100), held(CHARLIE, 30, price=12))

    await _publish(db_session)

    q2 = await _flows(db_session, Q2)
    assert _flow(q2[BRAVO]) == (0, 500, -500, -50, 0, 1, 0, 1)
    assert _flow(q2[CHARLIE]) == (360, 0, 360, 30, 1, 0, 1, 0)
    assert _flow(q2[ALPHA]) == (0, 0, 0, 0, 0, 0, 0, 0)


async def test_a_filers_first_period_is_not_a_flow(db_session: AsyncSession) -> None:
    """The late fund's first period is Q2, so its 500 shares of Alpha are
    where its history starts, not a purchase. Counted, every filer added to
    the universe would be a buying spree in the quarter it starts. Q1 is
    everyone's first period, so it has no flows at all."""
    early = await _fund(db_session, "early")
    late = await _fund(db_session, "late")
    await _quarter(db_session, early, Q1, held(ALPHA, 100))
    await _quarter(db_session, early, Q2, held(ALPHA, 100))
    await _quarter(db_session, late, Q2, held(ALPHA, 500))

    await _publish(db_session)

    assert await _flows(db_session, Q1) == {}
    alpha = (await _flows(db_session, Q2))[ALPHA]
    assert _flow(alpha) == (0, 0, 0, 0, 0, 0, 0, 0)


async def test_a_change_into_or_out_of_a_suspect_period_marks_the_flow_suspect(
    db_session: AsyncSession,
) -> None:
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 200), suspect=True)
    await _quarter(db_session, fund, Q3, held(ALPHA, 200))

    await _publish(db_session, include_suspect=True)

    assert (await _flows(db_session, Q2))[ALPHA].suspect
    assert (await _flows(db_session, Q3))[ALPHA].suspect


# --- mv_filer_summary --------------------------------------------------------------


async def test_the_top_ten_weight_is_the_ten_largest_positions_share_of_the_book(
    db_session: AsyncSession,
) -> None:
    """Twelve positions worth $100 to $1,200, $7,800 in all. The ten largest
    are $7,500 of it."""
    fund = await _fund(db_session)
    await _quarter(
        db_session, fund, Q1, *(held(f"{n:06d}AB{n % 10}", 10 * n) for n in range(1, 13))
    )

    await _publish(db_session)

    summary = await _summary(db_session, fund, Q1)
    assert (summary.portfolio_value_usd, summary.position_count) == (Decimal(7_800), 12)
    assert summary.top10_weight_pct == Decimal("96.153846")


async def test_turnover_is_half_of_what_was_traded_over_the_previous_book(
    db_session: AsyncSession,
) -> None:
    """Q1's book is $1,000. In Q2 Alpha is sold out of ($500 at its last
    price), Charlie is bought ($300), and Bravo is added to by 10 shares at
    $12 ($120) as its price rises 20%. $920 traded, half of it is $460, and
    $460 of a $1,000 book is 46%. The rise in Bravo's price is not trading.
    Q1 is the fund's first period, with no previous book to turn over."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 50), held(BRAVO, 50))
    await _quarter(db_session, fund, Q2, held(BRAVO, 60, price=12), held(CHARLIE, 30))

    await _publish(db_session)

    assert (await _summary(db_session, fund, Q1)).turnover_pct is None
    q2 = await _summary(db_session, fund, Q2)
    assert (q2.portfolio_value_usd, q2.position_count) == (Decimal(1_020), 2)
    assert q2.turnover_pct == Decimal(46)


async def test_a_quarter_of_holding_still_through_a_rally_turned_over_nothing(
    db_session: AsyncSession,
) -> None:
    """Every price doubles and every share count drifts by a share or two,
    inside the hold band. Nothing was traded."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100_000), held(BRAVO, 50_000))
    await _quarter(
        db_session, fund, Q2, held(ALPHA, 100_002, price=20), held(BRAVO, 49_999, price=20)
    )

    await _publish(db_session)

    assert (await _summary(db_session, fund, Q2)).turnover_pct == 0


async def test_a_period_after_a_suspect_one_has_a_suspect_turnover(
    db_session: AsyncSession,
) -> None:
    """Q3's own filing passed, but its turnover is measured against Q2's
    book, which was published unchecked."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100), suspect=True)
    await _quarter(db_session, fund, Q3, held(ALPHA, 100))

    await _publish(db_session, include_suspect=True)

    assert [(await _summary(db_session, fund, period)).suspect for period in (Q1, Q2, Q3)] == [
        False,
        True,
        True,
    ]


# --- the refresh ----------------------------------------------------------------------


async def test_a_view_is_as_of_its_last_refresh(db_session: AsyncSession) -> None:
    """A recompute changes the tables and not the views. The check sees the
    difference, which is what makes it a check, and the refresh closes it."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100))
    await _publish(db_session)

    await _quarter(db_session, fund, Q3, held(ALPHA, 300), held(BRAVO, 10))
    await recompute(db_session, EVERYTHING)

    assert await _flows(db_session, Q3) == {}
    for view in MATERIALISED_VIEWS:
        with pytest.raises(AssertionError):
            await _check(db_session, view)

    await refresh_views(db_session)

    assert (await _flows(db_session, Q3)).keys() == {ALPHA, BRAVO}
    for view in MATERIALISED_VIEWS:
        await _check(db_session, view)


async def test_a_view_never_populated_is_refreshed_plainly_once(db_session: AsyncSession) -> None:
    """``WITH NO DATA`` leaves a view that refuses reads and concurrent
    refreshes alike. The refresh fills it the plain way, and the next one is
    concurrent again."""
    await db_session.execute(text("REFRESH MATERIALIZED VIEW mv_quarter_flows WITH NO DATA"))

    first = await refresh_views(db_session)
    second = await refresh_views(db_session)

    assert {view.name: view.concurrently for view in first} == {
        "mv_consensus_holdings": True,
        "mv_quarter_flows": False,
        "mv_filer_summary": True,
    }
    assert all(view.concurrently for view in second)


async def test_a_refresh_keeps_rebuilds_waiting_until_it_commits(
    migrated_engine: AsyncEngine,
) -> None:
    """So no rebuild commits between one view's refresh and the next, and all
    three are refreshed from the same tables."""
    take_lock = select(func.pg_try_advisory_xact_lock(RECOMPUTE_LOCK))
    async with (
        migrated_engine.connect() as first,
        migrated_engine.connect() as second,
        AsyncSession(bind=first) as refreshing,
    ):
        await refresh_views(refreshing)

        await second.begin()
        assert await second.scalar(take_lock) is False

        await refreshing.rollback()
        assert await second.scalar(take_lock) is True
        await second.rollback()


# --- the schema ---------------------------------------------------------------------


async def test_every_materialised_view_has_a_unique_index_a_concurrent_refresh_can_use(
    db_session: AsyncSession,
) -> None:
    """``REFRESH ... CONCURRENTLY`` needs a unique index on plain columns, with
    no ``WHERE``, to match old rows to new. Every materialised view in the
    database, so one added without an index, or without being listed in
    ``MATERIALISED_VIEWS``, fails here rather than on its first refresh."""
    rows = await db_session.execute(
        text("""
            SELECT m.matviewname,
                   array_agg(a.attname ORDER BY k.ord) FILTER (WHERE a.attname IS NOT NULL)
            FROM pg_matviews m
            LEFT JOIN pg_index i
              ON i.indrelid = format('%I.%I', m.schemaname, m.matviewname)::regclass
             AND i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL
            LEFT JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE m.schemaname = 'public'
            GROUP BY m.matviewname
        """)
    )
    indexed = {name: tuple(columns or ()) for name, columns in rows.tuples()}

    assert indexed == {view.name: view.key for view in MATERIALISED_VIEWS}


@pytest.mark.parametrize("view", MATERIALISED_VIEWS, ids=lambda view: view.name)
async def test_the_handle_the_live_query_and_the_view_have_the_same_columns(
    db_session: AsyncSession, view: MaterialisedView
) -> None:
    """In the same order, so a check compares like with like, and a read
    through the handle names columns the view has."""
    in_database = list(
        (
            await db_session.execute(
                text("""
                    SELECT attname FROM pg_attribute
                    WHERE attrelid = CAST(:name AS regclass) AND attnum > 0
                      AND NOT attisdropped
                    ORDER BY attnum
                """),
                {"name": view.name},
            )
        ).scalars()
    )

    assert [column.name for column in view.handle.c] == in_database
    assert list(view.live().selected_columns.keys()) == in_database


# --- the command ----------------------------------------------------------------------


@pytest.fixture
def committed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[AsyncEngine]:
    """For the command, which commits through ``session_scope`` and so cannot
    see a test's rolled-back transaction. Tables truncated around the test,
    and the views refreshed after, since a truncate does not reach them."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    _truncate(migrated_engine)
    yield migrated_engine
    _truncate(migrated_engine)
    # The command pointed logging at the runner's stderr, which is closed now.
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def _truncate(engine: AsyncEngine) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE ingestion_run, position_change, position_snapshot, holding, "
                    "filing, security, filer_cik, filer RESTART IDENTITY CASCADE"
                )
            )
            for view in MATERIALISED_VIEWS:
                await connection.execute(text(f"REFRESH MATERIALIZED VIEW {view.name}"))

    asyncio.run(run())


def test_refresh_views_refreshes_every_view_and_says_how_many_rows(
    committed: AsyncEngine,
) -> None:
    async def two_funds_published() -> None:
        async with AsyncSession(committed) as session:
            a = await _fund(session, "a-fund")
            b = await _fund(session, "b-fund")
            for fund in (a, b):
                await _quarter(session, fund, Q1, held(ALPHA, 100))
                await _quarter(session, fund, Q2, held(ALPHA, 200), held(BRAVO, 10))
            await recompute(session, EVERYTHING)
            await session.commit()

    asyncio.run(two_funds_published())

    result = CliRunner().invoke(app, ["refresh-views"])

    assert result.exit_code == 0, result.output
    # Q1: Alpha. Q2: Alpha and Bravo. Flows: Q2's two. Summaries: two funds, two periods.
    lines = [re.sub(r"\d+\.\ds", "0.0s", line) for line in result.stdout.splitlines()]
    assert lines == [
        "refresh-views  3 materialised views refreshed in 0.0s",
        "  mv_consensus_holdings          3 rows    0.0s",
        "  mv_quarter_flows               2 rows    0.0s",
        "  mv_filer_summary               4 rows    0.0s",
    ]


async def test_turnover_says_what_it_is_in_the_database(db_session: AsyncSession) -> None:
    """There is no single standard for turnover, so whoever reads the column
    in psql is told which one this is."""
    comment = await db_session.scalar(
        text("""
            SELECT col_description(a.attrelid, a.attnum) FROM pg_attribute a
            WHERE a.attrelid = 'mv_filer_summary'::regclass AND a.attname = 'turnover_pct'
        """)
    )

    assert comment is not None
    assert "sum(traded) / 2 / previous portfolio value" in comment
