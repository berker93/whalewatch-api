"""Scoped ``recompute``: rebuilding some ``(filer, period)`` pairs, and the period after each.

``test_position_snapshot`` and ``test_position_change`` prove what the two tables
hold. These prove that rebuilding part of them leaves the tables exactly as
rebuilding all of them would. A scope has to find every row it must replace,
including rows nothing is filed under any more. The period after a rebuilt one
has to have its changes rebuilt too, and it is the filer's next published
period, not the next quarter. Nothing outside the two may be touched. And a
rebuild run twice must be the rebuild run once, row for row.

Most run over made-up filings at $10 a share, inserted as loaded. Berkshire's
real filings, published one at a time in several orders, check the whole of it
against one rebuild of everything.
"""

import asyncio
import logging
import random
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from sqlalchemy import Text, func, insert, inspect, literal_column, select, text, update
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
    PositionSnapshot,
    Security,
)
from app.db.models.base import Base
from app.db.models.enums import AmendmentKind
from app.derived.recompute import RECOMPUTE_LOCK, recompute
from app.derived.scope import EVERYTHING, Scope, filing_pairs, resolve_scope
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import by_slug, load_fixtures

FUND_CIK = "0000000001"
OTHER_CIK = "0000000002"
Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
Q1_2025 = date(2025, 3, 31)
ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"

BERKSHIRE = "0001067983"

RESTATEMENT = AmendmentKind.RESTATEMENT
NEW_HOLDINGS = AmendmentKind.NEW_HOLDINGS

_accessions = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal


def held(cusip: str, shares: int) -> _Held:
    """``shares`` of ``cusip``, at $10 a share."""
    return _Held(cusip, Decimal(shares))


async def _fund(session: AsyncSession, slug: str = "a-fund", cik: str = FUND_CIK) -> int:
    """A made-up filer with one CIK."""
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id


async def _file(
    session: AsyncSession,
    filer_id: int,
    period: date,
    *positions: _Held,
    cik: str = FUND_CIK,
    amends: AmendmentKind | None = None,
    suspect: bool = False,
) -> None:
    """A made-up 13F for ``period`` holding ``positions``, inserted as loaded.

    An original unless ``amends`` says which kind of amendment it is. Each
    filing is filed an hour after the one before it, so a restatement replaces
    whatever came before it and an addition adds to it.
    """
    number = next(_accessions)
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-{period:%y}-{number:06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR" if amends is None else "13F-HR/A",
            amendment_kind=amends,
            period_of_report=period,
            filed_at=datetime(period.year, period.month, period.day, tzinfo=UTC)
            + timedelta(days=45, hours=number),
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
                value_usd=position.shares * 10,
                shares=position.shares,
                sshprnamt_type="SH",
            )
        )


def _pair(filer_id: int, period: date) -> Scope:
    return Scope.of([(filer_id, period)])


async def _snapshot(session: AsyncSession, period: date) -> dict[str, Decimal]:
    """One period's published shares, by CUSIP."""
    rows = await session.execute(
        select(Security.cusip, PositionSnapshot.shares)
        .join(Security, Security.id == PositionSnapshot.security_id)
        .where(PositionSnapshot.period_of_report == period)
    )
    return dict(rows.tuples().all())


async def _changes(session: AsyncSession, period: date) -> dict[str, PositionChange]:
    """One period's changes, by CUSIP, read fresh: a rebuild replaced the rows
    under any copy the session already holds."""
    rows = await session.execute(
        select(Security.cusip, PositionChange)
        .join(Security, Security.id == PositionChange.security_id)
        .where(PositionChange.period_of_report == period)
        .execution_options(populate_existing=True)
    )
    return dict(rows.tuples().all())


def _change(change: PositionChange) -> tuple[object, ...]:
    """``(action, prev_period_of_report, prev_shares, shares_delta)``."""
    return (change.action, change.prev_period_of_report, change.prev_shares, change.shares_delta)


_TABLES: tuple[type[Base], ...] = (PositionSnapshot, PositionChange)


async def _published(session: AsyncSession) -> dict[str, list[tuple[Any, ...]]]:
    """Both tables, every column but ``computed_at``, in key order: all a reader sees."""
    published = {}
    for model in _TABLES:
        mapper = inspect(model)
        rows = await session.execute(
            select(*(column for column in mapper.columns if column.name != "computed_at")).order_by(
                *mapper.primary_key
            )
        )
        published[model.__tablename__] = [tuple(row) for row in rows]
    return published


