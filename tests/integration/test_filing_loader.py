"""The loader, run twice — which is the only way it is ever run in production.

Every test here loads a filing that is already loaded, or loads one that changed
underneath a previous load, because that is what ingestion does: Celery
redelivers, backfills resume from the start of a quarter, and a parser fix means
re-reading every document since January. A test that loads a filing once and
reads it back would pass against a loader that duplicates everything.

Against a real Postgres, necessarily. ``ON CONFLICT DO UPDATE``, ``ON CONFLICT
DO NOTHING``, the ``NULLS NOT DISTINCT`` unique key the delete-and-reinsert
exists to respect, and the composite foreign key tying a holding to its filing's
period are the whole subject — none of them exist outside Postgres.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AmendmentKind, Filer, FilerCik, Filing, Holding, ParseStatus, Security
from app.ingestion.loaders import LoadResult, load_filing
from app.ingestion.normalisation import normalise_filing
from app.ingestion.parsers.thirteen_f import InformationTable, InfoTableRow, PrimaryDoc

ACCESSION = "0001067983-24-000011"
BERKSHIRE = "0001067983"
PERIOD = date(2024, 3, 31)
# After the whole-dollars cutover, so the multiplier is 1 and the values in the
# assertions below are the values in the fixtures.
FILED_AT = datetime(2024, 5, 15, 16, 30, tzinfo=UTC)

APPLE = "037833100"
MICROSOFT = "594918104"
COCA_COLA = "191216100"


def cover(
    *,
    entry_total: int | None = None,
    form_type: str = "13F-HR",
    cik: str = BERKSHIRE,
    period: date = PERIOD,
) -> PrimaryDoc:
    """A cover page. ``entry_total`` defaults to ``None`` — no declared count, so
    no row-count guard — because most tests here are about writes, not verdicts."""
    return PrimaryDoc(
        cik=cik,
        filer_name="Berkshire Hathaway Inc",
        form_type=form_type,
        period_of_report=period,
        signature_date=None,
        amendment_no=None,
        amendment_kind=None,
        report_type="13F HOLDINGS REPORT",
        table_entry_total=entry_total,
        table_value_total=None,
        other_included_managers_count=None,
        confidential_omitted=False,
    )


def row(
    *,
    cusip: str = APPLE,
    value: Decimal = Decimal(1_000_000),
    shares: Decimal = Decimal(10_000),
    **overrides: Any,
) -> InfoTableRow:
    fields: dict[str, Any] = {
        "name_of_issuer": "APPLE INC",
        "title_of_class": "COM",
        "cusip": cusip,
        "figi": None,
        "value": value,
        "shares": shares,
        "sh_prn_type": "SH",
        "put_call": None,
        "investment_discretion": "SOLE",
        "other_managers": None,
        "voting_sole": shares,
        "voting_shared": None,
        "voting_none": None,
    }
    return InfoTableRow(**{**fields, **overrides})


PORTFOLIO = (
    row(cusip=APPLE, value=Decimal(2_040_000_000), shares=Decimal(12_000_000)),
    row(cusip=MICROSOFT, value=Decimal(1_190_000_000), shares=Decimal(3_500_000)),
    row(cusip=COCA_COLA, value=Decimal(1_500_000_000), shares=Decimal(25_000_000)),
)


async def load(
    session: AsyncSession,
    *rows: InfoTableRow,
    doc: PrimaryDoc | None = None,
    accession_no: str = ACCESSION,
    raw_key: str | None = "13f/2024Q1/0001067983-24-000011.xml",
    source_url: str
    | None = "https://www.sec.gov/Archives/edgar/data/1067983/000106798324000011.txt",
) -> LoadResult:
    """One ingest of ``rows``, through the real normalisation path."""
    document = doc if doc is not None else cover()
    return await load_filing(
        session,
        accession_no=accession_no,
        filed_at=FILED_AT,
        primary_doc=document,
        normalised=normalise_filing(
            filed_at=FILED_AT,
            cover=document,
            table=InformationTable(rows=rows, warnings=()),
        ),
        raw_key=raw_key,
        source_url=source_url,
    )


@pytest.fixture
async def berkshire(db_session: AsyncSession) -> Filer:
    """A filer whose CIK is already resolved, which is the ordinary case."""
    filer = Filer(name="Berkshire Hathaway Inc", slug="berkshire-hathaway")
    db_session.add(filer)
    await db_session.flush()
    db_session.add(FilerCik(filer_id=filer.id, cik=BERKSHIRE))
    await db_session.flush()
    return filer


async def count(session: AsyncSession, model: type[Filing] | type[Holding] | type[Security]) -> int:
    return (await session.scalar(select(func.count()).select_from(model))) or 0


# --- the property the whole module exists for --------------------------------


async def test_ingesting_one_filing_three_times_leaves_one_filing_and_one_portfolio(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """The AC, stated as bluntly as it can be.

    Three calls, byte-identical inputs — a redelivered task, a resumed backfill,
    an operator re-running a quarter. If this fails it does not fail loudly: it
    fails as a fund reporting three times what it holds.
    """
    results = [await load(db_session, *PORTFOLIO) for _ in range(3)]

    assert await count(db_session, Filing) == 1
    assert await count(db_session, Holding) == len(PORTFOLIO)
    assert await count(db_session, Security) == len(PORTFOLIO)

    # The same row each time, not three rows one of which survived. Anything
    # holding a filing id across a re-ingest depends on this.
    assert {result.filing_id for result in results} == {results[0].filing_id}
    assert [result.holdings_loaded for result in results] == [3, 3, 3]
    # Only the first call created the securities; the other two found them.
    assert [result.securities_created for result in results] == [3, 0, 0]


async def test_a_position_the_document_no_longer_reports_does_not_survive_a_reload(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """Delete-and-reinsert, as distinct from merge.

    An amendment that restates a period, or a re-parse that reads the document
    differently, drops positions as well as adding them. An upsert keyed on the
    holding's natural key never deletes, so the dropped ones would stay — and
    the filing would report a position it does not contain.
    """
    await load(db_session, *PORTFOLIO)
    await load(db_session, PORTFOLIO[0])

    held = (await db_session.scalars(select(Holding.cusip))).all()
    assert held == [APPLE]


async def test_a_restated_value_replaces_the_old_one_rather_than_joining_it(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    await load(db_session, row(value=Decimal(1_000_000), shares=Decimal(10_000)))
    await load(db_session, row(value=Decimal(2_500_000), shares=Decimal(10_000)))

    holdings = (await db_session.scalars(select(Holding))).all()
    assert len(holdings) == 1
    assert holdings[0].value_usd == Decimal("2500000.00")


async def test_a_reparse_that_finds_nothing_empties_the_filing(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """A 13F NOTICE, or a restatement down to zero positions. The filing stays."""
    await load(db_session, *PORTFOLIO)
    result = await load(db_session)

    assert await count(db_session, Filing) == 1
    assert await count(db_session, Holding) == 0
    assert result.holdings_loaded == 0


# --- the filing row ----------------------------------------------------------


async def test_the_filing_carries_what_the_cover_page_and_the_index_said(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    result = await load(db_session, *PORTFOLIO)

    filing = await db_session.get(Filing, result.filing_id)
    assert filing is not None
    assert filing.accession_no == ACCESSION
    assert filing.cik == BERKSHIRE
    assert filing.filer_id == berkshire.id
    assert filing.form_type == "13F-HR"
    assert filing.period_of_report == PERIOD
    assert filing.filed_at == FILED_AT
    # Filed after the cutover, so the document's values are already dollars.
    assert filing.value_multiplier == 1
    assert filing.parse_status == ParseStatus.OK
    assert filing.parsed_at is not None
    assert filing.ingested_at is not None
    assert filing.raw_key == "13f/2024Q1/0001067983-24-000011.xml"
    # Generated by Postgres from period_of_report, never written by the loader.
    assert filing.quarter == "2024Q1"


async def test_a_second_ingest_updates_the_row_it_conflicts_with(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """The ``DO UPDATE`` half. A re-parse under a fixed parser has to be able to
    change the verdict on a filing that is already in the table — otherwise the
    fix is invisible and the filing stays suspect forever."""
    suspect = await load(db_session, *PORTFOLIO, doc=cover(entry_total=99))

    filing = await db_session.get(Filing, suspect.filing_id)
    assert filing is not None
    assert filing.parse_status == ParseStatus.SUSPECT
    assert filing.parse_notes is not None

    await load(db_session, *PORTFOLIO)
    await db_session.refresh(filing)

    assert filing.parse_status == ParseStatus.OK
    assert filing.parse_notes is None
    assert await count(db_session, Filing) == 1


async def test_a_reingest_does_not_unlink_an_amendment(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """``amends_id`` is set by the step that links an amendment to what it
    amends, which needs both filings to exist and so cannot run inside the
    loader. It is left out of the ``DO UPDATE`` for exactly this reason: the
    insert's NULL would otherwise undo that work on every re-ingest."""
    original = await load(db_session, *PORTFOLIO)
    amendment = await load(
        db_session,
        *PORTFOLIO,
        accession_no="0001067983-24-000012",
        doc=cover(form_type="13F-HR/A"),
    )

    filing = await db_session.get(Filing, amendment.filing_id)
    assert filing is not None
    filing.amends_id = original.filing_id
    filing.amendment_kind = AmendmentKind.RESTATEMENT
    await db_session.flush()

    await load(
        db_session, *PORTFOLIO, accession_no="0001067983-24-000012", doc=cover(form_type="13F-HR/A")
    )
    await db_session.refresh(filing)

    assert filing.amends_id == original.filing_id


