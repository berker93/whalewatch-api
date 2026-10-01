"""``position_snapshot``: the published portfolio, rebuilt from real filings.

``test_effective_filing`` and ``test_amendments`` prove which filings count for
a period. These prove what the snapshot does with that answer: sums it into
positions, leaves out everything that is not shares of stock, weighs what is
left, traces each row to its filing, and holds back a period that a suspect
filing counts toward. Most run over what EDGAR actually holds for Berkshire,
Point72 and Soros. Where the point is an exact set of positions, the filings
are made up.

A filing is made suspect by the smallest change that does it. The cover page
declares one row more than the table has, so the entry-count guard fires and
nothing else about the filing moves.
"""

import asyncio
import logging
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import Text, func, insert, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import (
    AmendmentKind,
    Filer,
    FilerCik,
    Filing,
    Holding,
    PositionChange,
    PositionSnapshot,
    Security,
)
from app.derived.position_snapshot import recompute_position_snapshot, snapshot_positions
from app.derived.scope import resolve_scope
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import ADDED_TO_PERIOD, RESTATED_PERIOD, by_slug, load_fixtures

BERKSHIRE = "0001067983"
POINT72 = "0001603466"
SOROS = "0001029160"
Q4_2022 = date(2022, 12, 31)
Q3 = date(2023, 9, 30)
Q4 = date(2023, 12, 31)
CHUBB = "H1467J104"

Q3_ORIGINAL, Q3_RESTATEMENT, Q3_NEW_HOLDINGS = RESTATED_PERIOD
Q4_ORIGINAL, Q4_NEW_HOLDINGS = ADDED_TO_PERIOD

# The filers' own tableValueTotal, in whole dollars.
Q3_BOOK = Decimal(313_257_308_189)
Q3_CHUBB = Decimal(1_695_320_075)
Q4_BOOK = Decimal(347_358_074_461)
Q4_CHUBB = Decimal(4_542_600_000)

# The made-up filings: one fund, one quarter, three stocks at $10.
FUND_CIK = "0000000001"
SECOND_CIK = "0000000002"
Q1_2024 = date(2024, 3, 31)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"

_accessions = count(1)


async def _filer(session: AsyncSession, slug: str, cik: str) -> int:
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id


async def _berkshire(session: AsyncSession) -> int:
    return await _filer(session, "berkshire-hathaway", BERKSHIRE)


async def _load(session: AsyncSession, slug: str, *, suspect: bool = False) -> int:
    """One committed fixture through the parser, the guards and the loader; its filing's id."""
    fixture = by_slug(slug)
    cover, table = fixture.parse()
    if suspect:
        cover = cover.model_copy(update={"table_entry_total": len(table.rows) + 1})
    normalised = normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table)
    assert (normalised.parse_status == "suspect") is suspect
    result = await load_filing(
        session,
        accession_no=fixture.accession_no,
        filed_at=fixture.filed_at,
        primary_doc=cover,
        normalised=normalised,
    )
    return result.filing_id


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal


def held(cusip: str, shares: int) -> _Held:
    """``shares`` of ``cusip``, at $10 a share."""
    return _Held(cusip, Decimal(shares))


async def _fund(session: AsyncSession, *, overlap: str = "successor") -> int:
    """A made-up filer with two CIKs, the second listed last."""
    filer_id = await session.scalar(
        insert(Filer).values(name="A Fund", slug="a-fund", overlap=overlap).returning(Filer.id)
    )
    assert filer_id is not None
    for priority, cik in enumerate((FUND_CIK, SECOND_CIK)):
        await session.execute(
            insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=priority)
        )
    return filer_id


async def _filing(
    session: AsyncSession,
    filer_id: int,
    *positions: _Held,
    cik: str = FUND_CIK,
    form: str = "13F-HR",
    kind: AmendmentKind | None = None,
    days_later: int = 0,
) -> int:
    """A made-up 13F for 2024Q1 holding ``positions``, inserted as loaded; its id.

    Straight into the tables, not through the loader. The resolution under test
    starts from loaded filings, and the positions have to be exactly these.
    """
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-24-{next(_accessions):06d}",
            cik=cik,
            filer_id=filer_id,
            form_type=form,
            period_of_report=Q1_2024,
            filed_at=datetime(2024, 5, 15, 16, tzinfo=UTC) + timedelta(days=days_later),
            value_multiplier=1,
            amendment_kind=kind,
            parse_status="ok",
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
                period_of_report=Q1_2024,
                cusip=position.cusip,
                value_usd=position.shares * 10,
                shares=position.shares,
                sshprnamt_type="SH",
            )
        )
    return filing_id


