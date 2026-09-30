"""``check-data``: the cross-filing checks, over real quarters and made-up ones.

Real filings where the point is that the check stays quiet — or speaks up — on
what EDGAR actually holds: two consecutive Berkshire quarters have nothing in
them to look at, and the same filer with two quarters missing has a gap.
Made-up portfolios where the point is a boundary. A split has to be a precise
multiple of a share count, and no real filing was chosen for that.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, Security
from app.db.queries.checks import (
    FilingGap,
    check_data,
    concentrated_periods,
    filing_gaps,
    position_jumps,
    suspect_periods,
)
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import by_slug

BERKSHIRE = "0001067983"
CIK = "0000000001"
Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)
NEXT_Q1 = date(2025, 3, 31)

ALPHA = "11111A101"
BRAVO = "22222B202"
CHARLIE = "33333C303"
NAMES = {ALPHA: "ALPHA CORP", BRAVO: "BRAVO CORP", CHARLIE: "CHARLIE CORP"}

_accessions = count(1)


@dataclass(frozen=True, slots=True)
class _Held:
    cusip: str
    shares: Decimal
    value: Decimal
    put_call: str | None = None


def held(cusip: str, shares: int, *, at: str, put_call: str | None = None) -> _Held:
    """``shares`` of ``cusip`` at a price of ``at`` a share."""
    return _Held(cusip, Decimal(shares), Decimal(shares) * Decimal(at), put_call)


async def _filer(session: AsyncSession, slug: str = "a-fund", cik: str = CIK) -> int:
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id


async def _filing(
    session: AsyncSession,
    filer_id: int,
    period: date,
    *positions: _Held,
    status: str = "ok",
    form: str = "13F-HR",
    kind: AmendmentKind | None = None,
    days_later: int = 0,
) -> str:
    """One filing for ``period`` holding ``positions``; its accession number."""
    accession = f"{CIK}-{period.year % 100:02d}-{next(_accessions):06d}"
    filed = period + timedelta(days=45 + days_later)
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=accession,
            cik=CIK,
            filer_id=filer_id,
            form_type=form,
            period_of_report=period,
            filed_at=datetime.combine(filed, time(16), tzinfo=UTC),
            value_multiplier=1,
            amendment_kind=kind,
            parse_status=status,
            parse_notes=[
                {
                    "kind": "entry_count",
                    "severity": "error",
                    "detail": "parsed 1 rows, cover page declares 2",
                    "observed": "1",
                    "expected": "2",
                },
                {
                    "kind": "dropped_row",
                    "severity": "warning",
                    "detail": "value: expected a number (got 'N/A'); row not loaded",
                    "row": 2,
                },
            ]
            if status == "suspect"
            else None,
        )
        .returning(Filing.id)
    )
    for position in positions:
        await session.execute(
            pg_insert(Security)
            .values(cusip=position.cusip, name=NAMES.get(position.cusip))
            .on_conflict_do_nothing(index_elements=[Security.cusip])
        )
        security_id = await session.scalar(
            select(Security.id).where(Security.cusip == position.cusip)
        )
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=security_id,
                filer_id=filer_id,
                period_of_report=period,
                cusip=position.cusip,
                value_usd=position.value,
                shares=position.shares,
                sshprnamt_type="SH",
                put_call=position.put_call,
            )
        )
    return accession


async def _load(session: AsyncSession, slug: str) -> None:
    """One committed fixture through the parser, the guards and the loader."""
    fixture = by_slug(slug)
    cover, table = fixture.parse()
    await load_filing(
        session,
        accession_no=fixture.accession_no,
        filed_at=fixture.filed_at,
        primary_doc=cover,
        normalised=normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table),
    )


# --- over real filings ---------------------------------------------------------


async def test_two_consecutive_real_quarters_have_nothing_to_look_at(
    db_session: AsyncSession,
) -> None:
    """Berkshire's 2022Q3 and Q4, either side of the units cutover. The largest
    position is two fifths of the book, the largest share count grew a fifth,
    and there is nothing between them to be missing. A check that fires here
    fires on everything."""
    await _filer(db_session, "berkshire-hathaway", BERKSHIRE)
    await _load(db_session, "berkshire-2022q3-thousands")
    await _load(db_session, "berkshire-2022q4-dollars")

    report = await check_data(db_session)

    assert report.clean, report


async def test_quarters_missing_between_two_real_ones_are_a_gap(db_session: AsyncSession) -> None:
    await _filer(db_session, "berkshire-hathaway", BERKSHIRE)
    await _load(db_session, "berkshire-2022q4-dollars")
    await _load(db_session, "berkshire-2023q3-original")

    assert await filing_gaps(db_session) == [
        FilingGap(
            slug="berkshire-hathaway",
            missing=("2023Q1", "2023Q2"),
            after="2022Q4",
            before="2023Q3",
            unloaded=0,
        )
    ]


async def test_an_addition_counting_alone_is_a_concentrated_period(
    db_session: AsyncSession,
) -> None:
    """Berkshire's 2023Q3 amendment releasing Chubb, loaded without the book it
    adds to: the period is one position, all of it. The check cannot tell why —
    here, a filing that has not been loaded — only that someone should look."""
    await _filer(db_session, "berkshire-hathaway", BERKSHIRE)
    await _load(db_session, "berkshire-2023q3-new-holdings")

    (found,) = await concentrated_periods(db_session)

    assert (found.slug, found.period, found.cusip) == (
        "berkshire-hathaway",
        date(2023, 9, 30),
        "H1467J104",
    )
    assert (found.weight, found.positions) == (Decimal(1), 1)
    assert found.period_value == Decimal(1_695_320_075)


# --- suspect periods -----------------------------------------------------------


async def test_a_period_a_suspect_filing_counts_toward_is_listed_with_what_failed(
    db_session: AsyncSession,
) -> None:
    """The warning beside the error is evidence, not a failed guard."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 100, at="10"))
    accession = await _filing(db_session, fund, Q2, held(ALPHA, 100, at="10"), status="suspect")

    (period,) = await suspect_periods(db_session)

    assert (period.slug, period.period) == ("a-fund", Q2)
    (filing,) = period.filings
    assert (filing.accession_no, filing.failed) == (accession, ("entry_count",))


