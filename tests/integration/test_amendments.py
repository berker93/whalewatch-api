"""Amendments, resolved over real filings: Berkshire's 2023Q3 and 2023Q4, whole.

``test_effective_filing`` proves the ``effective_filing`` rules over rows made
up for the purpose. This file proves them over what EDGAR actually holds for two
periods, loaded through the real parser and loader, because the rules are only
as good as the ``amendment_kind`` they are fed — and a classification is only
proven against documents nobody wrote for a test.

The two periods are the two shapes an amendment takes:

* **2023Q3** — the original; a restatement two days later, the same 152 rows
  with the Other Manager column filled in; and six months after that a
  ``NEW HOLDINGS`` amendment releasing Chubb from confidential treatment. The
  restatement is the whole period, so the original and the restatement summed
  is the same $313bn book twice. The addition comes after it and adds to it.
* **2023Q4** — the original, 138 rows filed with ``isConfidentialOmitted``,
  and one ``NEW HOLDINGS`` row. Read as a restatement, that row replaces the
  other 41 positions.

Both mistakes load without an error, and both produce a portfolio that looks
like one.
"""

import asyncio
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding
from app.db.queries.amendments import PeriodResolution, Role, audit_amendments
from app.db.queries.effective import resolved_filings
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from tests.conftest import make_settings
from tests.fixtures_13f import ADDED_TO_PERIOD, RESTATED_PERIOD, Fixture, by_slug, load_fixtures

BERKSHIRE = "0001067983"
Q3 = date(2023, 9, 30)
Q4 = date(2023, 12, 31)
CHUBB = "H1467J104"

Q3_ORIGINAL, Q3_RESTATEMENT, Q3_NEW_HOLDINGS = RESTATED_PERIOD
Q4_ORIGINAL, Q4_NEW_HOLDINGS = ADDED_TO_PERIOD

# The filers' own tableValueTotal, in whole dollars: every one of these filings
# is after the 2023-01-03 cutover.
Q3_BOOK = Decimal(313_257_308_189)
Q3_CHUBB = Decimal(1_695_320_075)
Q4_BOOK = Decimal(347_358_074_461)
Q4_CHUBB = Decimal(4_542_600_000)

_AS_FILED: Any = object()


@pytest.fixture
async def berkshire(db_session: AsyncSession) -> int:
    return await _berkshire(db_session)


async def _berkshire(session: AsyncSession) -> int:
    filer_id = await session.scalar(
        insert(Filer)
        .values(name="Berkshire Hathaway Inc", slug="berkshire-hathaway")
        .returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=BERKSHIRE, priority=1))
    return filer_id


async def _load(
    session: AsyncSession,
    slug: str,
    *,
    kind: AmendmentKind | None = _AS_FILED,
    cik: str | None = None,
    accession_no: str | None = None,
) -> int:
    """One committed fixture, through the parser, normalisation and the loader.

    ``kind`` overrides what the cover page says, for the tests that show what a
    misread costs. ``cik`` and ``accession_no`` re-file the same document under
    another identity, for the overlap case.
    """
    fixture = by_slug(slug)
    cover, table = fixture.parse()
    overrides: dict[str, Any] = {}
    if kind is not _AS_FILED:
        overrides["amendment_kind"] = kind
    if cik is not None:
        overrides["cik"] = cik
    cover = cover.model_copy(update=overrides)
    result = await load_filing(
        session,
        accession_no=accession_no or fixture.accession_no,
        filed_at=fixture.filed_at,
        primary_doc=cover,
        normalised=normalise_filing(filed_at=fixture.filed_at, cover=cover, table=table),
    )
    return result.filing_id


_Position = tuple[str, str | None, str, Decimal, Decimal]


async def _holdings(session: AsyncSession, filing_ids: Iterable[int]) -> list[_Position]:
    """``(cusip, put_call, sshprnamt_type, shares, value_usd)`` for every row, sorted."""
    rows = await session.execute(
        select(
            Holding.cusip,
            Holding.put_call,
            Holding.sshprnamt_type,
            Holding.shares,
            Holding.value_usd,
        ).where(Holding.filing_id.in_(list(filing_ids)))
    )
    return sorted(rows.tuples(), key=_natural_key)


def _natural_key(position: _Position) -> tuple[str, str, str]:
    """Sortable with ``put_call`` null, which a plain tuple sort is not."""
    return position[0], position[1] or "", position[2]


def _total(positions: list[_Position]) -> Decimal:
    return sum((position[4] for position in positions), start=Decimal(0))


