"""``security_split``: splits detected from the holders' filings, and the changes across them.

Made-up filers holding one stock, ALPHA, across a 10-for-1 in Q2 2024: at $100 a
share in Q1 and $10.50 in Q2, a 5% quarter. Each test publishes a few of them,
some holding straight through, some buying or selling as well, and checks what
was detected and what ``position_change`` says each of them did. A reverse split
is the same at a tenth of the shares and ten times the price: the five years of
real data have none worth testing on.

``tests/test_security_split.py`` proves the rule on its evidence. These prove the
evidence is gathered right, the changes are adjusted by it, and ``recompute``
rebuilds every change a split moves, whoever's filing moved it.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import (
    Filer,
    FilerCik,
    Filing,
    Holding,
    PositionChange,
    Security,
    SecuritySplit,
)
from app.db.queries.checks import position_jumps
from app.derived.position_change import recompute_position_change
from app.derived.recompute import detect_splits, recompute
from app.derived.reconcile import reconcile
from app.derived.scope import EVERYTHING, Scope
from app.derived.views import MATERIALISED_VIEWS, refresh_views
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

D = Decimal

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
ALPHA = "11111A101"

#: ALPHA's price, a 10-for-1 in Q2 in a quarter it rose 5%.
PRICES = {Q1: D(100), Q2: D("10.5"), Q3: D("10.5")}

_numbers = count(1)


async def _filer(session: AsyncSession) -> int:
    number = next(_numbers)
    filer_id = await session.scalar(
        insert(Filer).values(name=f"fund {number}", slug=f"fund-{number}").returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{number:010d}", priority=1)
    )
    return filer_id


async def _file(
    session: AsyncSession,
    filer_id: int,
    period: date,
    shares: int | Decimal,
    *,
    price: Decimal | None = None,
) -> None:
    """A 13F of ``shares`` of ALPHA for ``period``, at that period's price, loaded."""
    number = next(_numbers)
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-{period:%y}-{number:06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR",
            period_of_report=period,
            filed_at=datetime(period.year, period.month, period.day, tzinfo=UTC)
            + timedelta(days=45, hours=number),
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    await session.execute(
        pg_insert(Security).values(cusip=ALPHA).on_conflict_do_nothing(index_elements=["cusip"])
    )
    await session.execute(
        insert(Holding).values(
            filing_id=filing_id,
            security_id=select(Security.id).where(Security.cusip == ALPHA).scalar_subquery(),
            filer_id=filer_id,
            period_of_report=period,
            cusip=ALPHA,
            value_usd=D(shares) * (PRICES[period] if price is None else price),
            shares=shares,
            sshprnamt_type="SH",
        )
    )


async def _held(session: AsyncSession, *books: dict[date, int]) -> list[int]:
    """One filer per book of ``{period: shares}``, each period filed. Their ids, in order."""
    filers = []
    for book in books:
        filer_id = await _filer(session)
        for period, shares in book.items():
            await _file(session, filer_id, period, shares)
        filers.append(filer_id)
    return filers


async def _splits(session: AsyncSession) -> dict[date, tuple[int, int, str]]:
    rows = await session.execute(
        select(
            SecuritySplit.period_of_report,
            SecuritySplit.numerator,
            SecuritySplit.denominator,
            SecuritySplit.confidence,
        ).execution_options(populate_existing=True)
    )
    return {period: (up, down, confidence) for period, up, down, confidence in rows.tuples()}


async def _change(session: AsyncSession, filer_id: int, period: date) -> PositionChange:
    return (
        await session.execute(
            select(PositionChange)
            .where(PositionChange.filer_id == filer_id, PositionChange.period_of_report == period)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _what(change: PositionChange) -> tuple[str, Decimal, Decimal | None, bool, Decimal | None]:
    return (
        change.action,
        change.shares_delta,
        change.shares_delta_pct,
        change.is_split,
        change.split_ratio,
    )


async def _everything(session: AsyncSession) -> list[tuple[object, ...]]:
    """Every change and split, as a rebuild of everything would leave them."""
    changes = await session.execute(
        select(
            PositionChange.filer_id,
            PositionChange.period_of_report,
            PositionChange.action,
            PositionChange.shares_delta,
            PositionChange.is_split,
            PositionChange.split_ratio,
        ).order_by(PositionChange.filer_id, PositionChange.period_of_report)
    )
    splits = await session.execute(
        select(
            SecuritySplit.period_of_report, SecuritySplit.numerator, SecuritySplit.denominator
        ).order_by(SecuritySplit.period_of_report)
    )
    return [*changes.tuples(), *splits.tuples()]


# --- a split, before and after ---------------------------------------------------


async def test_holders_through_a_split_are_adds_as_filed_and_holds_adjusted(
    db_session: AsyncSession,
) -> None:
    """Two hold 100 and 200 shares through it, a third holds 300 and buys 600
    after it. As filed, every one of them is a +900% add or more. Adjusted, two
    held and the third bought 600 shares, 20%, with the split's 2,700 gone."""
    held, also, bought = await _held(
        db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000}, {Q1: 300, Q2: 3_600}
    )

    await recompute(db_session, EVERYTHING)
    adjusted = [_what(await _change(db_session, filer, Q2)) for filer in (held, also, bought)]
    await db_session.execute(delete(SecuritySplit))
    await recompute_position_change(db_session, EVERYTHING)
    as_filed = [_what(await _change(db_session, filer, Q2)) for filer in (held, also, bought)]

    assert as_filed == [
        ("add", D(900), D(900), False, None),
        ("add", D(1_800), D(900), False, None),
        ("add", D(3_300), D(1_100), False, None),
    ]
    assert adjusted == [
        ("hold", D(0), D(0), True, D(10)),
        ("hold", D(0), D(0), True, D(10)),
        ("add", D(600), D(20), True, D(10)),
    ]


async def test_a_split_is_dated_the_quarter_after_it_with_its_exact_ratio(
    db_session: AsyncSession,
) -> None:
    await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000}, {Q1: 300, Q2: 3_600})

    rebuilt = await recompute(db_session, EVERYTHING)
    row = (await db_session.execute(select(SecuritySplit))).scalar_one()

    assert await _splits(db_session) == {Q2: (10, 1, "high")}
    assert (row.ratio, row.holders, row.holders_agreeing, row.holders_exact) == (D(10), 3, 3, 2)
    assert (row.shares_ratio, row.price_ratio) == (D(10), D("0.105"))
    assert rebuilt.changes.split == 3


