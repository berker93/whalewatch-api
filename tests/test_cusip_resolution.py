"""The resolution policy, without a database: which listing, which source, which name.

The shapes below are cut down from real OpenFIGI answers of 2026-10-12. Apple
has a ``US`` composite among hundreds of venue rows. Hess has no composite and
one US venue. Exxon's own CUSIP has listings only abroad. A closed-end fund
comes back under its trading ticker and its NAV symbol. An Albemarle preferred
comes back with a bond-style ticker.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import pytest

from app.core.tickers import normalise_ticker
from app.db.models.sec_13f_security import ListStatus
from app.db.models.security import ResolutionSource
from app.ingestion.enrichment.figi import FIGIResult, id_type
from app.ingestion.enrichment.issuer_names import ListedName
from app.ingestion.enrichment.resolution import (
    Candidate,
    Listing,
    choose_listing,
    resolve,
)

ASKED_AT: Final = datetime(2026, 10, 12, tzinfo=UTC)


def _row(ticker: str, exchange: str, figi: str, **fields: Any) -> dict[str, Any]:
    return {
        "figi": figi,
        "ticker": ticker,
        "exchCode": exchange,
        "marketSector": "Equity",
        "securityType": "Common Stock",
        "compositeFIGI": figi,
        "name": "SOME ISSUER",
        **fields,
    }


def _found(cusip: str, *rows: dict[str, Any]) -> FIGIResult:
    return FIGIResult.from_response(cusip, {"data": list(rows)}, ASKED_AT)


APPLE: Final = _found(
    "037833100",
    _row("AAPL", "UA", "BBG000B9XSK7", compositeFIGI="BBG000B9XRY4", name="APPLE INC"),
    _row("AAPL", "US", "BBG000B9XRY4", name="APPLE INC"),
    _row("APC", "GR", "BBG000BCTY61", name="APPLE INC"),
)
HESS: Final = _found(
    "42809H107",
    _row("HESUSD", "XB", "BBG00JX0Z1P5", name="HESS CORP"),
    _row("HES", "UA", "BBG000BBD079", compositeFIGI="BBG000BBD070", name="HESS CORP"),
)
EXXON: Final = _found(
    "30231G102",
    _row("XOM", "UZ", "BBG00XOMUZ01", name="EXXON MOBIL CORP"),
    _row("EXMOC", "CP", "BBG00B8TC1V3", name="EXXON MOBIL CORP"),
)
FUND: Final = _found(
    "09262F100",
    _row("ECAT", "US", "BBG012XYZ001", securityType="Closed-End Fund"),
    _row("XESGX", "US", "BBG012XYZ002", securityType="Closed-End Fund"),
)
PREFERRED: Final = _found(
    "012653200",
    _row("ALB 7.25 03/01/27", "NEW YORK", "BBG01LV4WBY2", marketSector="Pfd"),
)
BERKSHIRE_B: Final = _found(
    "084670702", _row("BRK/B", "US", "BBG000DWG505", name="BERKSHIRE HATHAWAY INC-CL B")
)
MISSING: Final = FIGIResult.from_response(
    "999999999", {"warning": "No identifier found."}, ASKED_AT
)


# --- choosing a listing -----------------------------------------------------------


def test_the_us_composite_is_taken_among_every_venue() -> None:
    assert choose_listing(APPLE) == Listing("AAPL", "BBG000B9XRY4")


def test_without_a_composite_a_us_venue_gives_the_ticker_and_its_composite_figi() -> None:
    assert choose_listing(HESS) == Listing("HES", "BBG000BBD070")


def test_listings_only_abroad_are_no_listing() -> None:
    """``UZ`` starts with a U and is not a US venue."""
    assert choose_listing(EXXON) is None


def test_a_funds_nav_symbol_gives_way_to_its_trading_ticker() -> None:
    assert choose_listing(FUND) == Listing("ECAT", "BBG012XYZ001")


def test_two_trading_tickers_are_no_listing() -> None:
    two = _found(
        "60365F109",
        _row("MMED", "US", "BBG000000001"),
        _row("MMEDV", "US", "BBG000000002"),
    )
    assert choose_listing(two) is None


def test_us_venues_that_disagree_without_a_composite_are_no_listing() -> None:
    two = _found(
        "60365F109",
        _row("ABC", "UN", "BBG000000001"),
        _row("ABD", "UQ", "BBG000000002"),
    )
    assert choose_listing(two) is None


def test_a_listing_outside_equity_is_no_listing() -> None:
    """A preferred's "ticker" is a description, not something to look a stock up by."""
    assert choose_listing(PREFERRED) is None


