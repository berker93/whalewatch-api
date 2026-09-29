"""The ``effective_filing`` views, and the overlap audit that checks their policy.

Every per-filer total in the product is going to be read through
``effective_filing``, so the cases here are the ones that would otherwise be
silent: a restated period counted alongside its restatement, a confidential
position released by amendment and then dropped, one fund's book counted twice
because it changed legal entity mid-quarter. Each of those produces a
portfolio that looks entirely plausible.

Rows are inserted directly rather than through the loader: what is under test
is which of several loaded filings count, not how they got loaded.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import TableClause

from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, Security
from app.db.queries.effective import EFFECTIVE_FILING, EFFECTIVE_FILING_BY_CIK
from app.db.queries.overlaps import Verdict, audit_overlaps

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
OLD = "0000000001"
NEW = "0000000002"
FILED = datetime(2024, 5, 15, 16, 0, tzinfo=UTC)

_accessions = count(1)


async def _filer(session: AsyncSession, *, overlap: str = "successor") -> int:
    result = await session.execute(
        insert(Filer)
        .values(name="A Fund", slug=f"fund-{next(_accessions)}", overlap=overlap)
        .returning(Filer.id)
    )
    return result.scalar_one()


async def _cik(session: AsyncSession, filer_id: int, cik: str, priority: int) -> None:
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=priority))


async def _filing(
    session: AsyncSession,
    filer_id: int,
    *,
    cik: str = OLD,
    period: date = Q1,
    form: str = "13F-HR",
    kind: AmendmentKind | None = None,
    days_later: int = 0,
    status: str = "ok",
) -> str:
    """Insert a filing; return its accession number, which is what tests compare."""
    accession = f"{cik}-24-{next(_accessions):06d}"
    await session.execute(
        insert(Filing).values(
            accession_no=accession,
            cik=cik,
            filer_id=filer_id,
            form_type=form,
            period_of_report=period,
            filed_at=FILED + timedelta(days=days_later),
            value_multiplier=1,
            amendment_kind=kind,
            parse_status=status,
            parse_notes=[{"kind": "row_count"}] if status == "suspect" else None,
        )
    )
    return accession


async def _effective(
    session: AsyncSession, period: date = Q1, view: TableClause = EFFECTIVE_FILING
) -> set[str]:
    rows = await session.execute(
        select(Filing.accession_no)
        .join(view, view.c.filing_id == Filing.id)
        .where(view.c.period_of_report == period)
    )
    return set(rows.scalars())


# --- within one CIK: amendments ---------------------------------------------


async def test_an_original_filing_counts(db_session: AsyncSession) -> None:
    filer = await _filer(db_session)
    original = await _filing(db_session, filer)

    assert await _effective(db_session) == {original}


async def test_a_restatement_replaces_the_original(db_session: AsyncSession) -> None:
    filer = await _filer(db_session)
    await _filing(db_session, filer)
    restated = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.RESTATEMENT, days_later=30
    )

    assert await _effective(db_session) == {restated}


async def test_the_latest_of_several_restatements_wins(db_session: AsyncSession) -> None:
    filer = await _filer(db_session)
    await _filing(db_session, filer)
    await _filing(db_session, filer, form="13F-HR/A", kind=AmendmentKind.RESTATEMENT, days_later=10)
    latest = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.RESTATEMENT, days_later=20
    )

    assert await _effective(db_session) == {latest}


async def test_a_new_holdings_amendment_adds_to_the_original(db_session: AsyncSession) -> None:
    """The confidential-treatment case: the original stands, the amendment adds
    the positions that were withheld from it."""
    filer = await _filer(db_session)
    original = await _filing(db_session, filer)
    released = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.NEW_HOLDINGS, days_later=200
    )

    assert await _effective(db_session) == {original, released}


async def test_a_restatement_supersedes_earlier_additions_but_not_later_ones(
    db_session: AsyncSession,
) -> None:
    filer = await _filer(db_session)
    await _filing(db_session, filer)
    await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.NEW_HOLDINGS, days_later=10
    )
    restated = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.RESTATEMENT, days_later=20
    )
    added_after = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.NEW_HOLDINGS, days_later=30
    )

    assert await _effective(db_session) == {restated, added_after}


async def test_additions_count_while_the_original_is_not_loaded_yet(
    db_session: AsyncSession,
) -> None:
    filer = await _filer(db_session)
    released = await _filing(
        db_session, filer, form="13F-HR/A", kind=AmendmentKind.NEW_HOLDINGS, days_later=200
    )

    assert await _effective(db_session) == {released}


@pytest.mark.parametrize(
    ("form", "kind", "status"),
    [
        ("13F-HR", None, "pending"),  # no holdings loaded yet
        ("13F-HR", None, "failed"),  # no holdings at all
        ("13F-NT", None, "ok"),  # a notice reports no holdings by definition
        ("13F-HR/A", None, "ok"),  # an amendment of unknown kind: not guessed at
    ],
)
async def test_filings_without_countable_holdings_are_left_out(
    db_session: AsyncSession, form: str, kind: AmendmentKind | None, status: str
) -> None:
    filer = await _filer(db_session)
    original = await _filing(db_session, filer)
    await _filing(db_session, filer, form=form, kind=kind, status=status, days_later=5)

    assert await _effective(db_session) == {original}


async def test_a_suspect_filing_still_counts(db_session: AsyncSession) -> None:
    """Suspect is loaded and believed with reservations — not withheld."""
    filer = await _filer(db_session)
    suspect = await _filing(db_session, filer, status="suspect")

    assert await _effective(db_session) == {suspect}


# --- across CIKs: the overlap policy -----------------------------------------


async def test_under_successor_only_the_last_listed_cik_counts_in_an_overlap(
    db_session: AsyncSession,
) -> None:
    filer = await _filer(db_session)
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 1)
    await _filing(db_session, filer, cik=OLD)
    successor = await _filing(db_session, filer, cik=NEW)

    assert await _effective(db_session) == {successor}
    # ... and the per-CIK view, which the audit reads, still has both.
    assert len(await _effective(db_session, view=EFFECTIVE_FILING_BY_CIK)) == 2


async def test_under_successor_a_period_only_one_cik_filed_is_unaffected(
    db_session: AsyncSession,
) -> None:
    """A predecessor's history outside the overlap is the filer's history."""
    filer = await _filer(db_session)
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 1)
    old_only = await _filing(db_session, filer, cik=OLD, period=Q1)
    new_only = await _filing(db_session, filer, cik=NEW, period=Q2)

    assert await _effective(db_session, Q1) == {old_only}
    assert await _effective(db_session, Q2) == {new_only}


async def test_under_successor_a_failed_successor_falls_back_to_the_predecessor(
    db_session: AsyncSession,
) -> None:
    filer = await _filer(db_session)
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 1)
    predecessor = await _filing(db_session, filer, cik=OLD)
    await _filing(db_session, filer, cik=NEW, status="failed")

    assert await _effective(db_session) == {predecessor}


async def test_under_sum_every_cik_counts(db_session: AsyncSession) -> None:
    filer = await _filer(db_session, overlap="sum")
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 1)
    first = await _filing(db_session, filer, cik=OLD)
    second = await _filing(db_session, filer, cik=NEW)

    assert await _effective(db_session) == {first, second}


async def test_amendments_resolve_per_cik_before_the_policy_applies(
    db_session: AsyncSession,
) -> None:
    """The successor CIK's restatement and addition both count; nothing of the
    predecessor's does."""
    filer = await _filer(db_session)
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 1)
    await _filing(db_session, filer, cik=OLD)
    await _filing(db_session, filer, cik=NEW)
    restated = await _filing(
        db_session, filer, cik=NEW, form="13F-HR/A", kind=AmendmentKind.RESTATEMENT, days_later=9
    )
    added = await _filing(
        db_session, filer, cik=NEW, form="13F-HR/A", kind=AmendmentKind.NEW_HOLDINGS, days_later=99
    )

    assert await _effective(db_session) == {restated, added}