async def _resolved(session: AsyncSession, filer_id: int, period: date) -> list[str]:
    filings = await resolved_filings(session, filer_id=filer_id, period=period)
    return [filing.accession_no for filing in filings]


def _accession(slug: str) -> str:
    return by_slug(slug).accession_no


# --- classification, from the document to the column --------------------------

_AMENDMENT_TYPE = re.compile(rb"<(?:[\w.\-]+:)?amendmentType>\s*([^<]*?)\s*</")
_AMENDMENT_NO = re.compile(rb"<(?:[\w.\-]+:)?amendmentNo>\s*(\d+)\s*</")
_DECLARED_KINDS = {
    b"RESTATEMENT": AmendmentKind.RESTATEMENT,
    b"NEW HOLDINGS": AmendmentKind.NEW_HOLDINGS,
}


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda fixture: fixture.slug)
async def test_every_filing_is_stored_with_the_amendment_its_cover_page_declares(
    db_session: AsyncSession, berkshire: int, fixture: Fixture
) -> None:
    """Every golden fixture, loaded, and its row checked against its own bytes.

    The expectation is read out of the raw ``primary_doc.xml`` with a regex
    that shares no code with the parser — so this is a second reading of the
    document, not the parser agreeing with itself. The fixtures that are not
    Berkshire's load with their holdings deferred; the filing row, which is
    what is under test, is written either way.
    """
    raw = fixture.primary_doc_bytes()
    declared_type = _AMENDMENT_TYPE.search(raw)
    declared_no = _AMENDMENT_NO.search(raw)

    filing = await db_session.get(Filing, await _load(db_session, fixture.slug))

    assert filing is not None
    assert filing.amendment_kind == (
        _DECLARED_KINDS[declared_type.group(1).upper()] if declared_type else None
    )
    assert filing.amendment_no == (int(declared_no.group(1)) if declared_no else None)
    # And nothing slipped through as "not an amendment": every /A in the set
    # declares which kind it is, and every original declares neither.
    assert (filing.amendment_kind is not None) == filing.form_type.upper().endswith("/A")


# --- resolution, over the real periods ----------------------------------------


@pytest.mark.parametrize(
    "order",
    [(Q3_ORIGINAL, Q3_RESTATEMENT), (Q3_RESTATEMENT, Q3_ORIGINAL)],
    ids=["original-first", "restatement-first"],
)
async def test_a_restatement_replaces_its_original_rather_than_adding_to_it(
    db_session: AsyncSession, berkshire: int, order: tuple[str, str]
) -> None:
    """The resolved holdings are the restatement's, not the union of both.

    In either load order: a backfill loads filings concurrently, so the one
    that lands second is not necessarily the one EDGAR accepted second.
    """
    ids = {slug: await _load(db_session, slug) for slug in order}

    resolved = await resolved_filings(db_session, filer_id=berkshire, period=Q3)
    holdings = await _holdings(db_session, (filing.id for filing in resolved))

    assert [filing.accession_no for filing in resolved] == [_accession(Q3_RESTATEMENT)]
    assert holdings == await _holdings(db_session, [ids[Q3_RESTATEMENT]])
    assert _total(holdings) == Q3_BOOK
    # What reading it as an addition would have reported: the book twice.
    union = await _holdings(db_session, ids.values())
    assert _total(union) == 2 * Q3_BOOK


async def test_a_new_holdings_amendment_adds_to_its_original(
    db_session: AsyncSession, berkshire: int
) -> None:
    """The resolved holdings are the original's and the amendment's, together."""
    original = await _load(db_session, Q4_ORIGINAL)
    amendment = await _load(db_session, Q4_NEW_HOLDINGS)

    resolved = await resolved_filings(db_session, filer_id=berkshire, period=Q4)
    holdings = await _holdings(db_session, (filing.id for filing in resolved))

    assert [filing.accession_no for filing in resolved] == [
        _accession(Q4_ORIGINAL),
        _accession(Q4_NEW_HOLDINGS),
    ]
    original_holdings = await _holdings(db_session, [original])
    amendment_holdings = await _holdings(db_session, [amendment])
    assert holdings == sorted(original_holdings + amendment_holdings, key=_natural_key)
    assert len(holdings) == len(original_holdings) + 1 == 42
    assert CHUBB in {position[0] for position in holdings}
    assert _total(holdings) == Q4_BOOK + Q4_CHUBB


