"""``GET /filings/{accession_no}``, through the app, against a real Postgres.

Seeded through the real loader rather than by inserting rows, because the thing
worth proving here is that the loop closes: what ingestion writes is what the
endpoint reads back, in the units ingestion decided on. A test that inserted its
own ``holding`` rows would agree with itself about the multiplier and never
notice if the two halves disagreed.

The interesting assertions are all about *fidelity* — the ordering, the
serialisation of ``numeric``, and the columns that record what normalisation
did. A number that survives the database and is then rounded on its way out of
the API is wrong in exactly the way this endpoint exists to detect.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AmendmentKind, Filer, FilerCik, Filing
from app.ingestion.loaders import load_filing
from app.ingestion.normalisation import normalise_filing
from app.ingestion.parsers.thirteen_f import InformationTable, InfoTableRow, PrimaryDoc

ACCESSION = "0001067983-24-000011"
UNDASHED = "000106798324000011"
BERKSHIRE = "0001067983"

PERIOD = date(2024, 3, 31)
#: After the 2023-01-03 cutover, so the multiplier is 1 and the values asserted
#: below are the values in the fixtures.
FILED_AT = datetime(2024, 5, 15, 16, 30, tzinfo=UTC)

APPLE = "037833100"
MICROSOFT = "594918104"
COCA_COLA = "191216100"
AMEX = "025816109"


def cover(
    *,
    entry_total: int | None = None,
    form_type: str = "13F-HR",
    period: date = PERIOD,
    cik: str = BERKSHIRE,
    **overrides: Any,
) -> PrimaryDoc:
    fields: dict[str, Any] = {
        "cik": cik,
        "filer_name": "Berkshire Hathaway Inc",
        "form_type": form_type,
        "period_of_report": period,
        "signature_date": None,
        "amendment_no": None,
        "amendment_kind": None,
        "report_type": "13F HOLDINGS REPORT",
        "table_entry_total": entry_total,
        "table_value_total": None,
        "other_included_managers_count": None,
        "confidential_omitted": False,
    }
    return PrimaryDoc(**{**fields, **overrides})


def row(
    *,
    cusip: str = APPLE,
    name: str = "APPLE INC",
    value: Decimal = Decimal(1_000_000),
    shares: Decimal = Decimal(10_000),
    **overrides: Any,
) -> InfoTableRow:
    fields: dict[str, Any] = {
        "name_of_issuer": name,
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


#: Four positions, two of them tied on value, so the tie-break is exercised by
#: the ordinary case rather than by a test nobody runs.
PORTFOLIO = (
    row(cusip=APPLE, name="APPLE INC", value=Decimal(2_040_000_000), shares=Decimal(12_000_000)),
    row(
        cusip=MICROSOFT,
        name="MICROSOFT CORP",
        value=Decimal(1_190_000_000),
        shares=Decimal(3_500_000),
    ),
    row(
        cusip=COCA_COLA,
        name="COCA COLA CO",
        value=Decimal(1_500_000_000),
        shares=Decimal(25_000_000),
    ),
    row(
        cusip=AMEX,
        name="AMERICAN EXPRESS CO",
        value=Decimal(1_500_000_000),
        shares=Decimal(30_000_000),
    ),
)

CALL_ON_APPLE = row(
    cusip=APPLE,
    name="APPLE INC",
    value=Decimal(100_000_000),
    shares=Decimal(500_000),
    put_call="Call",
)


async def ingest(
    session: AsyncSession,
    *rows: InfoTableRow,
    doc: PrimaryDoc | None = None,
    accession_no: str = ACCESSION,
    filed_at: datetime = FILED_AT,
) -> None:
    """One filing, loaded the way the CLI loads it."""
    document = doc if doc is not None else cover()
    await load_filing(
        session,
        accession_no=accession_no,
        filed_at=filed_at,
        primary_doc=document,
        normalised=normalise_filing(
            filed_at=filed_at,
            cover=document,
            table=InformationTable(rows=rows, warnings=()),
        ),
        raw_key=f"13f/2024Q1/{accession_no}.xml",
        source_url=f"https://www.sec.gov/Archives/edgar/data/1067983/{UNDASHED}.txt",
    )
    await session.flush()


@pytest.fixture
async def berkshire(db_session: AsyncSession) -> Filer:
    """A filer whose CIK is already resolved, which is the ordinary case."""
    filer = Filer(name="Berkshire Hathaway Inc", slug="berkshire-hathaway")
    db_session.add(filer)
    await db_session.flush()
    db_session.add(FilerCik(filer_id=filer.id, cik=BERKSHIRE))
    await db_session.flush()
    return filer


# --- the ordinary read -------------------------------------------------------


async def test_a_filing_comes_back_with_its_metadata_and_its_holdings(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    await ingest(db_session, *PORTFOLIO)

    response = await client.get(f"/filings/{ACCESSION}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["accession_no"] == ACCESSION
    assert payload["cik"] == BERKSHIRE
    assert payload["form_type"] == "13F-HR"
    assert payload["period_of_report"] == "2024-03-31"
    # Generated by Postgres from the period; it has to survive the round trip
    # because it is what every aggregate in the API will group on.
    assert payload["quarter"] == "2024Q1"
    assert payload["filer_id"] == berkshire.id
    assert payload["filer_name"] == "Berkshire Hathaway Inc"
    assert payload["filer_slug"] == "berkshire-hathaway"
    assert payload["source_url"].endswith(f"{UNDASHED}.txt")
    assert len(payload["holdings"]) == len(PORTFOLIO)


async def test_holdings_come_back_largest_first_and_in_a_stable_order(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """Value descending, ties broken by CUSIP.

    The tie-break is the half that matters. Two positions at the same value is
    not a contrived case — round numbers and equal-weighted positions collide —
    and without a deterministic order the same request returns two different
    documents, which makes every diff of this endpoint's output noise.
    """
    await ingest(db_session, *PORTFOLIO)

    payload = (await client.get(f"/filings/{ACCESSION}")).json()

    assert [holding["cusip"] for holding in payload["holdings"]] == [
        APPLE,  # 2.04bn
        AMEX,  # 1.5bn, and sorts before Coca-Cola on CUSIP
        COCA_COLA,  # 1.5bn
        MICROSOFT,  # 1.19bn
    ]
    values = [Decimal(holding["value_usd"]) for holding in payload["holdings"]]
    assert values == sorted(values, reverse=True)


async def test_a_holding_carries_both_the_filed_cusip_and_what_we_resolved_it_to(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """An unresolved security is null, not absent, and never a 500.

    Nothing has run OpenFIGI against these CUSIPs, which is the normal state of
    a freshly ingested filing — so ``ticker`` is null while ``issuer_name`` is
    the name the filing itself printed, carried into ``security`` by the loader.
    """
    await ingest(db_session, PORTFOLIO[0])

    holding = (await client.get(f"/filings/{ACCESSION}")).json()["holdings"][0]

    assert holding["cusip"] == APPLE
    assert holding["issuer_name"] == "APPLE INC"
    assert holding["ticker"] is None
    assert holding["sshprnamt_type"] == "SH"
    assert holding["investment_discretion"] == "SOLE"
    assert holding["put_call"] is None


# --- the serialisation the ticket exists for ---------------------------------


async def test_money_and_quantities_are_json_strings_not_numbers(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """Every ``numeric`` leaves as a string, with its scale intact.

    A JSON number here would be an IEEE 754 double at the far end of every
    client, and the loss is not hypothetical: ``value_usd`` is ``numeric(20,2)``
    and ``shares`` is ``numeric(20,4)``, both of which carry more significant
    digits than a double has. The trailing zeros are part of the assertion —
    they are the column's scale, and a float cannot represent the difference
    between ``1000`` and ``1000.00`` at all.
    """
    await ingest(db_session, PORTFOLIO[0])

    holding = (await client.get(f"/filings/{ACCESSION}")).json()["holdings"][0]

    assert holding["value_usd"] == "2040000000.00"
    assert holding["shares"] == "12000000.0000"
    assert holding["voting_sole"] == "12000000.0000"
    assert all(isinstance(holding[field], str) for field in ("value_usd", "shares", "voting_sole"))


async def test_a_quantity_a_double_cannot_hold_survives_the_round_trip(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """2^53 + 1: the smallest integer a double cannot represent.

    Deliberately extreme, because it is provable — ``float`` silently returns
    2^53 for it. Ordinary nine-figure share counts lose their digits further
    down rather than at the units, which is precisely why this is worth pinning
    with a value that makes the loss impossible to argue with.
    """
    enormous = Decimal("9007199254740993")
    await ingest(
        db_session,
        row(shares=enormous, value=enormous * 10, voting_sole=enormous),
    )

    holding = (await client.get(f"/filings/{ACCESSION}")).json()["holdings"][0]

    assert Decimal(holding["shares"]) == enormous
    # The assertion above passes for free if this line is ever wrong about
    # floats, so state the premise rather than trusting it.
    assert Decimal(float(enormous)) != enormous


# --- the two spellings -------------------------------------------------------


async def test_both_accession_spellings_reach_the_same_filing(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """Dashed as EDGAR's indexes print it, undashed as archive URLs do.

    The column is ``CHAR(20)`` holding the dashed form, so the undashed spelling
    matches nothing unless it is normalised first — and the failure mode is a
    404 that says the filing has not been ingested when it is sitting in the
    table.
    """
    await ingest(db_session, *PORTFOLIO)

    dashed = await client.get(f"/filings/{ACCESSION}")
    undashed = await client.get(f"/filings/{UNDASHED}")

    assert undashed.status_code == 200
    assert undashed.json() == dashed.json()
    # Normalised, so the response says the accession number the database holds
    # rather than echoing whichever spelling was asked for.
    assert undashed.json()["accession_no"] == ACCESSION


@pytest.mark.parametrize(
    "argument", ["not-an-accession", "12345", "0001067983-24-00001X", "0001067983-24-0000110"]
)
async def test_a_malformed_accession_number_is_rejected_before_any_lookup(
    client: AsyncClient, argument: str
) -> None:
    """422, not 404. The two answers send you to different places: one says the
    filing needs ingesting, the other that the URL is wrong."""
    response = await client.get(f"/filings/{argument}")

    assert response.status_code == 422
    assert "accession number" in response.text


# --- the filing that is not there --------------------------------------------


async def test_an_unknown_accession_number_is_404_and_says_what_to_do(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The message names the command that would fix it.

    "Not found" on this endpoint almost always means "not ingested yet", and the
    reader is usually the person who can ingest it.
    """
    response = await client.get("/filings/0000000000-99-000001")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "0000000000-99-000001" in detail
    assert "ingest-filing" in detail


