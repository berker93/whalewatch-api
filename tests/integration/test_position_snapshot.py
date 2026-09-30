"""``position_snapshot``: the published portfolio, rebuilt from real filings.

``test_effective_filing`` and ``test_amendments`` prove which filings count for
a period. These prove what the snapshot does with that answer: sums it into
positions, weighs them, and holds back a period that a suspect filing counts
toward. They run over what EDGAR actually holds for Berkshire's 2022Q4, 2023Q3
and 2023Q4 and for Soros's 2026Q2.

A filing is made suspect by the smallest change that does it. The cover page
declares one row more than the table has, so the entry-count guard fires and
nothing else about the filing moves.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import Filer, FilerCik, PositionSnapshot
from app.derived.position_snapshot import recompute_position_snapshot
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import ADDED_TO_PERIOD, RESTATED_PERIOD, by_slug

BERKSHIRE = "0001067983"
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


async def _filer(session: AsyncSession, slug: str, cik: str) -> int:
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id


async def _berkshire(session: AsyncSession) -> int:
    return await _filer(session, "berkshire-hathaway", BERKSHIRE)


async def _load(session: AsyncSession, slug: str, *, suspect: bool = False) -> None:
    """One committed fixture through the parser, the guards and the loader."""
    fixture = by_slug(slug)
    cover, table = fixture.parse()
    if suspect:
        cover = cover.model_copy(update={"table_entry_total": len(table.rows) + 1})
    normalised = normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table)
    assert (normalised.parse_status == "suspect") is suspect
    await load_filing(
        session,
        accession_no=fixture.accession_no,
        filed_at=fixture.filed_at,
        primary_doc=cover,
        normalised=normalised,
    )


async def _snapshot(
    session: AsyncSession, period: date | None = None, *, filer_id: int | None = None
) -> list[PositionSnapshot]:
    statement = select(PositionSnapshot).order_by(
        PositionSnapshot.filer_id, PositionSnapshot.period_of_report, PositionSnapshot.id
    )
    if period is not None:
        statement = statement.where(PositionSnapshot.period_of_report == period)
    if filer_id is not None:
        statement = statement.where(PositionSnapshot.filer_id == filer_id)
    return list(await session.scalars(statement))


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
    assert CHUBB in {row.cusip for row in rows}
    assert not any(row.suspect for row in rows)
    assert (rebuild.filers, rebuild.periods, rebuild.positions) == (1, 1, 46)
    assert (rebuild.suspect_periods, rebuild.withheld) == (0, 0)


async def test_a_weight_is_a_positions_share_of_the_period_with_options_left_out(
    db_session: AsyncSession,
) -> None:
    """Soros's 2026Q2 has twelve option lines. Each one's value is the notional
    of the underlying, so it gets no weight and is not in the total the others
    are divided by — or every real position would shrink by the hedge."""
    await _filer(db_session, "soros-fund-management", SOROS)
    await _load(db_session, "soros-2026q2-options")

    await recompute_position_snapshot(db_session)

    rows = await _snapshot(db_session)
    options = [row for row in rows if row.put_call is not None]
    positions = [row for row in rows if row.put_call is None]
    assert len(options) == 12
    assert all(row.weight is None for row in options)
    total = _value(positions)
    for row in positions:
        expected = (row.value_usd / total).quantize(Decimal("0.000001"), ROUND_HALF_UP)
        assert row.weight == expected, row.cusip
    weights = sum((row.weight for row in positions if row.weight is not None), start=Decimal(0))
    assert abs(weights - 1) < Decimal("0.0001")


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


async def test_recomputing_one_filer_leaves_the_others_as_they_were(
    db_session: AsyncSession,
) -> None:
    """Berkshire's Q4 changes and is rebuilt. Soros's rows keep their ids, so
    they were not so much as rewritten."""
    berkshire = await _berkshire(db_session)
    soros = await _filer(db_session, "soros-fund-management", SOROS)
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, "soros-2026q2-options")
    await recompute_position_snapshot(db_session)
    before = [(row.id, row.value_usd) for row in await _snapshot(db_session, filer_id=soros)]

    await _load(db_session, Q4_NEW_HOLDINGS)
    rebuild = await recompute_position_snapshot(db_session, filer_id=berkshire)

    after = [(row.id, row.value_usd) for row in await _snapshot(db_session, filer_id=soros)]
    assert after == before
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
                    "TRUNCATE position_snapshot, holding, filing, security, filer_cik, filer "
                    "RESTART IDENTITY CASCADE"
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


def test_recompute_says_what_it_published_and_what_it_withheld(committed: AsyncEngine) -> None:
    _commit(committed, ("berkshire-2022q4-dollars", False), (Q4_ORIGINAL, True))

    result = CliRunner().invoke(app, ["recompute"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "recompute  position_snapshot for every filer: 49 positions in 1 period of 1 filer",
        "  withheld    1 period with a suspect filing — check-data lists them; "
        "--include-suspect publishes them",
    ]
    assert _count_snapshot(committed) == 49


def test_recompute_include_suspect_says_what_it_published_unchecked(
    committed: AsyncEngine,
) -> None:
    _commit(committed, ("berkshire-2022q4-dollars", False), (Q4_ORIGINAL, True))

    result = CliRunner().invoke(
        app, ["recompute", "--filer", "berkshire-hathaway", "--include-suspect"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "recompute  position_snapshot for berkshire-hathaway: 90 positions in 2 periods of 1 filer",
        "  suspect     1 period with a suspect filing published, every row marked suspect",
    ]


def test_recompute_refuses_a_filer_that_does_not_exist(committed: AsyncEngine) -> None:
    """Rather than rebuilding nobody's rows and reporting success."""
    _commit(committed, ("berkshire-2022q4-dollars", False))

    result = CliRunner().invoke(app, ["recompute", "--filer", "berkshire-hathway"])

    assert result.exit_code == 1
    assert "no filer has the slug 'berkshire-hathway'" in result.stderr
    assert _count_snapshot(committed) == 0