async def test_a_reverse_split_turns_the_trims_into_holds(db_session: AsyncSession) -> None:
    """1-for-10 at $1 to $10.50. One holder sells half of what is left after it."""
    prices = {Q1: D(1), Q2: D("10.5")}
    held, sold = await _filer(db_session), await _filer(db_session)
    for filer_id, book in ((held, {Q1: 1_000, Q2: 100}), (sold, {Q1: 2_000, Q2: 100})):
        for period, shares in book.items():
            await _file(db_session, filer_id, period, shares, price=prices[period])
    third = await _filer(db_session)
    await _file(db_session, third, Q1, 3_000, price=prices[Q1])
    await _file(db_session, third, Q2, 300, price=prices[Q2])

    await recompute(db_session, EVERYTHING)

    # The seller moved by a twentieth, too far from a tenth to agree: two of
    # three, both exact.
    assert await _splits(db_session) == {Q2: (1, 10, "medium")}
    assert _what(await _change(db_session, held, Q2)) == ("hold", D(0), D(0), True, D("0.1"))
    assert _what(await _change(db_session, sold, Q2)) == ("trim", D(-100), D(-50), True, D("0.1"))


async def test_one_holder_is_not_a_split(db_session: AsyncSession) -> None:
    """However exactly its count moved: the rule asks for two."""
    [only] = await _held(db_session, {Q1: 100, Q2: 1_000})

    await recompute(db_session, EVERYTHING)

    assert await _splits(db_session) == {}
    assert _what(await _change(db_session, only, Q2)) == ("add", D(900), D(900), False, None)


