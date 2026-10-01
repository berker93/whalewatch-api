"""``position_change``: what each filer did to each position, rebuilt from the snapshot.

``test_position_snapshot`` proves what a period is. These prove what changed
between two of them: which positions are new, added to, trimmed or held, against
which previous period, and by how much. They also prove that the hold band
absorbs the few shares of drift a manager's counts show between quarters with
nobody having traded. Most run over made-up filings at $10 a share, where the
point is an exact set of positions. Berkshire's real quarters check the window
against the long way round.

A filing is made suspect as directly as the schema allows: ``parse_status``
says so, and ``parse_notes`` says why, which a suspect filing must.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from sqlalchemy import Text, func, insert, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Filer,
    FilerCik,
    Filing,
    Holding,
    PositionChange,
    PositionSnapshot,
    Security,
)
from app.derived.position_change import (
    HOLD_BAND_PCT,
    ChangeRebuild,
    position_changes,
    recompute_position_change,
)
from app.derived.position_snapshot import recompute_position_snapshot
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.fixtures_13f import load_fixtures

FUND_CIK = "0000000001"
OTHER_CIK = "0000000002"
Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"

BERKSHIRE = "0001067983"
Q3_2023 = date(2023, 9, 30)
Q4_2023 = date(2023, 12, 31)
CHUBB = "H1467J104"
APPLE = "037833100"

_accessions = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal
    price: Decimal


def held(cusip: str, shares: int, *, price: int = 10) -> _Held:
    """``shares`` of ``cusip``, at ``price`` dollars a share."""
    return _Held(cusip, Decimal(shares), Decimal(price))


async def _fund(session: AsyncSession, slug: str = "a-fund", cik: str = FUND_CIK) -> int:
    """A made-up filer with one CIK."""
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id


async def _quarter(
    session: AsyncSession,
    filer_id: int,
    period: date,
    *positions: _Held,
    cik: str = FUND_CIK,
    suspect: bool = False,
) -> None:
    """A made-up 13F-HR for ``period`` holding ``positions``, inserted as loaded.

    Straight into the tables, not through the loader: the changes under test
    start from loaded filings, and the positions have to be exactly these.
    """
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


async def _rebuild(
    session: AsyncSession, *, filer_id: int | None = None, include_suspect: bool = False
) -> ChangeRebuild:
    """What ``recompute`` does: the snapshot, then the changes from it."""
    await recompute_position_snapshot(session, filer_id=filer_id, include_suspect=include_suspect)
    return await recompute_position_change(session, filer_id=filer_id)


async def _changes(session: AsyncSession, period: date) -> dict[str, PositionChange]:
    """One period's changes, by CUSIP, read fresh: a rebuild replaced the rows
    under any copy the session already holds."""
    rows = await session.execute(
        select(Security.cusip, PositionChange)
        .join(Security, Security.id == PositionChange.security_id)
        .where(PositionChange.period_of_report == period)
        .execution_options(populate_existing=True)
    )
    return {cusip: change for cusip, change in rows.tuples()}


def _deltas(change: PositionChange) -> tuple[object, ...]:
    """``(action, shares_delta, shares_delta_pct, value_delta, weight_delta)``."""
    return (
        change.action,
        change.shares_delta,
        change.shares_delta_pct,
        change.value_delta,
        change.weight_delta,
    )


# --- the acceptance criteria ----------------------------------------------------


async def test_a_position_appearing_for_the_first_time_is_new_with_no_previous_figures(
    db_session: AsyncSession,
) -> None:
    """Bravo is bought in Q2. The filer has a Q1, so new is a claim about Q1:
    Bravo was not in it. The deltas count from zero, so that a flow summed
    across filers includes the ones who bought in fresh."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100), held(BRAVO, 300))

    await _rebuild(db_session)

    bravo = (await _changes(db_session, Q2))[BRAVO]
    assert bravo.action == "new"
    assert (bravo.prev_shares, bravo.prev_value_usd, bravo.prev_weight_pct) == (None, None, None)
    assert bravo.prev_period_of_report == Q1
    assert _deltas(bravo) == ("new", 300, None, 3_000, 75)