async def _snapshot(
    session: AsyncSession, period: date | None = None, *, filer_id: int | None = None
) -> list[PositionSnapshot]:
    statement = select(PositionSnapshot).order_by(
        PositionSnapshot.filer_id, PositionSnapshot.period_of_report, PositionSnapshot.security_id
    )
    if period is not None:
        statement = statement.where(PositionSnapshot.period_of_report == period)
    if filer_id is not None:
        statement = statement.where(PositionSnapshot.filer_id == filer_id)
    return list(await session.scalars(statement))


async def _by_cusip(
    session: AsyncSession, period: date = Q1_2024
) -> dict[str, tuple[Decimal, Decimal, int]]:
    """One period of the snapshot as ``cusip -> (shares, value_usd, source_filing_id)``."""
    rows = await session.execute(
        select(
            Security.cusip,
            PositionSnapshot.shares,
            PositionSnapshot.value_usd,
            PositionSnapshot.source_filing_id,
        )
        .join(Security, Security.id == PositionSnapshot.security_id)
        .where(PositionSnapshot.period_of_report == period)
    )
    return {row.cusip: (row.shares, row.value_usd, row.source_filing_id) for row in rows}


def _value(rows: list[PositionSnapshot]) -> Decimal:
    return sum((row.value_usd for row in rows), start=Decimal(0))


# --- what a period is ----------------------------------------------------------


async def test_a_period_is_the_filings_that_count_summed_into_positions(
    db_session: AsyncSession,
) -> None:
    """All three 2023Q3 filings: the restatement's book plus the Chubb row
    released six months later — and not the original too, which would be the
    same $313bn twice."""
    await _berkshire(db_session)
    for slug in RESTATED_PERIOD:
        await _load(db_session, slug)

    rebuild = await recompute_position_snapshot(db_session)

    rows = await _snapshot(db_session, Q3)
    assert _value(rows) == Q3_BOOK + Q3_CHUBB
    assert len(rows) == 46
    assert not any(row.suspect for row in rows)
    assert (rebuild.filers, rebuild.periods, rebuild.positions) == (1, 1, 46)
    assert (rebuild.suspect_periods, rebuild.withheld) == (0, 0)


async def test_a_restatement_resolves_to_its_own_holdings_not_the_union(
    db_session: AsyncSession,
) -> None:
    """The restatement corrects Alpha and replaces Bravo with Charlie. The union
    of the two filings would keep Bravo, which the filer took back, and add the
    two Alphas together. The period is what the restatement says, and nothing
    it dropped."""
    fund = await _fund(db_session)
    await _filing(db_session, fund, held(ALPHA, 100), held(BRAVO, 200))
    restatement = await _filing(
        db_session,
        fund,
        held(ALPHA, 150),
        held(CHARLIE, 300),
        form="13F-HR/A",
        kind=AmendmentKind.RESTATEMENT,
        days_later=2,
    )

    await recompute_position_snapshot(db_session)

    assert await _by_cusip(db_session) == {
        ALPHA: (Decimal(150), Decimal(1_500), restatement),
        CHARLIE: (Decimal(300), Decimal(3_000), restatement),
    }


async def test_a_real_restated_period_is_the_restatement_alone(db_session: AsyncSession) -> None:
    """Berkshire's 2023Q3 original and restatement are the same lines to the
    dollar, so the union is the quarter twice over."""
    await _berkshire(db_session)
    await _load(db_session, Q3_ORIGINAL)
    restatement = await _load(db_session, Q3_RESTATEMENT)

    await recompute_position_snapshot(db_session)

    rows = await _snapshot(db_session, Q3)
    assert (len(rows), _value(rows)) == (45, Q3_BOOK)
    assert {row.source_filing_id for row in rows} == {restatement}


async def test_one_security_in_several_of_a_period_s_filings_is_one_position(
    db_session: AsyncSession,
) -> None:
    """Alpha in the original, and again in the new-holdings amendment that
    released the tranche confidential treatment held back. One position, both
    lines summed, traced to the amendment: the filing that last changed it."""
    fund = await _fund(db_session)
    original = await _filing(db_session, fund, held(ALPHA, 100), held(BRAVO, 200))
    addition = await _filing(
        db_session,
        fund,
        held(ALPHA, 50),
        form="13F-HR/A",
        kind=AmendmentKind.NEW_HOLDINGS,
        days_later=90,
    )

    await recompute_position_snapshot(db_session)

    assert await _by_cusip(db_session) == {
        ALPHA: (Decimal(150), Decimal(1_500), addition),
        BRAVO: (Decimal(200), Decimal(2_000), original),
    }