async def test_trimming_as_the_price_rises_is_not_a_reverse_split(
    db_session: AsyncSession,
) -> None:
    """Three managers keeping ALPHA at a fixed weight, as it rose 50%: each sold a
    third, give or take, which is a 2-for-3's ratio with a price to match, and
    no holder within a share of it."""
    prices = {Q1: D(10), Q2: D(15)}
    filers = []
    for before, after in ((3_000, 2_010), (6_000, 3_960), (9_000, 6_030)):
        filer_id = await _filer(db_session)
        await _file(db_session, filer_id, Q1, before, price=prices[Q1])
        await _file(db_session, filer_id, Q2, after, price=prices[Q2])
        filers.append(filer_id)

    await recompute(db_session, EVERYTHING)

    assert await _splits(db_session) == {}
    assert {(await _change(db_session, filer, Q2)).action for filer in filers} == {"trim"}


# --- which changes are adjusted ---------------------------------------------------


async def test_a_change_across_a_skipped_quarter_is_adjusted_for_the_split_in_it(
    db_session: AsyncSession,
) -> None:
    """A filer with nothing loaded for Q2: its Q3 is against its Q1, and the
    split between them."""
    await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000})
    [skipped] = await _held(db_session, {Q1: 300, Q3: 3_000})

    await recompute(db_session, EVERYTHING)
    change = await _change(db_session, skipped, Q3)

    assert change.prev_period_of_report == Q1
    assert _what(change) == ("hold", D(0), D(0), True, D(10))


async def test_new_positions_and_exits_across_a_split_are_not_adjusted(
    db_session: AsyncSession,
) -> None:
    """A new position has nothing to adjust, and an exit is the position as filed, gone."""
    await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000})
    opened, exited = await _filer(db_session), await _filer(db_session)
    await _file(db_session, opened, Q2, 500)
    await _file(db_session, exited, Q1, 400)
    # Something else in Q2, so the period is published and the exit has a period.
    await db_session.execute(pg_insert(Security).values(cusip="22222B202").on_conflict_do_nothing())
    await db_session.execute(
        insert(Holding).values(
            filing_id=select(Filing.id)
            .where(Filing.filer_id == exited, Filing.period_of_report == Q1)
            .scalar_subquery(),
            security_id=select(Security.id).where(Security.cusip == "22222B202").scalar_subquery(),
            filer_id=exited,
            period_of_report=Q1,
            cusip="22222B202",
            value_usd=1_000,
            shares=10,
            sshprnamt_type="SH",
        )
    )
    await _file(db_session, exited, Q2, 0)

    await recompute(db_session, EVERYTHING)
    new = await _change(db_session, opened, Q2)
    exits = (
        await db_session.execute(
            select(PositionChange).where(
                PositionChange.filer_id == exited,
                PositionChange.period_of_report == Q2,
                PositionChange.action == "exit",
            )
        )
    ).scalars()

    assert _what(new) == ("new", D(500), None, False, None)
    assert [(e.shares_delta, e.is_split) for e in exits] == [(D(-10), False)]


# --- what a rebuild touches ------------------------------------------------------


async def test_another_filers_filing_reclassifies_this_filers_change(
    db_session: AsyncSession,
) -> None:
    """One holder alone moved by ten, an add. A second's Q2, loaded and rebuilt
    on its own pair, makes it a split, and the first holder's change, outside
    the scope, a hold. As one rebuild of everything leaves it."""
    first, second = await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200})
    await recompute(db_session, EVERYTHING)
    assert (await _change(db_session, first, Q2)).action == "add"

    await _file(db_session, second, Q2, 2_000)
    rebuilt = await recompute(db_session, Scope.of([(second, Q2)]))
    scoped = await _everything(db_session)
    await recompute(db_session, EVERYTHING)

    assert rebuilt.splits.added == {(rebuilt.splits.splits[0].candidate.security_id, Q2)}
    assert rebuilt.crossing == {(first, Q2)}
    assert (await _change(db_session, first, Q2)).action == "hold"
    assert scoped == await _everything(db_session)