async def test_an_unchanged_position_is_hold_with_zero_deltas(db_session: AsyncSession) -> None:
    fund = await _fund(db_session)
    for period in (Q1, Q2):
        await _quarter(db_session, fund, period, held(ALPHA, 100), held(BRAVO, 300))

    await _rebuild(db_session)

    changes = await _changes(db_session, Q2)
    assert set(changes) == {ALPHA, BRAVO}
    for change in changes.values():
        assert _deltas(change) == ("hold", 0, 0, 0, 0)
        assert (change.prev_shares, change.prev_value_usd, change.prev_weight_pct) == (
            change.shares,
            change.value_usd,
            change.weight_pct,
        )


# --- add, trim, and the band ----------------------------------------------------


async def test_more_shares_is_an_add_and_fewer_is_a_trim(db_session: AsyncSession) -> None:
    """Alpha doubles and Bravo loses a third, at $10 throughout: a quarter of
    the book moves from one to the other, and every delta says so."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 300))
    await _quarter(db_session, fund, Q2, held(ALPHA, 200), held(BRAVO, 200))

    await _rebuild(db_session)

    changes = await _changes(db_session, Q2)
    assert _deltas(changes[ALPHA]) == ("add", 100, 100, 1_000, 25)
    assert _deltas(changes[BRAVO]) == ("trim", -100, Decimal("-33.333333"), -1_000, -25)


@pytest.mark.parametrize(
    ("shares_now", "action"),
    [
        pytest.param(1_000_003, "hold", id="a-dividend-reinvested"),
        pytest.param(1_000_100, "hold", id="up-0.01%-the-edge"),
        pytest.param(999_900, "hold", id="down-0.01%-the-edge"),
        pytest.param(1_000_101, "add", id="just-over"),
        pytest.param(999_899, "trim", id="just-under"),
    ],
)
async def test_the_hold_band_is_a_hundredth_of_a_percent_either_way(
    db_session: AsyncSession, shares_now: int, action: str
) -> None:
    """On a million shares, a hundred either way is a hold, and a hundred and
    one is not. The band decides the action and nothing else: a hold keeps
    the delta it has, so the deltas still add up to the position."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 1_000_000))
    await _quarter(db_session, fund, Q2, held(ALPHA, shares_now))

    await _rebuild(db_session)

    alpha = (await _changes(db_session, Q2))[ALPHA]
    assert (alpha.action, alpha.shares_delta) == (action, Decimal(shares_now - 1_000_000))


async def test_hold_is_judged_on_shares_not_value(db_session: AsyncSession) -> None:
    """Alpha tripled in price and the filer did nothing. Its value and weight
    move, and it is still a hold, as is Bravo, whose weight fell under it."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100, price=30), held(BRAVO, 100))

    await _rebuild(db_session)

    changes = await _changes(db_session, Q2)
    assert _deltas(changes[ALPHA]) == ("hold", 0, 0, 2_000, 25)
    assert _deltas(changes[BRAVO]) == ("hold", 0, 0, 0, -25)


# --- what previous means --------------------------------------------------------


async def test_a_position_sold_and_bought_back_is_new_again(db_session: AsyncSession) -> None:
    """Alpha is held in Q1, gone in Q2 and back in Q3 at the same size. LAG
    alone reaches back to Q1 and makes Q3 a hold, of a position the filer
    did not have a quarter ago."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _quarter(db_session, fund, Q2, held(BRAVO, 100))
    await _quarter(db_session, fund, Q3, held(ALPHA, 100), held(BRAVO, 100))

    await _rebuild(db_session)

    alpha = (await _changes(db_session, Q3))[ALPHA]
    assert (alpha.action, alpha.prev_period_of_report, alpha.prev_shares) == ("new", Q2, None)
    assert alpha.shares_delta == 100


async def test_a_filers_first_period_is_new_against_no_period_at_all(
    db_session: AsyncSession,
) -> None:
    """Everything in the first period on record is new. A null
    prev_period_of_report is what says the period is the first we have,
    rather than a quarter in which the filer bought everything."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 300))
    await _quarter(db_session, fund, Q2, held(ALPHA, 100), held(BRAVO, 300), held(CHARLIE, 50))

    rebuild = await _rebuild(db_session)

    first = await _changes(db_session, Q1)
    assert {(change.action, change.prev_period_of_report) for change in first.values()} == {
        ("new", None)
    }
    assert (await _changes(db_session, Q2))[CHARLIE].prev_period_of_report == Q1
    assert rebuild == ChangeRebuild(new=3, add=0, trim=0, hold=2)


async def test_a_quarter_with_nothing_filed_is_stepped_over(db_session: AsyncSession) -> None:
    """No 13F for Q2. Q3 is compared with Q1 and says so, rather than calling
    every position new because the calendar quarter before it is empty."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q3, held(ALPHA, 100))

    await _rebuild(db_session)

    alpha = (await _changes(db_session, Q3))[ALPHA]
    assert (alpha.action, alpha.prev_period_of_report) == ("hold", Q1)