async def _stored(session: AsyncSession) -> dict[tuple[str, int, date, int], str]:
    """Where every row of both tables is stored, by table and key. A row deleted
    and inserted again is stored somewhere new, even unchanged and in the same
    transaction."""
    stored = {}
    for model in _TABLES:
        key = inspect(model).primary_key
        rows = await session.execute(select(literal_column("ctid::text", Text), *key))
        for ctid, filer_id, period, security_id in rows.tuples():
            stored[(model.__tablename__, filer_id, period, security_id)] = ctid
    return stored


def _rewritten(
    before: dict[tuple[str, int, date, int], str], after: dict[tuple[str, int, date, int], str]
) -> set[tuple[str, date]]:
    """Each ``(table, period)`` with a row deleted, inserted or moved between the two."""
    return {(table, period) for (table, _, period, _), _ in (before.items() ^ after.items())}


async def _matches_rebuilding_everything(session: AsyncSession) -> None:
    """The tables as they are equal the tables as one rebuild of everything leaves them."""
    scoped = await _published(session)
    await recompute(session, EVERYTHING)
    assert await _published(session) == scoped


# --- what a scope is --------------------------------------------------------------


async def test_a_filer_a_period_or_both_resolve_to_the_pairs_filed(
    db_session: AsyncSession,
) -> None:
    a = await _fund(db_session)
    b = await _fund(db_session, "b-fund", OTHER_CIK)
    await _file(db_session, a, Q1, held(ALPHA, 100))
    await _file(db_session, a, Q2, held(ALPHA, 100))
    await _file(db_session, b, Q2, held(ALPHA, 100), cik=OTHER_CIK)

    assert await resolve_scope(db_session, period=Q2) == Scope.of({(a, Q2), (b, Q2)})
    assert await resolve_scope(db_session, filer_id=a) == Scope.of({(a, Q1), (a, Q2)})
    assert await resolve_scope(db_session, filer_id=a, period=Q2) == _pair(a, Q2)
    assert await resolve_scope(db_session, filer_id=b, period=Q1) == Scope.of(())
    assert await resolve_scope(db_session) == EVERYTHING


async def test_a_pair_nothing_is_filed_under_any_more_is_rebuilt_empty(
    db_session: AsyncSession,
) -> None:
    """A's Q1 filing turns out to be B's. A's Q1 is still published from it,
    and filed under nothing, so a scope read from ``filing`` alone would leave
    it. The scope finds it in the snapshot, and the rebuild empties it."""
    a = await _fund(db_session)
    b = await _fund(db_session, "b-fund", OTHER_CIK)
    await _file(db_session, a, Q1, held(ALPHA, 100))
    await recompute(db_session, EVERYTHING)

    await db_session.execute(update(Filing).values(filer_id=b))
    scope = await resolve_scope(db_session, period=Q1)
    await recompute(db_session, scope)

    assert scope == Scope.of({(a, Q1), (b, Q1)})
    published = await db_session.execute(
        select(PositionSnapshot.filer_id, func.count()).group_by(PositionSnapshot.filer_id)
    )
    assert published.tuples().all() == [(b, 1)]
    await _matches_rebuilding_everything(db_session)


# --- the period after ---------------------------------------------------------------


async def test_amending_q3_rebuilds_q3_and_q4_s_changes_and_nothing_else(
    db_session: AsyncSession,
) -> None:
    """What the walk forward is for. Q4's 150 shares were an add on Q3's 100.
    Q3 is restated to 150, and Q4 is a hold, rebuilt against the new Q3. Q2
    and 2025Q1 are where they were stored, and so is Q4's snapshot, which
    does not depend on Q3."""
    fund = await _fund(db_session)
    await _file(db_session, fund, Q2, held(ALPHA, 100))
    await _file(db_session, fund, Q3, held(ALPHA, 100))
    await _file(db_session, fund, Q4, held(ALPHA, 150))
    await _file(db_session, fund, Q1_2025, held(ALPHA, 150))
    await recompute(db_session, EVERYTHING)
    assert _change((await _changes(db_session, Q4))[ALPHA]) == ("add", Q3, 100, 50)
    before = await _stored(db_session)

    await _file(db_session, fund, Q3, held(ALPHA, 150), amends=RESTATEMENT)
    rebuilt = await recompute(db_session, _pair(fund, Q3))

    assert rebuilt.following == {(fund, Q4)}
    assert await _snapshot(db_session, Q3) == {ALPHA: 150}
    assert _change((await _changes(db_session, Q3))[ALPHA]) == ("add", Q2, 100, 50)
    assert _change((await _changes(db_session, Q4))[ALPHA]) == ("hold", Q3, 150, 0)
    assert _rewritten(before, await _stored(db_session)) == {
        ("position_snapshot", Q3),
        ("position_change", Q3),
        ("position_change", Q4),
    }
    await _matches_rebuilding_everything(db_session)