# --- options -----------------------------------------------------------------


async def test_option_lines_are_included_by_default(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    await ingest(db_session, PORTFOLIO[0], CALL_ON_APPLE)

    payload = (await client.get(f"/filings/{ACCESSION}")).json()

    assert [holding["put_call"] for holding in payload["holdings"]] == [None, "Call"]


async def test_include_options_false_drops_the_option_lines(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """The filter is on ``put_call IS NOT NULL``, not on the CUSIP.

    An option on a name that is also held outright shares its CUSIP, so a filter
    that worked by security would take the underlying position with it — which
    is the case this fixture is built to catch.
    """
    await ingest(db_session, PORTFOLIO[0], CALL_ON_APPLE)

    payload = (await client.get(f"/filings/{ACCESSION}?include_options=false")).json()

    assert [holding["cusip"] for holding in payload["holdings"]] == [APPLE]
    assert payload["holdings"][0]["put_call"] is None


# --- the normalisation decisions ---------------------------------------------


async def test_the_response_shows_which_units_the_filing_used(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """``value_multiplier`` next to the values it was applied to.

    A pre-cutover filing reports thousands; the loader stores whole dollars. The
    endpoint has to show both facts, because a portfolio that is out by 1000x
    looks entirely normal — every position is wrong by the same factor — and
    this field is the only thing that distinguishes "the filing said thousands"
    from "we multiplied when we should not have".
    """
    filed_at = datetime(2022, 5, 16, 16, 30, tzinfo=UTC)
    await ingest(
        db_session,
        row(value=Decimal(2_040_000), shares=Decimal(12_000_000)),
        doc=cover(period=date(2022, 3, 31)),
        filed_at=filed_at,
    )

    payload = (await client.get(f"/filings/{ACCESSION}")).json()

    assert payload["value_multiplier"] == 1000
    # Thousands in the document, whole dollars in the response.
    assert payload["holdings"][0]["value_usd"] == "2040000000.00"


async def test_a_suspect_filing_reads_as_suspect_and_says_why(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """Loaded, returned, and flagged — all three.

    A filing whose cover page disagrees with its rows is not withheld: 99% of a
    portfolio is worth more than an error page. What this endpoint owes the
    reader is the flag and the finding alongside the holdings, so the decision
    to trust it is theirs.
    """
    await ingest(db_session, *PORTFOLIO, doc=cover(entry_total=99))

    payload = (await client.get(f"/filings/{ACCESSION}")).json()

    assert payload["parse_status"] == "suspect"
    assert payload["parse_error"] is None
    assert [note["kind"] for note in payload["parse_notes"]] == ["entry_count"]
    assert payload["parse_notes"][0]["expected"] == "99"
    # Flagged, not withheld.
    assert len(payload["holdings"]) == len(PORTFOLIO)


async def test_a_clean_filing_has_a_status_of_ok_and_no_notes(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    await ingest(db_session, *PORTFOLIO, doc=cover(entry_total=len(PORTFOLIO)))

    payload = (await client.get(f"/filings/{ACCESSION}")).json()

    assert payload["parse_status"] == "ok"
    assert payload["parse_notes"] is None
    assert payload["parsed_at"] is not None


# --- the rows that are not the happy path ------------------------------------


async def test_a_filing_whose_cik_is_not_resolved_yet_still_reads(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """No ``berkshire`` fixture, so nothing links this CIK to a filer.

    The filing loads without holdings — ``holding.filer_id`` is NOT NULL, so
    they wait for the resolution — and this endpoint is how you find out that is
    what happened. An inner join to ``filer`` would answer 404 instead, which
    reads as "never ingested" and sends the reader to re-run an ingest that
    already worked.
    """
    await ingest(db_session, *PORTFOLIO)

    response = await client.get(f"/filings/{ACCESSION}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["filer_id"] is None
    assert payload["filer_name"] is None
    assert payload["holdings"] == []


async def test_an_amendment_names_the_filing_it_amends(
    client: AsyncClient, db_session: AsyncSession, berkshire: Filer
) -> None:
    """``amends_id`` is a link nothing sets during ingest yet, so it is set here.

    Worth an assertion anyway: the endpoint resolves it to an accession number,
    and an id nobody can look up would make the amendment chain unreadable from
    outside the database.
    """
    amendment = "0001067983-24-000012"
    await ingest(db_session, *PORTFOLIO)
    await ingest(
        db_session,
        PORTFOLIO[0],
        accession_no=amendment,
        doc=cover(
            form_type="13F-HR/A",
            amendment_no=1,
            amendment_kind=AmendmentKind.RESTATEMENT,
        ),
    )
    original_id = await db_session.scalar(select(Filing.id).where(Filing.accession_no == ACCESSION))
    await db_session.execute(
        update(Filing).where(Filing.accession_no == amendment).values(amends_id=original_id)
    )

    payload = (await client.get(f"/filings/{amendment}")).json()

    assert payload["form_type"] == "13F-HR/A"
    assert payload["amendment_kind"] == AmendmentKind.RESTATEMENT.value
    assert payload["amends_accession_no"] == ACCESSION