async def test_an_addition_filed_after_a_restatement_adds_to_the_restatement(
    db_session: AsyncSession, berkshire: int
) -> None:
    """All three 2023Q3 filings: the restatement plus Amendment No. 2, not the original."""
    for slug in RESTATED_PERIOD:
        await _load(db_session, slug)

    assert await _resolved(db_session, berkshire, Q3) == [
        _accession(Q3_RESTATEMENT),
        _accession(Q3_NEW_HOLDINGS),
    ]
    resolved = await resolved_filings(db_session, filer_id=berkshire, period=Q3)
    holdings = await _holdings(db_session, (filing.id for filing in resolved))
    assert _total(holdings) == Q3_BOOK + Q3_CHUBB


@pytest.mark.parametrize(
    ("slugs", "misread", "positions", "total"),
    [
        pytest.param(
            (Q3_ORIGINAL, Q3_RESTATEMENT),
            AmendmentKind.NEW_HOLDINGS,
            90,
            2 * Q3_BOOK,
            id="restatement-read-as-addition-doubles",
        ),
        pytest.param(
            (Q4_ORIGINAL, Q4_NEW_HOLDINGS),
            AmendmentKind.RESTATEMENT,
            1,
            Q4_CHUBB,
            id="addition-read-as-restatement-drops-the-rest",
        ),
    ],
)
async def test_reading_the_kind_backwards_is_what_goes_wrong(
    db_session: AsyncSession,
    berkshire: int,
    slugs: tuple[str, str],
    misread: AmendmentKind,
    positions: int,
    total: Decimal,
) -> None:
    """The same documents, with only the amendment's kind flipped.

    Neither raises; both resolve to a portfolio. This is the failure the two
    tests above are guarding against, measured in real money — and the proof
    that it is the classification, and nothing else about the filings, that
    they depend on.
    """
    original, amendment = slugs
    await _load(db_session, original)
    await _load(db_session, amendment, kind=misread)
    period = date.fromisoformat(by_slug(original).period_of_report)

    resolved = await resolved_filings(db_session, filer_id=berkshire, period=period)
    holdings = await _holdings(db_session, (filing.id for filing in resolved))

    assert len(holdings) == positions
    assert _total(holdings) == total


# --- the report ----------------------------------------------------------------


def _period(periods: list[PeriodResolution], period: date) -> PeriodResolution:
    (found,) = (candidate for candidate in periods if candidate.period == period)
    return found


async def test_the_report_lists_every_period_with_more_than_one_filing(
    db_session: AsyncSession, berkshire: int
) -> None:
    for slug in (*RESTATED_PERIOD, *ADDED_TO_PERIOD, "berkshire-2022q4-dollars"):
        await _load(db_session, slug)

    periods = await audit_amendments(db_session)

    # 2022Q4 has one filing and nothing to resolve.
    assert [(period.slug, period.period) for period in periods] == [
        ("berkshire-hathaway", Q3),
        ("berkshire-hathaway", Q4),
    ]

    q3 = _period(periods, Q3)
    assert q3.resolution == "restated, plus 1 addition"
    assert [(f.accession_no, f.role, f.counts) for f in q3.filings] == [
        (_accession(Q3_ORIGINAL), Role.REPLACED, False),
        (_accession(Q3_RESTATEMENT), Role.WHOLE_PERIOD, True),
        (_accession(Q3_NEW_HOLDINGS), Role.ADDS, True),
    ]
    assert q3.filings[0].reason == f"replaced by {_accession(Q3_RESTATEMENT)}"
    assert q3.filings[2].reason == f"counts: adds to {_accession(Q3_RESTATEMENT)}"
    assert [f.amendment_no for f in q3.filings] == [None, 1, 2]
    assert q3.value_usd == Q3_BOOK + Q3_CHUBB
    assert q3.concerns == ()

    q4 = _period(periods, Q4)
    assert q4.resolution == "original, plus 1 addition"
    assert [f.role for f in q4.filings] == [Role.WHOLE_PERIOD, Role.ADDS]
    assert q4.positions == 42
    assert q4.value_usd == Q4_BOOK + Q4_CHUBB
    assert q4.concerns == ()


async def test_the_report_totals_agree_with_the_resolved_filings(
    db_session: AsyncSession, berkshire: int
) -> None:
    """The report reads what counts from the view, so it cannot sum differently."""
    for slug in RESTATED_PERIOD:
        await _load(db_session, slug)

    (q3,) = await audit_amendments(db_session)

    assert [f.accession_no for f in q3.counting] == await _resolved(db_session, berkshire, Q3)


async def test_an_amendment_of_unknown_kind_is_reported_as_left_out(
    db_session: AsyncSession, berkshire: int
) -> None:
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, Q4_NEW_HOLDINGS, kind=None)

    (q4,) = await audit_amendments(db_session)

    assert q4.resolution == "original"
    assert [f.role for f in q4.filings] == [Role.WHOLE_PERIOD, Role.UNCLASSIFIED]
    (concern,) = q4.concerns
    assert _accession(Q4_NEW_HOLDINGS) in concern
    assert "no amendmentType" in concern