async def test_a_reparse_that_knows_no_archive_key_keeps_the_one_on_the_row(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """Re-parsing runs off bytes already on disk and may not know where they
    came from. Blanking the only pointer to the document would make the next
    parser bug a re-crawl of EDGAR rather than a re-read."""
    await load(db_session, *PORTFOLIO)
    result = await load(db_session, *PORTFOLIO, raw_key=None, source_url=None)

    filing = await db_session.get(Filing, result.filing_id)
    assert filing is not None
    assert filing.raw_key == "13f/2024Q1/0001067983-24-000011.xml"
    assert filing.source_url is not None


# --- securities --------------------------------------------------------------


async def test_an_unseen_cusip_gets_an_unresolved_security(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """Unresolved is a normal, permanent state for delisted and obscure names.
    The alternative for a loader that will not invent a ticker is to drop the
    holding, which shrinks a portfolio to protect a nullable column."""
    await load(db_session, *PORTFOLIO)

    security = await db_session.scalar(select(Security).where(Security.cusip == APPLE))
    assert security is not None
    assert security.name == "APPLE INC"
    assert security.ticker is None
    assert security.figi is None
    assert security.resolution_source is None
    assert security.resolved_at is None


async def test_a_reingest_does_not_overwrite_what_enrichment_resolved(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """``DO NOTHING``, not ``DO UPDATE``. By the second load OpenFIGI may have
    filled the ticker in, and the filing's own abbreviated ``nameOfIssuer`` is
    worse evidence than what enrichment found — so the second pass must not
    carry it back over the top."""
    await load(db_session, *PORTFOLIO)

    security = await db_session.scalar(select(Security).where(Security.cusip == APPLE))
    assert security is not None
    security.ticker = "AAPL"
    security.name = "Apple Inc."
    security.resolution_source = "openfigi"
    security.resolved_at = FILED_AT
    await db_session.flush()

    result = await load(db_session, *PORTFOLIO)
    await db_session.refresh(security)

    assert result.securities_created == 0
    assert security.ticker == "AAPL"
    assert security.name == "Apple Inc."


async def test_two_filings_holding_the_same_cusip_share_one_security(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    await load(db_session, row(cusip=APPLE))
    await load(db_session, row(cusip=APPLE), accession_no="0001067983-24-000012")

    assert await count(db_session, Security) == 1
    assert await count(db_session, Filing) == 2
    security_ids = set((await db_session.scalars(select(Holding.security_id))).all())
    assert len(security_ids) == 1


# --- the filer ---------------------------------------------------------------


async def test_holdings_carry_the_filer_and_period_of_the_filing_they_came_from(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """Both are denormalised off ``filing`` and held to it by a composite foreign
    key, so writing either one wrong is an IntegrityError rather than a slow
    divergence."""
    await load(db_session, *PORTFOLIO)

    holdings = (await db_session.scalars(select(Holding))).all()
    assert {holding.filer_id for holding in holdings} == {berkshire.id}
    assert {holding.period_of_report for holding in holdings} == {PERIOD}


async def test_a_filing_from_an_unknown_cik_still_loads(db_session: AsyncSession) -> None:
    """No ``filer_cik`` row for this CIK — the normal state of a filing found
    through the daily index before anything has resolved who filed it. The
    filing is the record that it exists, and losing it because we cannot name
    the filer yet would mean re-crawling to find it again."""
    result = await load(db_session, *PORTFOLIO)

    filing = await db_session.get(Filing, result.filing_id)
    assert filing is not None
    assert filing.filer_id is None
    assert result.filer_id is None
    # holding.filer_id is NOT NULL, so there is nothing to write yet — and the
    # caller is told so rather than left to infer it from a count of zero.
    assert result.holdings_deferred is True
    assert await count(db_session, Holding) == 0


async def test_resolving_the_filer_and_reingesting_finishes_the_job(
    db_session: AsyncSession,
) -> None:
    """The other half of deferral, and the reason it is not data loss: the
    filing is already in the table, so the retry is a re-parse of bytes we hold
    rather than another crawl."""
    first = await load(db_session, *PORTFOLIO)

    filer = Filer(name="Berkshire Hathaway Inc", slug="berkshire-hathaway")
    db_session.add(filer)
    await db_session.flush()
    db_session.add(FilerCik(filer_id=filer.id, cik=BERKSHIRE))
    await db_session.flush()

    second = await load(db_session, *PORTFOLIO)

    assert second.filing_id == first.filing_id
    assert second.filer_id == filer.id
    assert await count(db_session, Filing) == 1
    assert await count(db_session, Holding) == len(PORTFOLIO)


async def test_a_reingest_while_the_cik_is_unresolved_does_not_unlink_the_filer(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """The ``coalesce`` on ``filer_id``. A lookup that misses is not evidence
    that the link is wrong — during a filer merge it is evidence of nothing at
    all — and overwriting on the way past would strand the holdings' NOT NULL
    filer."""
    await load(db_session, *PORTFOLIO)

    await db_session.execute(delete(FilerCik))
    result = await load(db_session, *PORTFOLIO)

    assert result.filer_id == berkshire.id
    assert result.holdings_deferred is False
    assert await count(db_session, Holding) == len(PORTFOLIO)


# --- how it writes -----------------------------------------------------------


@contextmanager
def counting_statements(session: AsyncSession, table: str) -> Iterator[list[int]]:
    """Count cursor executions against ``table``, and the rows in each.

    A test of the *shape* of the write rather than its result, which is unusual
    and is earned here: inserting three thousand holdings one statement at a
    time produces exactly the same table as inserting them in one, and is the
    difference between a backfill that takes a day and one that takes a week.
    Nothing about the rows can tell you which happened.
    """
    bind = session.get_bind()
    connection = bind.sync_connection if hasattr(bind, "sync_connection") else bind
    batches: list[int] = []

    def before_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if f"INSERT INTO {table}" in statement:
            batches.append(len(parameters) if executemany else 1)

    event.listen(connection, "before_cursor_execute", before_cursor_execute)
    try:
        yield batches
    finally:
        event.remove(connection, "before_cursor_execute", before_cursor_execute)


async def test_the_holdings_go_in_as_one_statement_not_one_per_row(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    with counting_statements(db_session, "holding") as batches:
        await load(db_session, *PORTFOLIO)

    assert batches == [len(PORTFOLIO)]


async def test_the_filing_and_its_holdings_arrive_together_or_not_at_all(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """The transaction, tested through a row the database will refuse.

    13F is long-only and ``holding.value_usd >= 0`` says so, so a negative value
    is a sign error in the parser rather than a bearish bet. What matters here
    is not that it is rejected but what is left behind when it is: a filing row
    claiming a successful parse, with no holdings under it, is worse than no row
    — it looks like an empty portfolio to everything downstream.
    """
    with pytest.raises(IntegrityError):
        await load(db_session, row(value=Decimal(-1), shares=Decimal(10)))

    assert await count(db_session, Filing) == 0
    assert await count(db_session, Holding) == 0
    # The savepoint took the securities with it too: nothing from a load that
    # failed is left for the next one to trip over.
    assert await count(db_session, Security) == 0


# --- one position reported across several manager lines ----------------------


async def test_lines_sharing_a_natural_key_become_one_position(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """A filer reporting one holding across two ``otherManager`` lines — normal
    for affiliated managers on a jointly managed book.

    ``holding`` has no ``otherManager`` column and its natural key is
    ``(filing_id, cusip, put_call, sshprnamt_type)``, so the schema has already
    decided these are one position. Inserting both as filed is an
    IntegrityError; keeping the last is a real position silently missing. They
    are summed, which is what keeps the loaded total equal to the filing's own.
    """
    result = await load(
        db_session,
        row(value=Decimal(600_000), shares=Decimal(6_000), other_managers="1"),
        row(value=Decimal(400_000), shares=Decimal(4_000), other_managers="2"),
    )

    holdings = (await db_session.scalars(select(Holding))).all()
    assert len(holdings) == 1
    assert holdings[0].value_usd == Decimal("1000000.00")
    assert holdings[0].shares == Decimal(10_000)
    assert holdings[0].voting_sole == Decimal(10_000)
    assert result.rows_collapsed == 1
    assert result.holdings_loaded == 1


async def test_the_same_cusip_as_stock_and_as_options_stays_three_positions(
    db_session: AsyncSession, berkshire: Filer
) -> None:
    """The other side of the same key: puts, calls and the underlying are
    different positions in one filing and must not be collapsed into each
    other. This is why the natural key is not ``(filing_id, cusip)``."""
    result = await load(
        db_session,
        row(cusip=APPLE),
        row(cusip=APPLE, put_call="Call"),
        row(cusip=APPLE, put_call="Put"),
    )

    assert result.rows_collapsed == 0
    assert await count(db_session, Holding) == 3
    assert await count(db_session, Security) == 1