async def test_a_filing_for_the_quarter_before_a_split_detects_it(
    db_session: AsyncSession,
) -> None:
    """The second holder's Q1 arrives after its Q2: until then it is a new
    position, and the first holder alone. The detection at Q2 compares Q2 with
    Q1, so a rebuild of Q1 runs it, and both Q2 changes are holds."""
    first, second = await _held(db_session, {Q1: 100, Q2: 1_000}, {Q2: 2_000})
    await recompute(db_session, EVERYTHING)
    assert await _splits(db_session) == {}

    await _file(db_session, second, Q1, 200)
    rebuilt = await recompute(db_session, Scope.of([(second, Q1)]))
    scoped = await _everything(db_session)
    await recompute(db_session, EVERYTHING)

    assert await _splits(db_session) == {Q2: (10, 1, "medium")}
    assert rebuilt.following == {(second, Q2)}
    assert rebuilt.crossing == {(first, Q2)}
    assert [(await _change(db_session, f, Q2)).action for f in (first, second)] == ["hold"] * 2
    assert scoped == await _everything(db_session)


async def test_a_rebuild_that_moves_no_split_rebuilds_no_other_filer(
    db_session: AsyncSession,
) -> None:
    filers = await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000})
    await recompute(db_session, EVERYTHING)

    rebuilt = await recompute(db_session, Scope.of([(filers[0], Q2)]))

    assert not rebuilt.splits.moved
    assert rebuilt.crossing == frozenset()


async def test_detect_splits_rebuilds_the_changes_across_what_moved_and_then_nothing(
    db_session: AsyncSession,
) -> None:
    """The first run after the table was created: no split stored yet, and the
    changes as filed. The second finds what it wrote."""
    filers = await _held(db_session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000})
    await recompute(db_session, EVERYTHING)
    await db_session.execute(delete(SecuritySplit))
    await recompute_position_change(db_session, EVERYTHING)

    first = await detect_splits(db_session)
    second = await detect_splits(db_session, period=Q2)

    assert len(first.splits.added) == 1
    assert first.crossing == {(filer, Q2) for filer in filers}
    assert (first.changes.hold, first.changes.split) == (2, 2)
    assert second.splits.moved == frozenset()
    assert second.crossing == frozenset()


# --- the checks ------------------------------------------------------------------


async def test_the_tables_reconcile_across_a_split(db_session: AsyncSession) -> None:
    await _held(
        db_session, {Q1: 100, Q2: 1_000, Q3: 1_000}, {Q1: 200, Q2: 2_000}, {Q1: 300, Q3: 3_300}
    )
    await recompute(db_session, EVERYTHING)
    await refresh_views(db_session)

    report = await reconcile(db_session, sample=None)

    assert await _splits(db_session) == {Q2: (10, 1, "medium")}
    assert report.clean, report.failed


async def test_check_data_leaves_out_a_jump_across_a_detected_split(
    db_session: AsyncSession,
) -> None:
    """A 200-for-1, which nothing detects, written in: +19,900% is a finding
    until there is a split for it."""
    [filer] = await _held(db_session, {Q1: 100})
    await _file(db_session, filer, Q2, 20_000, price=D("0.5"))
    await recompute(db_session, EVERYTHING)
    before = await position_jumps(db_session)
    await db_session.execute(
        insert(SecuritySplit).values(
            security_id=select(Security.id).where(Security.cusip == ALPHA).scalar_subquery(),
            period_of_report=Q2,
            numerator=200,
            denominator=1,
            confidence="low",
            holders=1,
            holders_agreeing=1,
            holders_exact=1,
            shares_ratio=200,
            price_ratio=D("0.005"),
        )
    )

    after = await position_jumps(db_session)

    assert [(jump.period, jump.cusip) for jump in before] == [(Q2, ALPHA)]
    assert after == []