async def test_a_gap_in_the_amendment_numbers_is_flagged(
    db_session: AsyncSession, berkshire: int
) -> None:
    """2023Q3 without its restatement: Amendment No. 2 is loaded and No. 1 is not.

    By the rules the period resolves — the original plus the addition — and
    that is the wrong answer only because a filing is missing, which is why
    the number is kept at all.
    """
    await _load(db_session, Q3_ORIGINAL)
    await _load(db_session, Q3_NEW_HOLDINGS)

    (q3,) = await audit_amendments(db_session)

    assert q3.resolution == "original, plus 1 addition"
    (concern,) = q3.concerns
    assert concern.startswith(f"amendment no. 1 for CIK {BERKSHIRE}")


async def test_additions_counting_without_their_original_are_flagged(
    db_session: AsyncSession, berkshire: int
) -> None:
    """The original is queued but not parsed: only the released row counts."""
    await _load(db_session, Q4_NEW_HOLDINGS)
    original = by_slug(Q4_ORIGINAL)
    await db_session.execute(
        insert(Filing).values(
            accession_no=original.accession_no,
            cik=BERKSHIRE,
            filer_id=berkshire,
            form_type="13F-HR",
            period_of_report=Q4,
            filed_at=original.filed_at,
            value_multiplier=1,
        )
    )

    (q4,) = await audit_amendments(db_session)

    assert q4.resolution == "1 addition only"
    assert [(f.role, f.reason) for f in q4.filings] == [
        (Role.NOT_LOADED, "not parsed yet"),
        (Role.ADDS, "counts: an addition, with nothing loaded to add to"),
    ]
    (concern,) = q4.concerns
    assert "no original or restatement is loaded" in concern


async def test_a_period_resolved_by_the_overlap_policy_says_so(
    db_session: AsyncSession, berkshire: int
) -> None:
    """The same original refiled under a predecessor CIK, which the successor
    policy leaves out — explained, rather than listed as if it were replaced."""
    predecessor = "0000000001"
    await db_session.execute(
        insert(FilerCik).values(filer_id=berkshire, cik=predecessor, priority=0)
    )
    await _load(db_session, Q4_ORIGINAL)
    await _load(db_session, Q4_ORIGINAL, cik=predecessor, accession_no="0000000001-24-000001")

    (q4,) = await audit_amendments(db_session)

    assert q4.resolution == "original"
    by_cik = {f.cik: (f.role, f.reason) for f in q4.filings}
    assert by_cik[BERKSHIRE] == (Role.WHOLE_PERIOD, "counts: the whole period")
    assert by_cik[predecessor] == (
        Role.OTHER_CIK,
        f"overlap policy successor: CIK {BERKSHIRE} counts instead",
    )
    assert q4.concerns == ()


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
                    "TRUNCATE holding, filing, security, filer_cik, filer RESTART IDENTITY CASCADE"
                )
            )

    asyncio.run(run())


def _commit(engine: AsyncEngine, *slugs: str) -> None:
    async def run() -> None:
        async with AsyncSession(engine) as session:
            await _berkshire(session)
            for slug in slugs:
                await _load(session, slug)
            await session.commit()

    asyncio.run(run())


def test_audit_amendments_with_nothing_to_resolve_says_so(committed: AsyncEngine) -> None:
    result = CliRunner().invoke(app, ["audit-amendments"])

    assert result.exit_code == 0, result.output
    assert "no filer has more than one filing for any period" in result.stdout


def test_audit_amendments_prints_how_each_period_resolved(committed: AsyncEngine) -> None:
    _commit(committed, *RESTATED_PERIOD, Q4_NEW_HOLDINGS)

    result = CliRunner().invoke(app, ["audit-amendments", "--filer", "berkshire-hathaway"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == (
        "audit-amendments  1 periods with more than one filing across 1 filers, 0 to look at"
    )
    assert lines[1] == (
        "  berkshire-hathaway  2023Q3  restated, plus 1 addition: 46 positions, $314,952,628,264"
    )
    original, restatement, addition = lines[2:5]
    assert _accession(Q3_ORIGINAL) in original
    assert original.endswith(f"replaced by {_accession(Q3_RESTATEMENT)}")
    assert "13F-HR/A no.1 restatement" in restatement
    assert restatement.endswith("counts: the whole period")
    assert "13F-HR/A no.2 new holdings" in addition
    assert "$      1,695,320,075" in addition
    assert len(lines) == 5