async def test_equal_priorities_still_resolve_to_one_cik(db_session: AsyncSession) -> None:
    """Rows inserted outside the seed all have priority 0. A tie must not sum."""
    filer = await _filer(db_session)
    await _cik(db_session, filer, OLD, 0)
    await _cik(db_session, filer, NEW, 0)
    await _filing(db_session, filer, cik=OLD)
    higher_cik = await _filing(db_session, filer, cik=NEW)

    assert await _effective(db_session) == {higher_cik}


# --- the audit ---------------------------------------------------------------


async def _holdings(
    session: AsyncSession, accession: str, positions: dict[str, tuple[int, int]]
) -> None:
    """``{cusip: (shares, value_usd)}`` onto an existing filing."""
    filing = (
        await session.execute(select(Filing).where(Filing.accession_no == accession))
    ).scalar_one()
    for cusip, (shares, value) in positions.items():
        security_id = await session.scalar(select(Security.id).where(Security.cusip == cusip))
        if security_id is None:
            security_id = await session.scalar(
                insert(Security).values(cusip=cusip).returning(Security.id)
            )
        await session.execute(
            insert(Holding).values(
                filing_id=filing.id,
                security_id=security_id,
                filer_id=filing.filer_id,
                period_of_report=filing.period_of_report,
                cusip=cusip,
                shares=Decimal(shares),
                value_usd=Decimal(value),
                sshprnamt_type="SH",
            )
        )