# --- the command -----------------------------------------------------------------


@pytest.fixture
def committed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[AsyncEngine]:
    """For the command, which commits: tables truncated around the test."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.create_redis", lambda settings: FakeRedis())
    _truncate(migrated_engine)
    yield migrated_engine
    _truncate(migrated_engine)
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def _truncate(engine: AsyncEngine) -> None:
    """Every table the command wrote, and the views refreshed empty after them:
    a truncate does not reach a view, and a later module would read this one's
    rows from it."""

    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE ingestion_run, matview_refresh, position_change, "
                    "position_snapshot, holding, filing, security, filer_cik, filer "
                    "RESTART IDENTITY CASCADE"
                )
            )
            for view in MATERIALISED_VIEWS:
                await connection.execute(text(f"REFRESH MATERIALIZED VIEW {view.name}"))

    asyncio.run(run())


def _commit_as_filed(engine: AsyncEngine) -> None:
    """Three holders across the split, published, and the split then forgotten:
    the state the first ``detect-splits`` after the migration finds."""

    async def run() -> None:
        async with AsyncSession(engine) as session:
            await _held(session, {Q1: 100, Q2: 1_000}, {Q1: 200, Q2: 2_000}, {Q1: 300, Q2: 3_600})
            await recompute(session, EVERYTHING)
            await session.execute(delete(SecuritySplit))
            await recompute_position_change(session, EVERYTHING)
            await session.commit()

    asyncio.run(run())


def _count(engine: AsyncEngine, statement: str) -> int:
    async def run() -> int:
        async with engine.connect() as connection:
            return int((await connection.execute(text(statement))).scalar_one())

    return asyncio.run(run())


def test_detect_splits_dry_run_lists_the_split_and_writes_nothing(
    committed: AsyncEngine,
) -> None:
    _commit_as_filed(committed)

    result = CliRunner().invoke(app, ["detect-splits", "--all", "--dry-run"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == (
        "detect-splits  every quarter: 1 split (1 new, 0 gone, 0 re-rated) "
        "(dry run: nothing written)"
    )
    assert lines[1].split() == [
        "new",
        "2024Q2",
        "-",
        ALPHA,
        "10:1",
        "3",
        "of",
        "3",
        "holders,",
        "2",
        "exact",
        "shares",
        "x10.000,",
        "price",
        "x0.1050",
        "high",
    ]
    assert lines[2] == (
        "  changes  would rebuild 3 pairs across a split that moved: "
        "0 new, 1 add, 0 trim, 2 hold, 0 exit (3 across a split)"
    )
    assert _count(committed, "SELECT count(*) FROM security_split") == 0
    assert _count(committed, "SELECT count(*) FROM position_change WHERE is_split") == 0


def test_detect_splits_writes_once_and_a_second_run_finds_nothing_moved(
    committed: AsyncEngine,
) -> None:
    _commit_as_filed(committed)

    first = CliRunner().invoke(app, ["detect-splits", "--period", "2024Q2"])
    second = CliRunner().invoke(app, ["detect-splits", "--all", "--no-refresh-views"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first.stdout.startswith("detect-splits  2024Q2: 1 split (1 new, 0 gone, 0 re-rated)\n")
    assert "refresh-views  5 materialised views refreshed" in first.stdout
    assert second.stdout.splitlines()[-1] == (
        "  changes  rebuilt 0 pairs across a split that moved: 0 new, 0 add, 0 trim, 0 hold, 0 exit"
    )
    assert _count(committed, "SELECT count(*) FROM security_split") == 1
    assert _count(committed, "SELECT count(*) FROM position_change WHERE is_split") == 3


def test_detect_splits_needs_a_period_or_all(committed: AsyncEngine) -> None:
    result = CliRunner().invoke(app, ["detect-splits"])

    assert result.exit_code == 2
    assert "say where to detect: --period or --all" in result.stderr