async def test_a_suspect_filing_a_restatement_replaced_is_not_listed(
    db_session: AsyncSession,
) -> None:
    """Nothing reads it, and nothing anyone does will make it not suspect: the
    filer already fixed it. Listing it would keep the command failing forever
    over a period that is published correctly."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 100, at="10"), status="suspect")
    await _filing(
        db_session,
        fund,
        Q1,
        held(ALPHA, 100, at="10"),
        form="13F-HR/A",
        kind=AmendmentKind.RESTATEMENT,
        days_later=30,
    )

    assert await suspect_periods(db_session) == []


async def test_a_suspect_periods_positions_are_checked_only_when_asked(
    db_session: AsyncSession,
) -> None:
    """By default the snapshot checks read what ``recompute`` would publish, and
    a suspect period is not published. ``--include-suspect`` checks it as
    ``recompute --include-suspect`` would publish it."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 1_000, at="10"), status="suspect")

    assert await concentrated_periods(db_session) == []
    (found,) = await concentrated_periods(db_session, include_suspect=True)
    assert found.cusip == ALPHA


# --- concentration -------------------------------------------------------------


@pytest.mark.parametrize(
    ("top", "flagged"),
    [(900, False), (901, True)],
    ids=["exactly-90-percent", "just-over"],
)
async def test_the_concentration_threshold_is_more_than_ninety_percent(
    db_session: AsyncSession, top: int, flagged: bool
) -> None:
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, top, at="1"), held(BRAVO, 1_000 - top, at="1"))

    assert bool(await concentrated_periods(db_session)) is flagged


async def test_an_option_line_is_never_the_concentrated_position(
    db_session: AsyncSession,
) -> None:
    """A call on 100x the book's notional is a hedge, or a bet, but not a share
    of the portfolio: its value is the underlying's."""
    fund = await _filer(db_session)
    await _filing(
        db_session,
        fund,
        Q1,
        held(ALPHA, 500, at="1"),
        held(BRAVO, 500, at="1"),
        held(ALPHA, 100_000, at="1", put_call="Call"),
    )

    assert await concentrated_periods(db_session) == []


# --- position jumps ------------------------------------------------------------