# --- suspect periods ------------------------------------------------------------


async def test_a_period_withheld_for_a_suspect_filing_is_stepped_over(
    db_session: AsyncSession,
) -> None:
    """Q2 is withheld, so Q3 is compared with Q1: Alpha added to and Bravo
    held. A withheld quarter does not turn every position after it new, which
    would read as the manager who bought everything, a quarter later."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 150), held(BRAVO, 100), suspect=True)
    await _quarter(db_session, fund, Q3, held(ALPHA, 200), held(BRAVO, 100))

    await _rebuild(db_session)

    q3 = await _changes(db_session, Q3)
    against = {cusip: (change.action, change.prev_period_of_report) for cusip, change in q3.items()}
    assert against == {ALPHA: ("add", Q1), BRAVO: ("hold", Q1)}
    assert not any(change.suspect for change in q3.values())
    assert await _changes(db_session, Q2) == {}


async def test_include_suspect_marks_every_change_a_suspect_period_is_an_end_of(
    db_session: AsyncSession,
) -> None:
    """Published unchecked, Q2 is suspect, and so is every change into it or
    out of it. That includes Charlie's new in Q3, which is a claim about what
    the suspect filing did not list. Q1 had nothing to do with it."""
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 150), suspect=True)
    await _quarter(db_session, fund, Q3, held(ALPHA, 150), held(CHARLIE, 10))

    await _rebuild(db_session, include_suspect=True)

    suspect: dict[tuple[date, str], bool] = {}
    for period in (Q1, Q2, Q3):
        for cusip, change in (await _changes(db_session, period)).items():
            suspect[period, cusip] = change.suspect
    assert suspect == {
        (Q1, ALPHA): False,
        (Q2, ALPHA): True,
        (Q3, ALPHA): True,
        (Q3, CHARLIE): True,
    }
    assert (await _changes(db_session, Q3))[ALPHA].prev_period_of_report == Q2


# --- the window -----------------------------------------------------------------


async def _berkshire(session: AsyncSession) -> None:
    """Berkshire's four real quarters through the parser, the guards and the
    loader: 2022Q3 and Q4 either side of the units cutover, then 2023Q3 and Q4
    after a two-quarter gap, each amended."""
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


def _classify(shares: Decimal, before: Decimal | None) -> str:
    """The action, worked out again in Python, from the module's band."""
    if before is None:
        return "new"
    if abs(shares - before) * 100 <= before * HOLD_BAND_PCT:
        return "hold"
    return "add" if shares > before else "trim"


async def test_lag_agrees_with_looking_up_the_previous_period_the_long_way(
    db_session: AsyncSession,
) -> None:
    """For every position, the previous figures looked up by hand: the filer's
    previous published period, then the security in it. The window has to
    give the same answer row for row, the gap and both amended quarters
    included, and the action has to be the band's."""
    await _berkshire(db_session)

    rebuild = await _rebuild(db_session)

    snapshot = {
        (row.period_of_report, row.security_id): row
        for row in await db_session.scalars(select(PositionSnapshot))
    }
    periods = sorted({period for period, _ in snapshot})
    previous_period: dict[date, date | None] = dict(zip(periods, [None, *periods], strict=False))
    changes = list(await db_session.scalars(select(PositionChange)))

    assert len(periods) == 4
    assert len(changes) == len(snapshot)
    for change in changes:
        prev_period = previous_period[change.period_of_report]
        before = snapshot.get((prev_period, change.security_id)) if prev_period else None
        prev_shares = before.shares if before else None
        assert change.prev_period_of_report == prev_period
        assert (change.prev_shares, change.prev_value_usd, change.prev_weight_pct) == (
            (before.shares, before.value_usd, before.weight_pct) if before else (None, None, None)
        )
        assert change.action == _classify(change.shares, prev_shares)
        assert change.shares_delta == change.shares - (prev_shares or 0)
        assert change.value_delta == change.value_usd - (before.value_usd if before else 0)
    # 2022Q3 is the 49 new, the first period we have. The rest is what
    # Berkshire did over three quarters, one of them across the gap.
    assert rebuild == ChangeRebuild(new=62, add=12, trim=18, hold=94)


