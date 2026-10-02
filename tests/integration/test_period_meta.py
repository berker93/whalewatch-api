"""``meta`` for a period: how much of the universe it covers, and how fresh it is.

Built from published data only. The interesting cases are the ones where a
filing exists and is not the data: a suspect filing withheld from its period,
and a filer that has not filed yet. Both have to read as not reported, and
the withheld filing must not make the period look newer than what is served.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.meta import period_meta, quarter_label
from app.db.models import Filer, FilerCik, Filing, Holding, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views

Q1 = date(2026, 3, 31)
Q2 = date(2026, 6, 30)

_ciks = count(1)
_accessions = count(1)


async def _fund(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(insert(Filer).values(name=slug, slug=slug).returning(Filer.id))
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=1)
    )
    return filer_id


async def _file(
    session: AsyncSession, filer_id: int, period: date, filed_at: datetime, *, suspect: bool = False
) -> None:
    """A 13F-HR for ``period`` holding one position, inserted as loaded."""
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
            filed_at=filed_at,
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
        pg_insert(Security)
        .values(cusip="037833100")
        .on_conflict_do_nothing(index_elements=[Security.cusip])
    )
    await session.execute(
        insert(Holding).values(
            filing_id=filing_id,
            security_id=select(Security.id).where(Security.cusip == "037833100").scalar_subquery(),
            filer_id=filer_id,
            period_of_report=period,
            cusip="037833100",
            value_usd=Decimal(1_000),
            shares=Decimal(10),
            sshprnamt_type="SH",
        )
    )


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, tzinfo=UTC)


@pytest.fixture
async def universe(db_session: AsyncSession) -> None:
    """Four filers. For Q1: two publish, one's filing is suspect and the newest,
    one has not filed. Nothing at all for Q2."""
    alpha, bravo, charlie, _ = [
        await _fund(db_session, slug) for slug in ("alpha", "bravo", "charlie", "delta")
    ]
    await _file(db_session, alpha, Q1, _at(date(2026, 5, 1)))
    await _file(db_session, bravo, Q1, _at(date(2026, 5, 14)))
    await _file(db_session, charlie, Q1, _at(date(2026, 5, 15)), suspect=True)


async def test_a_period_states_its_coverage_and_freshness(
    db_session: AsyncSession, universe: None
) -> None:
    await recompute(db_session, EVERYTHING)
    await refresh_views(db_session)

    meta = await period_meta(db_session, Q1)

    assert (meta.period, meta.period_end) == ("2026Q1", Q1)
    assert (meta.coverage.filers_reported, meta.coverage.filers_tracked) == (2, 4)  # type: ignore[union-attr]
    # Bravo's, not Charlie's later one: that filing is withheld, so it is not
    # in anything this period serves.
    assert meta.latest_filing_at == _at(date(2026, 5, 14))
    assert meta.generated_at.tzinfo is not None


async def test_a_suspect_filing_published_on_purpose_counts(
    db_session: AsyncSession, universe: None
) -> None:
    await recompute(db_session, EVERYTHING, include_suspect=True)
    await refresh_views(db_session)

    meta = await period_meta(db_session, Q1)

    assert meta.coverage.filers_reported == 3  # type: ignore[union-attr]
    assert meta.latest_filing_at == _at(date(2026, 5, 15))


async def test_a_period_nothing_has_published_says_so(
    db_session: AsyncSession, universe: None
) -> None:
    await recompute(db_session, EVERYTHING)
    await refresh_views(db_session)

    meta = await period_meta(db_session, Q2)

    assert meta.period == "2026Q2"
    assert meta.coverage.filers_reported == 0  # type: ignore[union-attr]
    assert meta.latest_filing_at is None


async def test_coverage_is_as_of_the_last_refresh(db_session: AsyncSession, universe: None) -> None:
    """The documented trade: published counts come from ``mv_filer_summary``."""
    await recompute(db_session, EVERYTHING)

    meta = await period_meta(db_session, Q1)

    assert meta.coverage.filers_reported == 0  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("period", "label"),
    [(date(2024, 3, 31), "2024Q1"), (date(2024, 6, 30), "2024Q2"), (date(2024, 12, 31), "2024Q4")],
)
def test_quarter_label(period: date, label: str) -> None:
    assert quarter_label(period) == label


def test_a_day_that_is_not_a_quarter_end_has_no_label() -> None:
    with pytest.raises(ValueError, match="not a quarter end"):
        quarter_label(date(2024, 3, 30))