def test_a_row_without_a_ticker_is_passed_over() -> None:
    rows = _found("037833100", _row("", "US", "BBG000000001"), _row("AAPL", "UN", "BBG000B9XVV8"))
    assert choose_listing(rows) == Listing("AAPL", "BBG000B9XVV8")


@pytest.mark.parametrize("result", [MISSING, None])
def test_a_miss_or_no_answer_is_no_listing(result: FIGIResult | None) -> None:
    assert choose_listing(result) is None


def test_a_share_class_is_stored_with_a_dot() -> None:
    assert choose_listing(BERKSHIRE_B) == Listing("BRK.B", "BBG000DWG505")


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("brk/b", "BRK.B"), (" aapl ", "AAPL"), ("BF.B", "BF.B"), ("BRK-B", "BRK.B")],
)
def test_normalise_ticker(raw: str, stored: str) -> None:
    assert normalise_ticker(raw) == stored


@pytest.mark.parametrize(
    ("cusip", "kind"),
    [("037833100", "ID_CUSIP"), ("G54950103", "ID_CINS"), ("N07059210", "ID_CINS")],
)
def test_a_cins_is_asked_for_as_one(cusip: str, kind: str) -> None:
    assert id_type(cusip) == kind


# --- the order of sources -----------------------------------------------------------


def _candidate(cusip: str, filed_name: str | None = "AS FILED") -> Candidate:
    return Candidate(1, cusip, filed_name, Decimal(1000))


def _listed(cusip: str, name: str) -> dict[str, ListedName]:
    return {cusip: ListedName(cusip, name, date(2026, 6, 30), ListStatus.LISTED)}


def test_openfigi_wins_with_its_ticker_figi_and_name() -> None:
    resolution = resolve(_candidate("037833100"), APPLE, _listed("037833100", "APPLE INC LIST"))

    assert resolution.source is ResolutionSource.OPENFIGI
    assert (resolution.ticker, resolution.figi, resolution.name) == (
        "AAPL",
        "BBG000B9XRY4",
        "APPLE INC",
    )


def test_the_list_names_what_openfigi_could_not_tickers() -> None:
    """Exxon's foreign listings name it too, but only alongside a ticker does
    OpenFIGI's name win."""
    resolution = resolve(_candidate("30231G102"), EXXON, _listed("30231G102", "EXXON MOBIL CORP"))

    assert resolution.source is ResolutionSource.SEC_13F_LIST
    assert (resolution.ticker, resolution.figi, resolution.name) == (
        None,
        None,
        "EXXON MOBIL CORP",
    )


def test_with_neither_the_filed_name_stays_and_nothing_won() -> None:
    resolution = resolve(_candidate("999999999", "ACME CORP - converted at IPO"), MISSING, {})

    assert resolution.source is None
    assert (resolution.ticker, resolution.name) == (None, "ACME CORP - converted at IPO")


def test_a_ticker_openfigi_no_longer_lists_is_kept() -> None:
    """A delisting looks like this from OpenFIGI. The ticker is history."""
    candidate = Candidate(1, "00507V109", "ACTIVISION", Decimal(1000), ticker="ATVI")

    resolution = resolve(candidate, MISSING, _listed("00507V109", "ACTIVISION BLIZZARD INC"))

    assert resolution.kept
    assert resolution.ticker == "ATVI"