async def test_under_a_sum_policy_a_stock_two_ciks_hold_is_one_position(
    db_session: AsyncSession,
) -> None:
    """Two advisers filing for one manager, both counted. A stock both of them
    hold is one position, traced to whichever of the two filed last."""
    fund = await _fund(db_session, overlap="sum")
    await _filing(db_session, fund, held(ALPHA, 100), cik=FUND_CIK)
    later = await _filing(
        db_session, fund, held(ALPHA, 40), held(BRAVO, 10), cik=SECOND_CIK, days_later=1
    )

    await recompute_position_snapshot(db_session)

    assert await _by_cusip(db_session) == {
        ALPHA: (Decimal(140), Decimal(1_400), later),
        BRAVO: (Decimal(10), Decimal(100), later),
    }


async def test_each_position_traces_to_the_filing_it_was_read_from(
    db_session: AsyncSession,
) -> None:
    """Berkshire's 2023Q4: forty-one positions from the original, and Chubb
    from the amendment that released it the following May."""
    await _berkshire(db_session)
    original = await _load(db_session, Q4_ORIGINAL)
    addition = await _load(db_session, Q4_NEW_HOLDINGS)

    await recompute_position_snapshot(db_session)

    sources = {cusip: source for cusip, (_, _, source) in (await _by_cusip(db_session, Q4)).items()}
    assert sources.pop(CHUBB) == addition
    assert len(sources) == 41
    assert set(sources.values()) == {original}


# --- what a position is ----------------------------------------------------------


async def test_options_and_principal_amounts_are_left_out(db_session: AsyncSession) -> None:
    """Soros's 2026Q2 has 238 lines of stock, 16 principal amounts, and 12
    option lines, 8 of them on stocks it also holds. The snapshot is the 238,
    share for share. Calls on a stock are not more of the stock, and a
    principal amount is not a number of shares."""
    await _filer(db_session, "soros-fund-management", SOROS)
    await _load(db_session, "soros-2026q2-options")

    await recompute_position_snapshot(db_session)

    _, table = by_slug("soros-2026q2-options").parse()
    stock: defaultdict[str, Decimal] = defaultdict(Decimal)
    for row in table.rows:
        if row.put_call is None and row.sh_prn_type == "SH":
            stock[row.cusip] += row.shares
    optioned = {row.cusip for row in table.rows if row.put_call is not None}
    assert (len(stock), len(optioned & stock.keys())) == (238, 8)

    snapshot = await _by_cusip(db_session, date(2026, 6, 30))
    assert {cusip: shares for cusip, (shares, _, _) in snapshot.items()} == stock


async def test_weights_per_filer_and_period_sum_to_100(db_session: AsyncSession) -> None:
    """Every fixture at once: Berkshire's four periods, either side of the units
    cutover and two of them amended, Point72's 1,289 stocks, and Soros's 238.
    Six places of rounding move a sum by at most half a millionth per row."""
    await _berkshire(db_session)
    await _filer(db_session, "point72-asset-management", POINT72)
    await _filer(db_session, "soros-fund-management", SOROS)
    for fixture in load_fixtures():
        await _load(db_session, fixture.slug)

    await recompute_position_snapshot(db_session)

    sums = (
        await db_session.execute(
            select(
                PositionSnapshot.filer_id,
                PositionSnapshot.period_of_report,
                func.sum(PositionSnapshot.weight_pct),
            ).group_by(PositionSnapshot.filer_id, PositionSnapshot.period_of_report)
        )
    ).all()
    assert len(sums) == 6
    for filer_id, period, weights in sums:
        assert abs(weights - 100) <= Decimal("0.01"), (filer_id, period, weights)