async def test_the_period_after_is_the_filer_s_next_published_one(
    db_session: AsyncSession,
) -> None:
    """Nothing is loaded for Q3, and Q4's changes are against Q2. So it is Q4's
    that a rebuilt Q2 rebuilds. Q3, the next quarter, has none to rebuild."""
    fund = await _fund(db_session)
    await _file(db_session, fund, Q2, held(ALPHA, 100))
    await _file(db_session, fund, Q4, held(ALPHA, 150))
    await recompute(db_session, EVERYTHING)

    await _file(db_session, fund, Q2, held(ALPHA, 150), amends=RESTATEMENT)
    rebuilt = await recompute(db_session, _pair(fund, Q2))

    assert rebuilt.following == {(fund, Q4)}
    assert _change((await _changes(db_session, Q4))[ALPHA]) == ("hold", Q2, 150, 0)
    await _matches_rebuilding_everything(db_session)


async def test_a_position_a_restatement_drops_is_deleted_and_exits(
    db_session: AsyncSession,
) -> None:
    """Why the rebuild deletes and inserts rather than upserting. Bravo is in
    Q3 as first filed and not as restated. Upserting the restated rows would
    leave Bravo's Q3 row where it was. Rebuilt, Bravo exits in Q3 and is new
    again in Q4."""
    fund = await _fund(db_session)
    for period in (Q2, Q3, Q4):
        await _file(db_session, fund, period, held(ALPHA, 100), held(BRAVO, 100))
    await recompute(db_session, EVERYTHING)

    await _file(db_session, fund, Q3, held(ALPHA, 100), amends=RESTATEMENT)
    await recompute(db_session, _pair(fund, Q3))

    assert await _snapshot(db_session, Q3) == {ALPHA: 100}
    assert _change((await _changes(db_session, Q3))[BRAVO]) == ("exit", Q2, 100, -100)
    assert _change((await _changes(db_session, Q4))[BRAVO]) == ("new", Q3, None, 100)
    await _matches_rebuilding_everything(db_session)


async def test_a_period_withheld_by_a_suspect_amendment_is_stepped_over_by_the_next(
    db_session: AsyncSession,
) -> None:
    """Q2 is published, then a suspect addition withholds it. Its rows go, and
    Q3, whose changes were against Q2, is rebuilt against Q1."""
    fund = await _fund(db_session)
    await _file(db_session, fund, Q1, held(ALPHA, 100))
    await _file(db_session, fund, Q2, held(ALPHA, 120))
    await _file(db_session, fund, Q3, held(ALPHA, 120))
    await recompute(db_session, EVERYTHING)

    await _file(db_session, fund, Q2, held(BRAVO, 10), amends=NEW_HOLDINGS, suspect=True)
    rebuilt = await recompute(db_session, _pair(fund, Q2))

    assert rebuilt.snapshot.withheld == 1
    assert rebuilt.following == {(fund, Q3)}
    assert await _snapshot(db_session, Q2) == {}
    assert await _changes(db_session, Q2) == {}
    assert _change((await _changes(db_session, Q3))[ALPHA]) == ("add", Q1, 100, 20)
    await _matches_rebuilding_everything(db_session)


async def test_a_quarter_loaded_late_comes_between_the_two_either_side(
    db_session: AsyncSession,
) -> None:
    """Q3's changes were against Q1, Bravo's exit among them. Q2 arrives late,
    with Bravo gone and Charlie bought. Bravo's exit moves to Q2, and Q3, now
    against Q2, exits Charlie."""
    fund = await _fund(db_session)
    await _file(db_session, fund, Q1, held(ALPHA, 100), held(BRAVO, 100))
    await _file(db_session, fund, Q3, held(ALPHA, 100))
    await recompute(db_session, EVERYTHING)
    assert _change((await _changes(db_session, Q3))[BRAVO]) == ("exit", Q1, 100, -100)

    await _file(db_session, fund, Q2, held(ALPHA, 100), held(CHARLIE, 50))
    rebuilt = await recompute(db_session, _pair(fund, Q2))

    assert rebuilt.following == {(fund, Q3)}
    q2 = await _changes(db_session, Q2)
    assert {cusip: change.action for cusip, change in q2.items()} == {
        ALPHA: "hold",
        BRAVO: "exit",
        CHARLIE: "new",
    }
    q3 = await _changes(db_session, Q3)
    assert {cusip: _change(change) for cusip, change in q3.items()} == {
        ALPHA: ("hold", Q2, 100, 0),
        CHARLIE: ("exit", Q2, 50, -50),
    }
    await _matches_rebuilding_everything(db_session)


