"""The insider loader, run again and in the wrong order, against a real Postgres.

Real Form 4s from ``tests/fixtures/form4``, parsed by the real parser. What is
asserted is what the tables hold afterwards: the same rows, with the same ids,
however many times a filing is loaded, and an amended filing's lines gone
whichever of the two loaded first.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Filing,
    Insider,
    InsiderTransaction,
    ReportingOwner,
    SecCompanyTicker,
    Security,
)
from app.ingestion.loaders import AmendmentLink, Form4LoadResult, SecurityLink, load_form4
from app.ingestion.parsers.form4 import Form4Document, parse_form4

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "form4"
NEW_YORK: Final = ZoneInfo("America/New_York")

NVIDIA: Final = "0001045810"
PARKER: Final = "0002152188"
KINETIK: Final = "0001692787"
ALPHABET: Final = "0001652044"

AMENDMENT: Final = "0002152188-26-000007"
ORIGINAL: Final = "0002152188-26-000003"
#: As the filing header says: 13 October 2026, 17:09:29 in New York.
AMENDED_AT: Final = datetime(2026, 10, 13, 17, 9, 29, tzinfo=NEW_YORK)
#: The day the 4/A names as its original's.
ORIGINAL_AT: Final = datetime(2026, 9, 11, 18, 2, 0, tzinfo=NEW_YORK)


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.xml").read_bytes()


def _parsed(name: str) -> Form4Document:
    return parse_form4(_fixture(name))


def _nvidia_original(shares: str = "16041") -> Form4Document:
    """The Form 4 the NVIDIA 4/A corrects: the same grant, with the share count
    the amendment says was wrong."""
    xml = (
        _fixture("amendment-nvidia")
        .replace(b"<documentType>4/A</documentType>", b"<documentType>4</documentType>")
        .replace(b"<dateOfOriginalSubmission>2026-09-11</dateOfOriginalSubmission>", b"")
        .replace(
            b"<value>160418</value>\n                    <footnoteId",
            f"<value>{shares}</value>\n                    <footnoteId".encode(),
        )
    )
    return parse_form4(xml)


async def _load(
    session: AsyncSession,
    document: Form4Document,
    accession_no: str = "0000905148-26-004450",
    filed_at: datetime = AMENDED_AT,
    **options: Any,
) -> Form4LoadResult:
    return await load_form4(
        session,
        accession_no=accession_no,
        filed_at=filed_at,
        document=document,
        raw_key=f"raw/form4/{document.issuer.cik}/{accession_no}/",
        **options,
    )


async def _lines(session: AsyncSession, accession_no: str | None = None) -> list[tuple[Any, ...]]:
    statement = (
        select(
            InsiderTransaction.id,
            Filing.accession_no,
            InsiderTransaction.line_number,
            InsiderTransaction.transaction_date,
            Insider.cik,
            InsiderTransaction.transaction_code,
            InsiderTransaction.shares,
            InsiderTransaction.price_per_share,
            InsiderTransaction.value_usd,
            InsiderTransaction.security_id,
            InsiderTransaction.is_10b5_1,
        )
        .join(Filing, Filing.id == InsiderTransaction.filing_id)
        .join(Insider, Insider.id == InsiderTransaction.insider_id)
        .order_by(Filing.accession_no, InsiderTransaction.line_number)
    )
    if accession_no is not None:
        statement = statement.where(Filing.accession_no == accession_no)
    return [tuple(row) for row in await session.execute(statement)]


async def _owners(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        select(
            Insider.id,
            Insider.cik,
            Insider.name,
            ReportingOwner.is_director,
            ReportingOwner.is_officer,
            ReportingOwner.is_ten_percent_owner,
            ReportingOwner.officer_title,
        )
        .join(ReportingOwner, ReportingOwner.insider_id == Insider.id)
        .order_by(Insider.cik)
    )
    return [tuple(row) for row in rows]


async def _listed(session: AsyncSession, cik: str, *tickers: str) -> dict[str, int]:
    """SEC's map listing ``tickers`` under ``cik``, and a resolved security for
    each, as ``security.ticker`` spells it."""
    ids = {}
    for number, ticker in enumerate(tickers):
        session.add(SecCompanyTicker(ticker=ticker, cik=cik, name=ticker, seen_at=AMENDED_AT))
        security = Security(
            cusip=f"{cik[-6:]}{number:02d}{number}",
            name=ticker,
            ticker=ticker.replace("-", "."),
            resolution_source="openfigi",
            resolved_at=AMENDED_AT,
        )
        session.add(security)
        await session.flush()
        ids[ticker] = security.id
    return ids


# --- idempotency -------------------------------------------------------------------


async def test_the_same_filing_three_times_leaves_the_same_rows(
    db_session: AsyncSession,
) -> None:
    document = _parsed("joint-filing-kinetik")

    first = await _load(db_session, document)
    lines, owners = await _lines(db_session), await _owners(db_session)
    second = await _load(db_session, document)
    third = await _load(db_session, document)

    assert len(lines) == 4
    assert len(owners) == 5
    # The same rows, and the same ids: nothing was deleted and reinserted.
    assert await _lines(db_session) == lines
    assert await _owners(db_session) == owners
    assert (first.lines, first.lines_written) == (4, 4)
    assert (second.lines_written, third.lines_written) == (0, 0)
    assert first.filing_id == second.filing_id == third.filing_id
    filings = await db_session.scalars(select(Filing.accession_no))
    assert list(filings) == ["0000905148-26-004450"]


async def test_the_filing_row_is_a_form_4s(db_session: AsyncSession) -> None:
    await _load(db_session, _parsed("joint-filing-kinetik"))

    filing = await db_session.scalar(select(Filing))
    assert filing is not None
    assert (filing.cik, filing.form_type, filing.filer_id) == (KINETIK, "4", None)
    assert (filing.period_of_report, filing.quarter, filing.value_multiplier) == (None, None, 1)
    assert (filing.parse_status, filing.parse_notes, filing.amends_id) == ("ok", None, None)
    assert filing.filed_at == AMENDED_AT
    assert filing.raw_key == f"raw/form4/{KINETIK}/0000905148-26-004450/"


async def test_a_joint_filings_lines_are_attributed_once_to_the_first_owner(
    db_session: AsyncSession,
) -> None:
    document = _parsed("joint-filing-kinetik")

    result = await _load(db_session, document)

    first = document.owners[0].cik
    assert {row[4] for row in await _lines(db_session)} == {first}
    insider = await db_session.scalar(select(Insider.id).where(Insider.cik == first))
    assert result.insider_id == insider
    assert result.owners == 5
    assert sorted(row[1] for row in await _owners(db_session)) == sorted(
        owner.cik for owner in document.owners
    )


async def test_lines_keep_the_documents_values(db_session: AsyncSession) -> None:
    await _load(db_session, _parsed("exercise-intuitive-rsu"))

    rows = await db_session.execute(
        select(
            InsiderTransaction.line_number,
            InsiderTransaction.is_derivative,
            InsiderTransaction.transaction_code,
            InsiderTransaction.acquired_disposed,
            InsiderTransaction.shares,
            InsiderTransaction.price_per_share,
            InsiderTransaction.value_usd,
            InsiderTransaction.issuer_ticker,
        ).order_by(InsiderTransaction.line_number)
    )
    assert [tuple(row) for row in rows] == [
        (1, False, "M", "A", Decimal("2140.0000"), None, None, "ISRG"),
        (
            2,
            False,
            "F",
            "D",
            Decimal("1062.0000"),
            Decimal("423.96000000"),
            Decimal("450245.52"),
            "ISRG",
        ),
        # The RSUs themselves: their own count, and nothing paid for them.
        (3, True, "M", "D", Decimal("2140.0000"), None, None, "ISRG"),
    ]


async def test_footnotes_are_an_object_or_null(db_session: AsyncSession) -> None:
    await _load(db_session, _parsed("sale-everpure-director"), "0001593803-26-000006")
    await _load(db_session, _parsed("plan-sale-liquidia"), "0001104659-26-115641")

    rows = await db_session.execute(
        select(Filing.accession_no, InsiderTransaction.line_number, InsiderTransaction.footnotes)
        .join(Filing, Filing.id == InsiderTransaction.filing_id)
        .order_by(Filing.accession_no, InsiderTransaction.line_number)
    )
    by_line = {(accession, line): notes for accession, line, notes in rows}
    assert by_line[("0001593803-26-000006", 1)] is None
    sale = by_line[("0001104659-26-115641", 2)]
    assert isinstance(sale, dict) and any("10b5-1" in note for note in sale.values())


async def test_replace_writes_what_a_reparse_now_reads(db_session: AsyncSession) -> None:
    document = _parsed("purchase-mastercraft-ceo")
    await _load(db_session, document)
    before = await _lines(db_session)
    corrected = document.model_copy(
        update={
            "transactions": (
                document.transactions[0].model_copy(update={"shares": Decimal("2500")}),
            )
        }
    )

    await _load(db_session, corrected)
    assert await _lines(db_session) == before

    result = await _load(db_session, corrected, replace=True)
    after = await _lines(db_session)
    assert result.lines_written == 1
    assert [(row[2], row[6]) for row in after] == [(1, Decimal("2500.0000"))]


# --- insiders ----------------------------------------------------------------------


async def test_an_insider_is_one_row_per_cik_with_the_latest_filed_spelling(
    db_session: AsyncSession,
) -> None:
    amendment = _parsed("amendment-nvidia")
    renamed = _nvidia_original().model_copy(
        update={"owners": (amendment.owners[0].model_copy(update={"name": "PARKER NICHOLAS"}),)}
    )

    # The later filing first, then the earlier one with an older spelling.
    await _load(db_session, amendment, AMENDMENT, AMENDED_AT)
    await _load(db_session, renamed, ORIGINAL, ORIGINAL_AT)

    rows = await db_session.execute(select(Insider.cik, Insider.name))
    assert [tuple(row) for row in rows] == [(PARKER, "Parker Nicholas P.")]

    # And a later spelling does replace it.
    await _load(
        db_session, renamed, "0002152188-26-000009", datetime(2026, 10, 14, tzinfo=NEW_YORK)
    )
    rows = await db_session.execute(select(Insider.cik, Insider.name))
    assert [tuple(row) for row in rows] == [(PARKER, "PARKER NICHOLAS")]


# --- the security --------------------------------------------------------------------


async def test_the_security_is_linked_through_the_issuer_cik(db_session: AsyncSession) -> None:
    ids = await _listed(db_session, KINETIK, "KNTK")

    result = await _load(db_session, _parsed("joint-filing-kinetik"))

    assert (result.security_id, result.security_link) == (ids["KNTK"], SecurityLink.LINKED)
    assert {row[9] for row in await _lines(db_session)} == {ids["KNTK"]}


async def test_an_issuer_sec_does_not_list_loads_unlinked(db_session: AsyncSession) -> None:
    result = await _load(db_session, _parsed("joint-filing-kinetik"))

    assert (result.security_id, result.security_link) == (None, SecurityLink.UNLISTED)
    assert result.lines_written == 4
    assert {row[9] for row in await _lines(db_session)} == {None}


async def test_a_listed_issuer_no_security_answers_to_loads_unlinked(
    db_session: AsyncSession,
) -> None:
    db_session.add(SecCompanyTicker(ticker="KNTK", cik=KINETIK, name="Kinetik", seen_at=AMENDED_AT))
    await db_session.flush()

    result = await _load(db_session, _parsed("joint-filing-kinetik"))

    assert (result.security_id, result.security_link) == (None, SecurityLink.NO_SECURITY)


async def test_the_filings_symbol_chooses_between_an_issuers_classes(
    db_session: AsyncSession,
) -> None:
    ids = await _listed(db_session, ALPHABET, "GOOG", "GOOGL")
    document = _parsed("joint-filing-kinetik")
    issuer = document.issuer.model_copy(update={"cik": ALPHABET, "trading_symbol": "GOOGL"})

    result = await _load(db_session, document.model_copy(update={"issuer": issuer}))

    assert (result.security_id, result.security_link) == (ids["GOOGL"], SecurityLink.LINKED)


async def test_classes_the_symbol_does_not_choose_between_leave_it_unlinked(
    db_session: AsyncSession,
) -> None:
    await _listed(db_session, ALPHABET, "GOOG", "GOOGL")
    document = _parsed("joint-filing-kinetik")
    issuer = document.issuer.model_copy(update={"cik": ALPHABET, "trading_symbol": None})

    result = await _load(db_session, document.model_copy(update={"issuer": issuer}))

    assert (result.security_id, result.security_link) == (None, SecurityLink.AMBIGUOUS)


async def test_a_class_ticker_matches_sec_and_security_spellings(db_session: AsyncSession) -> None:
    """SEC writes ``BRK-B``, ``security`` writes ``BRK.B``, filers write either."""
    ids = await _listed(db_session, "0001067983", "BRK-A", "BRK-B")
    document = _parsed("joint-filing-kinetik")
    issuer = document.issuer.model_copy(update={"cik": "0001067983", "trading_symbol": "BRK.B"})

    result = await _load(db_session, document.model_copy(update={"issuer": issuer}))

    assert (result.security_id, result.security_link) == (ids["BRK-B"], SecurityLink.LINKED)


# --- amendments ----------------------------------------------------------------------


async def test_a_4a_supersedes_the_original_loaded_before_it(db_session: AsyncSession) -> None:
    original = await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT)
    assert original.lines_written == 1

    amendment = await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)

    assert (amendment.amendment, amendment.amends_id) == (AmendmentLink.LINKED, original.filing_id)
    assert (amendment.lines_written, amendment.lines_superseded) == (1, 1)
    assert [(row[1], row[6]) for row in await _lines(db_session)] == [
        (AMENDMENT, Decimal("160418.0000"))
    ]
    stored = await db_session.scalar(
        select(Filing.date_of_original_submission).where(Filing.accession_no == AMENDMENT)
    )
    assert stored == date(2026, 9, 11)


async def test_an_original_loaded_after_its_4a_is_superseded_at_once(
    db_session: AsyncSession,
) -> None:
    amendment = await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)
    assert amendment.amendment is AmendmentLink.NOT_LOADED

    original = await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT)

    assert (original.superseded_by, original.lines_written) == (amendment.filing_id, 0)
    assert [(row[1], row[6]) for row in await _lines(db_session)] == [
        (AMENDMENT, Decimal("160418.0000"))
    ]
    linked = await db_session.scalar(
        select(Filing.amends_id).where(Filing.id == amendment.filing_id)
    )
    assert linked == original.filing_id


async def test_reloading_a_superseded_original_does_not_bring_its_lines_back(
    db_session: AsyncSession,
) -> None:
    await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT)
    await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)
    lines = await _lines(db_session)

    for replace in (False, True, False):
        await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT, replace=replace)
        await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)

    assert await _lines(db_session) == lines


async def test_an_amendment_of_an_amendment_leaves_only_the_latest(
    db_session: AsyncSession,
) -> None:
    await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT)
    await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)
    second = _parsed("amendment-nvidia").model_copy(
        update={"date_of_original_submission": date(2026, 10, 13)}
    )

    result = await _load(
        db_session, second, "0002152188-26-000011", datetime(2026, 10, 14, 9, tzinfo=NEW_YORK)
    )

    assert result.amendment is AmendmentLink.LINKED
    assert {row[1] for row in await _lines(db_session)} == {"0002152188-26-000011"}


async def test_a_4a_whose_original_is_not_loaded_keeps_its_lines(
    db_session: AsyncSession,
) -> None:
    # Another of Parker's Form 4s, filed on another day.
    await _load(
        db_session, _nvidia_original(), ORIGINAL, datetime(2026, 9, 10, 18, tzinfo=NEW_YORK)
    )

    result = await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)

    assert (result.amendment, result.amends_id) == (AmendmentLink.NOT_LOADED, None)
    assert len(await _lines(db_session)) == 2


async def test_two_originals_the_4a_could_amend_are_left_alone(db_session: AsyncSession) -> None:
    await _load(db_session, _nvidia_original(), ORIGINAL, ORIGINAL_AT)
    await _load(
        db_session,
        _nvidia_original("17000"),
        "0002152188-26-000004",
        ORIGINAL_AT.replace(hour=19),
    )

    result = await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)

    assert (result.amendment, result.amends_id) == (AmendmentLink.AMBIGUOUS, None)
    assert len(await _lines(db_session)) == 3


async def test_a_4a_does_not_amend_another_insiders_form_4(db_session: AsyncSession) -> None:
    original = _nvidia_original()
    someone_else = original.model_copy(
        update={
            "owners": (
                original.owners[0].model_copy(update={"cik": "0001197647", "name": "HUANG"}),
            )
        }
    )
    await _load(db_session, someone_else, ORIGINAL, ORIGINAL_AT)

    result = await _load(db_session, _parsed("amendment-nvidia"), AMENDMENT, AMENDED_AT)

    assert result.amendment is AmendmentLink.NOT_LOADED
    assert len(await _lines(db_session)) == 2
