"""``implied_price``: the median of what each holder's filing says a share was worth.

Each test publishes one security in one quarter, held by a few made-up filers
whose shares and values are picked to make the price come out a particular way:
holders that agree, a minority that does not (in thousands, before a split, at
book value), holders that put no value on it. ``test_recompute`` proves the
table is rebuilt for the right keys; ``test_reconcile`` proves the invariants
catch it when it is not.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filer, FilerCik, Filing, Holding, ImpliedPrice, Security
from app.derived.implied_price import recompute_implied_price
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING

D = Decimal

Q1 = date(2024, 3, 31)
ALPHA = "11111A101"

_numbers = count(1)


@dataclass(frozen=True, slots=True)
class _Price:
    """An ``implied_price`` row, less its keys and ``computed_at``."""

    price: Decimal | None
    observation_count: int
    unpriced_count: int
    agreeing_count: int
    confidence: str | None
    suspect: bool = False


async def _held_by(
    session: AsyncSession, *positions: tuple[int, int | Decimal], suspect: bool = False
) -> None:
    """ALPHA in Q1, one made-up filer per ``(shares, value_usd)``, each filing its own 13F."""
    await session.execute(
        pg_insert(Security)
        .values(cusip=ALPHA)
        .on_conflict_do_nothing(index_elements=[Security.cusip])
    )
    for shares, value in positions:
        number = next(_numbers)
        cik = f"{number:010d}"
        filer_id = await session.scalar(
            insert(Filer).values(name=f"fund {number}", slug=f"fund-{number}").returning(Filer.id)
        )
        await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
        filing_id = await session.scalar(
            insert(Filing)
            .values(
                accession_no=f"{cik}-24-{number:06d}",
                cik=cik,
                filer_id=filer_id,
                form_type="13F-HR",
                period_of_report=Q1,
                filed_at=datetime(2024, 5, 1, tzinfo=UTC) + timedelta(hours=number),
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
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id).where(Security.cusip == ALPHA).scalar_subquery(),
                filer_id=filer_id,
                period_of_report=Q1,
                cusip=ALPHA,
                value_usd=value,
                shares=shares,
                sshprnamt_type="SH",
            )
        )


async def _price(session: AsyncSession, *, include_suspect: bool = False) -> _Price:
    """ALPHA's Q1 price, after publishing everything."""
    await recompute(session, EVERYTHING, include_suspect=include_suspect)
    row = (
        await session.execute(
            select(ImpliedPrice)
            .join(Security, Security.id == ImpliedPrice.security_id)
            .where(Security.cusip == ALPHA, ImpliedPrice.period_of_report == Q1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _Price(
        price=row.price,
        observation_count=row.observation_count,
        unpriced_count=row.unpriced_count,
        agreeing_count=row.agreeing_count,
        confidence=row.confidence,
        suspect=row.suspect,
    )


# --- how sure ---------------------------------------------------------------------


async def test_three_holders_agreeing_are_a_high_confidence_price(
    db_session: AsyncSession,
) -> None:
    await _held_by(db_session, (100, 1_000), (250, 2_500), (40, 400))

    assert await _price(db_session) == _Price(D(10), 3, 0, 3, "high")


async def test_two_holders_agreeing_are_medium(db_session: AsyncSession) -> None:
    await _held_by(db_session, (100, 1_000), (250, 2_500))

    assert await _price(db_session) == _Price(D(10), 2, 0, 2, "medium")


async def test_one_holder_is_low_however_exact(db_session: AsyncSession) -> None:
    """Eight places: a third of $10 is stored as it divides, not rounded to cents."""
    await _held_by(db_session, (3, 10))

    assert await _price(db_session) == _Price(D("3.33333333"), 1, 0, 1, "low")


async def test_two_holders_disagreeing_are_low_at_the_lower_one(db_session: AsyncSession) -> None:
    """Always an observation, never the $20 between them that nobody reported."""
    await _held_by(db_session, (100, 1_000), (100, 3_000))

    assert await _price(db_session) == _Price(D(10), 2, 0, 1, "low")


async def test_many_holders_with_no_majority_agreeing_are_low(db_session: AsyncSession) -> None:
    """Two of four at $10 is agreement, and not a majority of it."""
    await _held_by(db_session, (100, 1_000), (100, 1_000), (100, 2_000), (100, 3_000))

    assert await _price(db_session) == _Price(D(10), 4, 0, 2, "low")


async def test_two_percent_either_way_agrees_and_no_further(db_session: AsyncSession) -> None:
    """$102 is 2% over the $100 median and agrees. $102.01 is not."""
    await _held_by(db_session, (100, 10_000), (100, 10_000), (100, 10_200), (100, 10_201))

    assert await _price(db_session) == _Price(D(100), 4, 0, 3, "high")


# --- what a minority cannot do -------------------------------------------------------


@pytest.mark.parametrize(
    ("shares", "value"),
    [
        # Amazon at $208.27, in thousands after the switch to dollars.
        (1_000_000, 208_270),
        # Amazon's count from before its 20:1 split, at the value after it.
        (50_000, 208_270_000),
        # A strategic stake carried at book value, and a big one.
        (10_000_000, 1_000_000_000),
    ],
    ids=["in thousands", "before a split", "at book value"],
)
async def test_one_holder_off_by_any_factor_does_not_move_the_price(
    db_session: AsyncSession, shares: int, value: int
) -> None:
    """Three holders at $208.27 and one far from it, however large its position.
    A value-weighted price would follow the book-value stake to $100."""
    agreeing = [(1_000, 208_270), (2_000, 416_540), (500, 104_135)]
    await _held_by(db_session, *agreeing, (shares, value))

    assert await _price(db_session) == _Price(D("208.27"), 4, 0, 3, "high")


# --- holders who put no value on it --------------------------------------------------


async def test_a_holder_reporting_nothing_is_counted_and_not_priced(
    db_session: AsyncSession,
) -> None:
    """Two priced, both agreeing: medium, as two holders would be, not low for
    being two of three."""
    await _held_by(db_session, (100, 1_000), (250, 2_500), (40, 0))

    assert await _price(db_session) == _Price(D(10), 3, 1, 2, "medium")


async def test_a_security_nobody_priced_has_a_row_and_no_price(db_session: AsyncSession) -> None:
    await _held_by(db_session, (100, 0), (50, 0))

    assert await _price(db_session) == _Price(None, 2, 2, 0, None)


async def test_a_holding_of_no_shares_is_no_observation(db_session: AsyncSession) -> None:
    """Nothing to divide by. The holder with shares is the price's only observation."""
    await _held_by(db_session, (0, 0), (100, 1_000))

    assert await _price(db_session) == _Price(D(10), 1, 0, 1, "low")


async def test_a_period_published_suspect_marks_its_prices(db_session: AsyncSession) -> None:
    await _held_by(db_session, (100, 1_000))
    await _held_by(db_session, (100, 1_000), suspect=True)

    assert await _price(db_session, include_suspect=True) == _Price(
        D(10), 2, 0, 2, "medium", suspect=True
    )


# --- the keys ------------------------------------------------------------------------


async def test_more_keys_than_bind_parameters_rebuild_in_one_statement(
    db_session: AsyncSession,
) -> None:
    """Forty thousand keys, two to a pair, would be 80,000 parameters as a
    tuple list, and asyncpg stops at 32,767. As two arrays, they are two."""
    keys = frozenset((security_id, Q1) for security_id in range(1, 40_001))

    rebuilt = await recompute_implied_price(db_session, keys)

    assert rebuilt.prices == 0