async def test_a_filer_s_latest_period_has_no_period_after_to_rebuild(
    db_session: AsyncSession,
) -> None:
    fund = await _fund(db_session)
    await _file(db_session, fund, Q1, held(ALPHA, 100))
    await _file(db_session, fund, Q2, held(ALPHA, 100))

    rebuilt = await recompute(db_session, _pair(fund, Q2))

    assert rebuilt.following == frozenset()


# --- the same tables, however they are reached --------------------------------------


async def _two_funds_amended_and_withheld(session: AsyncSession) -> tuple[int, int]:
    """Two filers, published, then a restatement, an addition and a suspect
    filing landing on top: something for a rebuild to change."""
    a = await _fund(session)
    b = await _fund(session, "b-fund", OTHER_CIK)
    await _file(session, a, Q1, held(ALPHA, 100), held(BRAVO, 50))
    await _file(session, a, Q2, held(ALPHA, 120))
    await _file(session, a, Q3, held(ALPHA, 120), held(CHARLIE, 10))
    await _file(session, a, Q4, held(CHARLIE, 10))
    await _file(session, b, Q2, held(BRAVO, 70), cik=OTHER_CIK)
    await _file(session, b, Q3, held(BRAVO, 70), cik=OTHER_CIK)
    await _file(session, b, Q4, held(BRAVO, 90), cik=OTHER_CIK)
    await recompute(session, EVERYTHING)

    await _file(session, a, Q3, held(ALPHA, 80), amends=RESTATEMENT)
    await _file(session, a, Q2, held(BRAVO, 5), amends=NEW_HOLDINGS)
    await _file(session, b, Q3, held(ALPHA, 1), cik=OTHER_CIK, amends=NEW_HOLDINGS, suspect=True)
    return a, b


@pytest.mark.parametrize("which", ["--all", "--filer", "--period", "--filer --period"])
async def test_recomputing_twice_leaves_both_tables_exactly_as_once(
    db_session: AsyncSession, which: str
) -> None:
    """The second run of a rebuild changes nothing a reader can see, and says so."""
    a, _ = await _two_funds_amended_and_withheld(db_session)
    scope = {
        "--all": EVERYTHING,
        "--filer": await resolve_scope(db_session, filer_id=a),
        "--period": await resolve_scope(db_session, period=Q3),
        "--filer --period": _pair(a, Q3),
    }[which]

    first = await recompute(db_session, scope)
    once = await _published(db_session)
    second = await recompute(db_session, scope)

    assert second == first
    assert await _published(db_session) == once


async def _berkshire(session: AsyncSession) -> None:
    filer_id = await session.scalar(
        insert(Filer)
        .values(name="berkshire-hathaway", slug="berkshire-hathaway")
        .returning(Filer.id)
    )
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=BERKSHIRE, priority=1))


async def _ingest(session: AsyncSession, slug: str) -> None:
    """What ``ingest-filing`` does with one filing: load it, then rebuild the
    pair it is filed under, and the period after."""
    fixture = by_slug(slug)
    cover, table = fixture.parse()
    await load_filing(
        session,
        accession_no=fixture.accession_no,
        filed_at=fixture.filed_at,
        primary_doc=cover,
        normalised=normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table),
    )
    await recompute(session, Scope.of(await filing_pairs(session, fixture.accession_no)))


_BERKSHIRE_FILINGS = sorted(
    (fixture for fixture in load_fixtures() if fixture.slug.startswith("berkshire-")),
    key=lambda fixture: fixture.filed_at,
)


def _orders() -> dict[str, list[str]]:
    filed = [fixture.slug for fixture in _BERKSHIRE_FILINGS]
    shuffled = list(filed)
    random.Random(13).shuffle(shuffled)
    return {"as-filed": filed, "newest-first": filed[::-1], "shuffled": shuffled}


@pytest.mark.parametrize("order", list(_orders().values()), ids=list(_orders()))
async def test_publishing_filing_by_filing_in_any_order_is_rebuilding_everything(
    db_session: AsyncSession, order: list[str]
) -> None:
    """Berkshire's seven real filings across four periods, with a restatement
    and two additions, loaded as ingest loads them: one at a time, each
    followed by a rebuild of its own pair. Newest first, every period lands
    before the one its changes start from, and only the walk forward puts
    those changes right."""
    await _berkshire(db_session)

    for slug in order:
        await _ingest(db_session, slug)

    await _matches_rebuilding_everything(db_session)


# --- one at a time ------------------------------------------------------------------