async def test_a_hundred_and_fifty_to_one_split_is_a_jump_and_the_price_says_so(
    db_session: AsyncSession,
) -> None:
    """Shares x150, value unchanged. The share count alone is indistinguishable
    from buying 150 times over; the price on either side is what isn't."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 1_000_000, at="300"))
    await _filing(db_session, fund, Q2, held(ALPHA, 150_000_000, at="2"))

    (jump,) = await position_jumps(db_session)

    assert (jump.slug, jump.previous_period, jump.period, jump.cusip) == ("a-fund", Q1, Q2, ALPHA)
    assert jump.change == 149
    assert (jump.price_before, jump.price_after) == (Decimal(300), Decimal(2))
    assert jump.value_before == jump.value_after


@pytest.mark.parametrize(
    ("after", "flagged"),
    [(101_000_000, False), (101_000_001, True)],
    ids=["exactly-10000-percent", "just-over"],
)
async def test_the_jump_threshold_is_more_than_ten_thousand_percent(
    db_session: AsyncSession, after: int, flagged: bool
) -> None:
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 1_000_000, at="10"))
    await _filing(db_session, fund, Q2, held(ALPHA, after, at="10"))

    assert bool(await position_jumps(db_session)) is flagged


async def test_a_new_position_is_not_a_jump(db_session: AsyncSession) -> None:
    """From nothing is not a percentage."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(BRAVO, 1_000, at="10"))
    await _filing(db_session, fund, Q2, held(BRAVO, 1_000, at="10"), held(ALPHA, 10**9, at="1"))

    assert await position_jumps(db_session) == []


async def test_a_jump_across_a_gap_is_not_quarter_on_quarter(db_session: AsyncSession) -> None:
    """Q1 to Q3 is two quarters of trading. The gap check reports the gap."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 1, at="10"))
    await _filing(db_session, fund, Q3, held(ALPHA, 1_000_000, at="10"))

    assert await position_jumps(db_session) == []
    (gap,) = await filing_gaps(db_session)
    assert gap.missing == ("2024Q2",)


async def test_a_position_is_compared_like_with_like(db_session: AsyncSession) -> None:
    """Calls on a stock are not more of the stock."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 1_000, at="10"))
    await _filing(
        db_session,
        fund,
        Q2,
        held(ALPHA, 1_000, at="10"),
        held(ALPHA, 10_000_000, at="10", put_call="Call"),
    )

    assert await position_jumps(db_session) == []


# --- gaps ----------------------------------------------------------------------


async def test_a_quarter_whose_filing_did_not_load_is_a_gap_that_says_so(
    db_session: AsyncSession,
) -> None:
    """Found and not loaded sends someone to ``backfill``, not to EDGAR."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 100, at="10"))
    await _filing(db_session, fund, Q2, status="failed")
    await _filing(db_session, fund, Q3, held(ALPHA, 100, at="10"))

    (gap,) = await filing_gaps(db_session)

    assert (gap.missing, gap.after, gap.before, gap.unloaded) == (
        ("2024Q2",),
        "2024Q1",
        "2024Q3",
        1,
    )


async def test_a_notice_fills_its_quarter(db_session: AsyncSession) -> None:
    """A 13F-NT reports no holdings, and it is still that quarter's filing."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q1, held(ALPHA, 100, at="10"))
    await _filing(db_session, fund, Q2, form="13F-NT")
    await _filing(db_session, fund, Q3, held(ALPHA, 100, at="10"))

    assert await filing_gaps(db_session) == []


async def test_only_quarters_between_the_first_and_last_can_be_missing(
    db_session: AsyncSession,
) -> None:
    """Before the first is where the history starts. After the last is a
    manager who stopped, or a quarter not due yet."""
    fund = await _filer(db_session)
    await _filing(db_session, fund, Q2, held(ALPHA, 100, at="10"))
    await _filing(db_session, fund, Q3, held(ALPHA, 100, at="10"))

    assert await filing_gaps(db_session) == []


# --- scope ---------------------------------------------------------------------