async def test_the_window_function_weighs_each_position_as_a_self_join_would(
    db_session: AsyncSession,
) -> None:
    """The window function puts its period's total on every row in one pass.
    The self-join gets the same total the long way round, grouping the
    positions a second time and joining the totals back on. Whatever the plan,
    the answer has to be the same, digit for digit, across two filers."""
    await _berkshire(db_session)
    await _filer(db_session, "soros-fund-management", SOROS)
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, Q4_NEW_HOLDINGS)
    await _load(db_session, "soros-2026q2-options")

    snapshot = snapshot_positions().subquery()
    totals = (
        select(
            snapshot.c.filer_id,
            snapshot.c.period_of_report,
            func.sum(snapshot.c.value_usd).label("total"),
        )
        .group_by(snapshot.c.filer_id, snapshot.c.period_of_report)
        .subquery()
    )
    rows = (
        await db_session.execute(
            select(
                snapshot.c.weight_pct,
                (snapshot.c.value_usd * 100 / totals.c.total).label("by_self_join"),
            ).join(
                totals,
                (totals.c.filer_id == snapshot.c.filer_id)
                & (totals.c.period_of_report == snapshot.c.period_of_report),
            )
        )
    ).all()

    assert len(rows) == 42 + 238
    assert [row.weight_pct for row in rows] == [row.by_self_join for row in rows]


# --- what is withheld ----------------------------------------------------------


async def test_a_period_a_suspect_filing_counts_toward_is_withheld(
    db_session: AsyncSession,
) -> None:
    await _berkshire(db_session)
    await _load(db_session, Q4_ORIGINAL, suspect=True)
    await _load(db_session, "berkshire-2022q4-dollars")

    rebuild = await recompute_position_snapshot(db_session)

    assert {row.period_of_report for row in await _snapshot(db_session)} == {Q4_2022}
    assert (rebuild.periods, rebuild.suspect_periods, rebuild.withheld) == (1, 1, 1)


async def test_include_suspect_publishes_the_period_and_marks_every_row(
    db_session: AsyncSession,
) -> None:
    """Published without a check is never indistinguishable from published with one."""
    await _berkshire(db_session)
    await _load(db_session, Q4_ORIGINAL, suspect=True)
    await _load(db_session, "berkshire-2022q4-dollars")

    rebuild = await recompute_position_snapshot(db_session, include_suspect=True)

    suspect = await _snapshot(db_session, Q4)
    assert _value(suspect) == Q4_BOOK
    assert all(row.suspect for row in suspect)
    assert not any(row.suspect for row in await _snapshot(db_session, Q4_2022))
    assert (rebuild.periods, rebuild.suspect_periods, rebuild.withheld) == (2, 1, 0)


async def test_one_suspect_addition_withholds_the_whole_period(db_session: AsyncSession) -> None:
    """Not the original without it. That is the portfolio before confidential
    treatment expired, and a quarter later it would read as Berkshire buying
    $4.5bn of Chubb."""
    await _berkshire(db_session)
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, Q4_NEW_HOLDINGS, suspect=True)

    rebuild = await recompute_position_snapshot(db_session)

    assert await _snapshot(db_session, Q4) == []
    assert rebuild.withheld == 1


async def test_a_suspect_filing_a_restatement_replaced_does_not_hold_the_period_back(
    db_session: AsyncSession,
) -> None:
    """Nothing reads a replaced filing, so its guards have nothing left to say."""
    await _berkshire(db_session)
    await _load(db_session, Q3_ORIGINAL, suspect=True)
    await _load(db_session, Q3_RESTATEMENT)

    rebuild = await recompute_position_snapshot(db_session)

    rows = await _snapshot(db_session, Q3)
    assert _value(rows) == Q3_BOOK
    assert not any(row.suspect for row in rows)
    assert rebuild.suspect_periods == 0


async def test_a_suspect_restatement_does_not_fall_back_to_the_original(
    db_session: AsyncSession,
) -> None:
    """The original was restated because it was wrong. Withholding the period
    is not the same as publishing the filing it replaced."""
    await _berkshire(db_session)
    await _load(db_session, Q3_ORIGINAL)
    await _load(db_session, Q3_RESTATEMENT, suspect=True)

    await recompute_position_snapshot(db_session)

    assert await _snapshot(db_session, Q3) == []


# --- rebuilding ----------------------------------------------------------------


async def test_recomputing_replaces_the_snapshot_rather_than_adding_to_it(
    db_session: AsyncSession,
) -> None:
    await _berkshire(db_session)
    await _load(db_session, Q4_ORIGINAL)

    first = await recompute_position_snapshot(db_session)
    second = await recompute_position_snapshot(db_session)

    assert first == second
    assert len(await _snapshot(db_session)) == first.positions == 41


async def _tuples(session: AsyncSession, filer_id: int) -> list[tuple[str, Decimal]]:
    """A filer's rows as ``(ctid, value_usd)``. The ctid is where the row is
    stored, and a row deleted and inserted again is stored somewhere new, even
    unchanged and in the same transaction."""
    rows = await session.execute(
        select(literal_column("ctid::text", Text), PositionSnapshot.value_usd)
        .where(PositionSnapshot.filer_id == filer_id)
        .order_by(PositionSnapshot.security_id)
    )
    return [(ctid, value) for ctid, value in rows]