async def test_a_rebuild_keeps_the_next_one_waiting_until_it_commits(
    migrated_engine: AsyncEngine,
) -> None:
    """Two connections, as two backfill workers. The first has rebuilt and not
    committed, and the second cannot take the lock. Once the first is done,
    it can."""
    take_lock = select(func.pg_try_advisory_xact_lock(RECOMPUTE_LOCK))
    async with (
        migrated_engine.connect() as first,
        migrated_engine.connect() as second,
        AsyncSession(bind=first) as rebuilding,
    ):
        await recompute(rebuilding, Scope.of(()))

        await second.begin()
        assert await second.scalar(take_lock) is False

        await rebuilding.rollback()
        assert await second.scalar(take_lock) is True
        await second.rollback()


# --- the command --------------------------------------------------------------------


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
                    "TRUNCATE ingestion_run, matview_refresh, position_change, "
                    "position_snapshot, holding, filing, security, filer_cik, filer "
                    "RESTART IDENTITY CASCADE"
                )
            )

    asyncio.run(run())


def _commit_two_funds_with_q3_restated(engine: AsyncEngine) -> None:
    """Two filers' Q2 to Q4, published, and then a restatement of A's Q3."""

    async def run() -> None:
        async with AsyncSession(engine) as session:
            a = await _fund(session)
            b = await _fund(session, "b-fund", OTHER_CIK)
            for fund, cik in ((a, FUND_CIK), (b, OTHER_CIK)):
                for period in (Q2, Q3, Q4):
                    await _file(session, fund, period, held(ALPHA, 100), cik=cik)
            await recompute(session, EVERYTHING)
            await _file(session, a, Q3, held(ALPHA, 150), amends=RESTATEMENT)
            await session.commit()

    asyncio.run(run())


def test_recompute_period_rebuilds_the_quarter_and_the_changes_after_it(
    committed: AsyncEngine,
) -> None:
    """Lower case, and it is still September 30th: both filers' Q3 is found."""
    _commit_two_funds_with_q3_restated(committed)

    result = CliRunner().invoke(app, ["recompute", "--period", "2024q3"])

    assert result.exit_code == 0, result.output
    rebuild, refresh = _rebuild_then_refresh(result.stdout)
    assert rebuild == [
        "recompute  position_snapshot for 2024Q3: 2 positions in 2 periods of 2 filers",
        # Q3: A's add and B's hold. Q4: A's trim back to 100, and B's hold.
        "  changes     position_change: 0 new, 1 add, 1 trim, 2 hold, 0 exit",
        "  next        also the changes of 2 next periods, which start from a rebuilt one: 2024Q4",
    ]
    assert refresh.startswith("refresh-views  5 materialised views refreshed in ")


def test_recompute_filer_and_period_rebuild_that_one_pair(committed: AsyncEngine) -> None:
    _commit_two_funds_with_q3_restated(committed)

    result = CliRunner().invoke(app, ["recompute", "--filer", "a-fund", "--period", "2024Q3"])

    assert result.exit_code == 0, result.output
    rebuild, _ = _rebuild_then_refresh(result.stdout)
    assert rebuild == [
        "recompute  position_snapshot for a-fund 2024Q3: 1 position in 1 period of 1 filer",
        "  changes     position_change: 0 new, 1 add, 1 trim, 0 hold, 0 exit",
        "  next        also the changes of 1 next period, which start from a rebuilt one: 2024Q4",
    ]


def _rebuild_then_refresh(stdout: str) -> tuple[list[str], str]:
    """The rebuild's lines, and the first line of the refresh that follows them.
    The refresh's own lines are test_materialised_views' to check."""
    lines = stdout.splitlines()
    [at] = [n for n, line in enumerate(lines) if line.startswith("refresh-views  ")]
    return lines[:at], lines[at]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ([], "say what to rebuild: --filer, --period, or --all"),
        (["--all", "--filer", "a-fund"], "--all rebuilds everything"),
        (["--all", "--period", "2024Q3"], "--all rebuilds everything"),
    ],
)
def test_recompute_takes_one_scope_and_says_so(args: list[str], error: str) -> None:
    """Rebuilding everything has to be asked for. A bare ``recompute`` is
    exactly the five years of everything that one new filing must not cost."""
    result = CliRunner().invoke(app, ["recompute", *args])

    assert result.exit_code == 2
    assert error in result.stderr


@pytest.mark.parametrize("period", ["2024Q5", "2024-09-30", "Q3 2024"])
def test_recompute_refuses_a_period_that_is_not_a_quarter(period: str) -> None:
    result = CliRunner().invoke(app, ["recompute", "--period", period])

    assert result.exit_code == 2
    assert "is not a quarter" in result.stderr