async def test_one_filer_is_checked_alone(db_session: AsyncSession) -> None:
    fund = await _filer(db_session)
    other = await _filer(db_session, "another-fund", "0000000002")
    await _filing(db_session, fund, Q1, held(ALPHA, 100, at="10"), status="suspect")
    await _filing(db_session, other, Q1, held(ALPHA, 100, at="10"), status="suspect")

    everyone = await check_data(db_session)
    one = await check_data(db_session, filer_id=other)

    assert [period.slug for period in everyone.suspect_periods] == ["a-fund", "another-fund"]
    assert [period.slug for period in one.suspect_periods] == ["another-fund"]


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


def _commit_berkshire(engine: AsyncEngine, *slugs: str) -> None:
    async def run() -> None:
        async with AsyncSession(engine) as session:
            await _filer(session, "berkshire-hathaway", BERKSHIRE)
            for slug in slugs:
                await _load(session, slug)
            await session.commit()

    asyncio.run(run())


def _commit_one_of_each(engine: AsyncEngine) -> str:
    """A fund with one of every finding; the suspect filing's accession number.

    2024Q1 and Q2 are three quarters one stock, a quarter another — until a
    150-for-1 split in Q2. Q3 is missing. Q4 is one position. 2025Q1 is suspect.
    """

    async def run() -> str:
        async with AsyncSession(engine) as session:
            fund = await _filer(session)
            await _filing(
                session, fund, Q1, held(ALPHA, 1_000_000, at="300"), held(BRAVO, 2_000_000, at="50")
            )
            await _filing(
                session, fund, Q2, held(ALPHA, 150_000_000, at="2"), held(BRAVO, 2_000_000, at="50")
            )
            await _filing(session, fund, Q4, held(CHARLIE, 5_000_000, at="10"))
            suspect = await _filing(
                session, fund, NEXT_Q1, held(CHARLIE, 5_000_000, at="10"), status="suspect"
            )
            await session.commit()
            return suspect

    return asyncio.run(run())


def test_check_data_exits_zero_when_there_is_nothing_to_look_at(committed: AsyncEngine) -> None:
    _commit_berkshire(committed, "berkshire-2022q3-thousands", "berkshire-2022q4-dollars")

    result = CliRunner().invoke(app, ["check-data"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "check-data  every filer: nothing to look at — 0 suspect periods, "
        "0 concentrated periods, 0 position jumps, 0 filing gaps"
    ]


def test_check_data_exits_one_and_lists_every_finding(committed: AsyncEngine) -> None:
    suspect = _commit_one_of_each(committed)

    result = CliRunner().invoke(app, ["check-data"])

    assert result.exit_code == 1, result.output
    assert result.stdout.splitlines() == [
        "check-data  every filer: 4 findings — 1 suspect period, 1 concentrated period, "
        "1 position jump, 1 filing gap",
        "  suspect periods: withheld from position_snapshot",
        f"    a-fund  2025Q1  {suspect}  13F-HR  failed entry_count",
        "  concentrated periods: one position over 90% of the period's value",
        "    a-fund  2024Q4  33333C303 CHARLIE CORP  100.0% of $50,000,000 across 1 position",
        "  position jumps: shares up more than 10,000% on the quarter before",
        "    a-fund  2024Q2  11111A101 ALPHA CORP SH",
        "                1,000,000 -> 150,000,000 (+14,900%), price $300.00 -> $2.00",
        "  filing gaps: quarters with no 13F loaded",
        "    a-fund  2024Q3  between 2024Q2 and 2024Q4; nothing on file: check EDGAR, "
        "then discover-filings",
    ]


def test_check_data_include_suspect_checks_what_it_would_publish(committed: AsyncEngine) -> None:
    """2025Q1 is one position too. Published, it would be a concentrated period."""
    _commit_one_of_each(committed)

    result = CliRunner().invoke(app, ["check-data", "--include-suspect"])

    assert result.exit_code == 1, result.output
    lines = result.stdout.splitlines()
    assert (
        "  suspect periods: checked below as recompute --include-suspect would publish them"
    ) in lines
    assert "2 concentrated periods" in lines[0]


def test_check_data_refuses_a_filer_that_does_not_exist(committed: AsyncEngine) -> None:
    """Exit 2, not 0: a typo that checked nothing must not read as a clean bill."""
    result = CliRunner().invoke(app, ["check-data", "--filer", "no-such-fund"])

    assert result.exit_code == 2
    assert "no filer has the slug 'no-such-fund'" in result.stderr