BOOK = {"037833100": (1_000, 200_000), "594918104": (500, 150_000), "191216100": (100, 6_000)}


async def _overlap(
    session: AsyncSession, *, overlap: str, other: dict[str, tuple[int, int]]
) -> None:
    filer = await _filer(session, overlap=overlap)
    await _cik(session, filer, OLD, 0)
    await _cik(session, filer, NEW, 1)
    await _holdings(session, await _filing(session, filer, cik=NEW), BOOK)
    await _holdings(session, await _filing(session, filer, cik=OLD), other)


async def test_one_book_filed_twice_is_recognised_and_agrees_with_successor(
    db_session: AsyncSession,
) -> None:
    await _overlap(db_session, overlap="successor", other=BOOK)

    (finding,) = await audit_overlaps(db_session)

    assert finding.primary_cik == NEW
    assert finding.other_cik == OLD
    assert finding.verdict is Verdict.SAME_BOOK
    assert finding.conflict is None


async def test_separate_books_under_successor_are_flagged_with_what_is_dropped(
    db_session: AsyncSession,
) -> None:
    """Same names, different share counts: two advisers, two books."""
    other = {cusip: (shares * 3, value * 3) for cusip, (shares, value) in BOOK.items()}
    await _overlap(db_session, overlap="successor", other=other)

    (finding,) = await audit_overlaps(db_session)

    assert finding.verdict is Verdict.SEPARATE_BOOKS
    assert finding.conflict is not None
    assert "drops $1,068,000" in finding.conflict


async def test_one_book_filed_twice_under_sum_is_flagged_as_double_counting(
    db_session: AsyncSession,
) -> None:
    await _overlap(db_session, overlap="sum", other=BOOK)

    (finding,) = await audit_overlaps(db_session)

    assert finding.conflict is not None
    assert "double counted" in finding.conflict


async def test_a_partial_match_is_unclear_rather_than_guessed(db_session: AsyncSession) -> None:
    other = dict(BOOK, **{"037833100": (9_999, 200_000)})
    await _overlap(db_session, overlap="successor", other=other)

    (finding,) = await audit_overlaps(db_session)

    assert finding.verdict is Verdict.UNCLEAR
    assert finding.conflict is None


async def test_no_overlaps_means_no_findings(db_session: AsyncSession) -> None:
    filer = await _filer(db_session)
    await _holdings(db_session, await _filing(db_session, filer), BOOK)

    assert await audit_overlaps(db_session) == []