async def test_chubb_released_from_confidential_treatment_is_an_add_in_2023q4(
    db_session: AsyncSession,
) -> None:
    """Both quarters' Chubb positions arrived months late, in new-holdings
    amendments. Resolved through effective_filing, each quarter has the
    stake, and the second is larger: an add, not a new position."""
    await _berkshire(db_session)

    await _rebuild(db_session)

    chubb = (await _changes(db_session, Q4_2023))[CHUBB]
    assert (chubb.action, chubb.prev_period_of_report) == ("add", Q3_2023)
    assert chubb.value_delta == Decimal(4_542_600_000) - Decimal(1_695_320_075)


async def test_a_real_purchase_smaller_than_any_trade_is_not_mistaken_for_drift(
    db_session: AsyncSession,
) -> None:
    """The smallest move in Berkshire's four quarters is Apple's in 2022Q4:
    333,856 more shares on 894.8 million, up 0.037%. The band is a quarter of
    that, so it is an add. The band absorbs a few shares of drift and nothing
    a manager actually bought."""
    await _berkshire(db_session)

    await _rebuild(db_session)

    apple = (await _changes(db_session, date(2022, 12, 31)))[APPLE]
    assert (apple.action, apple.shares_delta) == ("add", Decimal(333_856))
    assert apple.shares_delta_pct == Decimal("0.037311")


def _window_aggregates(node: dict[str, Any]) -> int:
    """The ``WindowAgg`` nodes in an ``EXPLAIN (FORMAT JSON)`` plan, at any depth."""
    own = 1 if node["Node Type"] == "WindowAgg" else 0
    return own + sum(_window_aggregates(child) for child in node.get("Plans", ()))


async def test_every_lag_is_computed_in_one_pass_over_one_window(db_session: AsyncSession) -> None:
    """The four LAGs spell out one window, and Postgres plans them as one
    WindowAgg over one sort. The other WindowAgg is the filer's periods. A
    LAG whose window drifted from the rest, say a tiebreaker added to one
    ORDER BY, would be a third WindowAgg and another sort of the snapshot."""
    connection = await db_session.connection()
    sql = position_changes().compile(
        dialect=connection.dialect, compile_kwargs={"literal_binds": True}
    )

    plan = (await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {sql}")).scalar_one()

    assert _window_aggregates(plan[0]["Plan"]) == 2


# --- rebuilding -----------------------------------------------------------------


async def test_recomputing_replaces_the_changes_rather_than_adding_to_them(
    db_session: AsyncSession,
) -> None:
    fund = await _fund(db_session)
    await _quarter(db_session, fund, Q1, held(ALPHA, 100))
    await _quarter(db_session, fund, Q2, held(ALPHA, 120), held(BRAVO, 10))

    first = await _rebuild(db_session)
    second = await _rebuild(db_session)

    assert first == second == ChangeRebuild(new=2, add=1, trim=0, hold=0)
    assert await db_session.scalar(select(func.count()).select_from(PositionChange)) == 3


async def _stored(session: AsyncSession, filer_id: int) -> list[tuple[str, date, str]]:
    """A filer's rows as ``(ctid, period, action)``. The ctid is where the row
    is stored, and a row deleted and inserted again is stored somewhere new."""
    rows = await session.execute(
        select(
            literal_column("ctid::text", Text),
            PositionChange.period_of_report,
            PositionChange.action,
        )
        .where(PositionChange.filer_id == filer_id)
        .order_by(PositionChange.period_of_report, PositionChange.security_id)
    )
    return [(ctid, period, action) for ctid, period, action in rows]


async def test_recomputing_one_filer_leaves_the_others_as_they_were(
    db_session: AsyncSession,
) -> None:
    """A's Q3 arrives and A is rebuilt. B's changes are where they were stored,
    so they were not so much as rewritten."""
    a = await _fund(db_session)
    b = await _fund(db_session, "b-fund", OTHER_CIK)
    for fund, cik in ((a, FUND_CIK), (b, OTHER_CIK)):
        await _quarter(db_session, fund, Q1, held(ALPHA, 100), cik=cik)
        await _quarter(db_session, fund, Q2, held(ALPHA, 100), cik=cik)
    await _rebuild(db_session)
    before = await _stored(db_session, b)

    await _quarter(db_session, a, Q3, held(ALPHA, 200))
    rebuild = await _rebuild(db_session, filer_id=a)

    assert await _stored(db_session, b) == before
    assert rebuild == ChangeRebuild(new=1, add=1, trim=0, hold=1)