async def test_recomputing_one_filer_leaves_the_others_as_they_were(
    db_session: AsyncSession,
) -> None:
    """Berkshire's Q4 changes and is rebuilt. Soros's rows are where they were
    stored, so they were not so much as rewritten."""
    berkshire = await _berkshire(db_session)
    soros = await _filer(db_session, "soros-fund-management", SOROS)
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, "soros-2026q2-options")
    await recompute_position_snapshot(db_session)
    before = await _tuples(db_session, soros)

    await _load(db_session, Q4_NEW_HOLDINGS)
    rebuild = await recompute_position_snapshot(
        db_session, await resolve_scope(db_session, filer_id=berkshire)
    )

    assert await _tuples(db_session, soros) == before
    q4 = await _snapshot(db_session, Q4)
    assert (len(q4), _value(q4)) == (42, Q4_BOOK + Q4_CHUBB)
    assert (rebuild.filers, rebuild.positions) == (1, 42)


# --- the command ---------------------------------------------------------------


@pytest.fixture
def committed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[AsyncEngine]:
    """For the command, which commits through ``session_scope`` and so cannot
    see a test's rolled-back transaction: tables truncated around the test."""
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
                    "TRUNCATE position_change, position_snapshot, holding, filing, security, "
                    "filer_cik, filer RESTART IDENTITY CASCADE"
                )
            )

    asyncio.run(run())


def _commit(engine: AsyncEngine, *loads: tuple[str, bool]) -> None:
    async def run() -> None:
        async with AsyncSession(engine) as session:
            await _berkshire(session)
            for slug, suspect in loads:
                await _load(session, slug, suspect=suspect)
            await session.commit()

    asyncio.run(run())


def _count_snapshot(engine: AsyncEngine) -> int:
    async def run() -> int:
        async with AsyncSession(engine) as session:
            return len(await _snapshot(session))

    return asyncio.run(run())


def _count_changes(engine: AsyncEngine) -> int:
    async def run() -> int:
        async with AsyncSession(engine) as session:
            return await session.scalar(select(func.count()).select_from(PositionChange)) or 0

    return asyncio.run(run())


def _rebuild(stdout: str) -> list[str]:
    """The rebuild's lines, without the refresh-views lines that follow them."""
    return stdout.split("refresh-views  ")[0].splitlines()


def test_recompute_says_what_it_published_and_what_it_withheld(committed: AsyncEngine) -> None:
    _commit(committed, ("berkshire-2022q4-dollars", False), (Q4_ORIGINAL, True))

    result = CliRunner().invoke(app, ["recompute", "--all"])

    assert result.exit_code == 0, result.output
    assert _rebuild(result.stdout) == [
        "recompute  position_snapshot for every filer: 49 positions in 1 period of 1 filer",
        "  changes     position_change: 49 new, 0 add, 0 trim, 0 hold, 0 exit",
        "  withheld    1 period with a suspect filing — check-data lists them; "
        "--include-suspect publishes them",
    ]
    assert _count_snapshot(committed) == _count_changes(committed) == 49


def test_recompute_include_suspect_says_what_it_published_unchecked(
    committed: AsyncEngine,
) -> None:
    _commit(committed, ("berkshire-2022q4-dollars", False), (Q4_ORIGINAL, True))

    result = CliRunner().invoke(
        app, ["recompute", "--filer", "berkshire-hathaway", "--include-suspect"]
    )

    assert result.exit_code == 0, result.output
    assert _rebuild(result.stdout) == [
        "recompute  position_snapshot for berkshire-hathaway: 90 positions in 2 periods of 1 filer",
        # 2022Q4's 49 positions, less the 31 still held in 2023Q4, are 18 exits.
        "  changes     position_change: 59 new, 4 add, 6 trim, 21 hold, 18 exit",
        "  suspect     1 period with a suspect filing published, every row marked suspect",
    ]


def test_recompute_refuses_a_filer_that_does_not_exist(committed: AsyncEngine) -> None:
    """Rather than rebuilding nobody's rows and reporting success."""
    _commit(committed, ("berkshire-2022q4-dollars", False))

    result = CliRunner().invoke(app, ["recompute", "--filer", "berkshire-hathway"])

    assert result.exit_code == 1
    assert "no filer has the slug 'berkshire-hathway'" in result.stderr
    assert _count_snapshot(committed) == _count_changes(committed) == 0
