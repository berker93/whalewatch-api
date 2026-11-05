"""CON-6, pure: what a PTR line's asset text says, before any lookup.

Every string here is one a 2026 House PTR filed, as the readers left it: the
e-filed form's ``name (TICKER) [CODE]`` split into its parts, a scan's line as
OCR read it, with no code and the ticker still in the name.
"""

import pytest

from app.ingestion.congress.asset_resolution import share_class
from app.ingestion.congress.assets import (
    AssetClass,
    Ticker,
    asset_key,
    classify,
    core_name,
    extract_ticker,
    name_similarity,
)

# --- the class ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("GS", "US Treasury Note 3.375% due 09/15/28 CUSIP 91282CNY3", AssetClass.TREASURY),
        ("GS", "U.S Treasury Bills", AssetClass.TREASURY),
        ("GS", "UNITED STATES TREAS SER AK- 2028; 4.25%; Due 02/15/2028", AssetClass.TREASURY),
        ("GS", "US TSY NOTE 02/15/34", AssetClass.TREASURY),
        ("GS", "Washington ST 5% Go Utx Due 08/01/30", AssetClass.MUNICIPAL_BOND),
        ("GS", "Los Angeles CNTY EXCISE", AssetClass.MUNICIPAL_BOND),
        ("GS", "FHLMC 30 YR GOLD RQ0128", AssetClass.AGENCY_BOND),
        ("GS", "Fannie Mae Note 9/24/26", AssetClass.AGENCY_BOND),
        ("GS", "American Expr Natl Bank CD", AssetClass.OTHER),
        ("CS", "HCA INC. NOTE CALL MAKE WHOLE 5.62500% 09/01/2028", AssetClass.CORPORATE_BOND),
        ("CS", "Microsoft Corp", AssetClass.CORPORATE_BOND),
        ("CS", "FEDERAL NATIONAL MORTGAGE ASSOCIATION POOL MA6054", AssetClass.AGENCY_BOND),
        ("MF", "Vanguard Wellington Fund Admiral", AssetClass.MUTUAL_FUND),
        ("CT", "Bitcoin (CRYPTO:BTC)", AssetClass.CRYPTO),
        ("RP", "Rental property, Austin TX", AssetClass.REAL_ESTATE),
        ("OI", "Saronic Technologies", AssetClass.PRIVATE),
        ("HN", "Alpine Investors IX, LP (GLAS Funds, LP)", AssetClass.PRIVATE),
        ("PS", "Andersen Corporation", AssetClass.PRIVATE),
        ("VA", "Jackson National Variable Annuity", AssetClass.OTHER),
        ("ZZ", "A code the form has never had", AssetClass.OTHER),
    ],
)
def test_a_code_with_no_security_is_classified_and_looked_up_no_further(
    code: str, name: str, expected: AssetClass
) -> None:
    found = classify(code, name)

    assert (found.asset_class, found.lookup) == (expected, False)


def test_a_stock_is_looked_up_and_stays_a_stock_unresolved() -> None:
    found = classify("ST", "Alphabet Inc. - Class A Common Stock", "GOOGL")

    assert (found.asset_class, found.otherwise, found.lookup, found.by_name) == (
        AssetClass.EQUITY,
        AssetClass.EQUITY,
        True,
        True,
    )


def test_an_option_is_looked_up_for_its_underlying() -> None:
    found = classify("OP", "Microsoft Corporation - Common Stock", "MSFT")

    assert (found.asset_class, found.lookup) == (AssetClass.STOCK_OPTION, True)


def test_a_reit_is_a_stock_only_if_it_resolves() -> None:
    found = classify("RE", "CNL Healthcare Properties Inc")

    assert (found.asset_class, found.otherwise, found.lookup) == (
        AssetClass.EQUITY,
        AssetClass.REAL_ESTATE,
        True,
    )


def test_other_with_a_ticker_is_looked_up_by_the_ticker_alone() -> None:
    found = classify("OT", "Vanguard Mega Cap Growth ETF", "MGK")
    unticked = classify("OT", "JP Morgan Chase & Co Depositary Shares Preferred Series GG")

    assert (found.asset_class, found.otherwise, found.lookup, found.by_name) == (
        AssetClass.EQUITY,
        AssetClass.OTHER,
        True,
        False,
    )
    assert (unticked.asset_class, unticked.lookup) == (AssetClass.OTHER, False)


@pytest.mark.parametrize(
    "name",
    [
        "Huntington Bancshares Incorporated - Depositary Shares L Preferred Stock",
        "Cadence Bank 5.50% Series A (CADE$A)",
        "Regions Financial Corporation Depositary Shares",
    ],
)
def test_a_preferred_is_never_found_by_its_issuers_name(name: str) -> None:
    """The name would find the common stock."""
    assert classify("ST", name).by_name is False


def test_an_adr_is_still_found_by_name() -> None:
    assert classify("ST", "Nokia Corporation Sponsored American Depositary Shares").by_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Taylor TX ISD BE/R/", AssetClass.MUNICIPAL_BOND),
        ("CITY CNTY OF DENVER CO RV BE/R/", AssetClass.MUNICIPAL_BOND),
        ("Plano TX Indpt Sch BE/R/. Municioal Bond", AssetClass.MUNICIPAL_BOND),
        ("NEW YORK ST MTG AGY HOMEOWNER MTG REV", AssetClass.MUNICIPAL_BOND),
        ("UNIVERSITY HOUSTON TEX UNIV REVS", AssetClass.MUNICIPAL_BOND),
        ("U.S. Treasury Bill", AssetClass.TREASURY),
        ("NORTHERN TAX ADVGT ULTRA SHRT FIXED INC", AssetClass.MUTUAL_FUND),
        ("VANGUARD TOTAL STOCK MARKET INDEX ADM", AssetClass.MUTUAL_FUND),
        ("Albany Rd Strategic Opp Fund LLC", AssetClass.PRIVATE),
        ("Mays Allocate 2025 LP", AssetClass.PRIVATE),
        ("LINDA M MCCAUL 2005 GST EXEMPT DESC TRUST", AssetClass.OTHER),
        ("Bitcoin", AssetClass.CRYPTO),
    ],
)
def test_a_scans_line_is_classified_by_the_words_in_its_name(
    name: str, expected: AssetClass
) -> None:
    found = classify(None, name)

    assert (found.asset_class, found.lookup) == (expected, False)


@pytest.mark.parametrize(
    "name", ["ORACLE CORP", "ISHARES CORE S&P 500 ETF", "Spdr Ser Tr Aerospace Defense XAR"]
)
def test_a_scans_line_that_may_be_a_stock_is_unknown_unless_it_resolves(name: str) -> None:
    found = classify(None, name)

    assert (found.asset_class, found.otherwise, found.lookup) == (
        AssetClass.EQUITY,
        AssetClass.UNKNOWN,
        True,
    )


def test_a_mutual_funds_ticker_says_what_it_is() -> None:
    assert classify("OT", "Cliffwater Corporate Lending Fund", "CCLFX").asset_class == (
        AssetClass.MUTUAL_FUND
    )


# --- the ticker -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "name", "expected"),
    [
        ("brk/b", "Berkshire Hathaway Inc.", Ticker("BRK.B")),
        (None, "Nvidia Corporation (NVDA)", Ticker("NVDA")),
        (None, "Cliffwater Corporate Lending Fund (Ticker: CCLFX)", Ticker("CCLFX")),
        (None, "Tractor Supply Co TSCO", Ticker("TSCO", hinted=True)),
        (None, "Spdr Ser Tr Aerospace Defense XAR", Ticker("XAR", hinted=True)),
        (None, "CNL Healthcare Properties Inc (widely held)", None),
        (None, "Alpine Investors IX, LP (GLAS Funds, LP)", None),
        # All upper case: its last word is as likely the name's as a ticker.
        (None, "ORACLE CORP", None),
        (None, "Hims Hers Health Inc", None),
        (None, None, None),
    ],
)
def test_a_ticker_is_the_lines_or_one_in_its_name(
    ticker: str | None, name: str | None, expected: Ticker | None
) -> None:
    assert extract_ticker(ticker, name) == expected


# --- the name -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filed", "ours"),
    [
        ("Alphabet Inc. - Class A Common Stock", "ALPHABET INC-CL A"),
        ("Tractor Supply Co", "TRACTOR SUPPLY COMPANY"),
        ("AMAZON COM", "AMAZON.COM INC"),
        ("WORKDAY INCCLA", "WORKDAY INC-CLASS A"),
        ("AERCAP HOLDINGS N.V.", "AERCAP HOLDINGS NV"),
        ("ESTEE LAUDER COMPANIES INC COM USD0.01 CLASS A", "ESTEE LAUDER COMPANIES-CL A"),
        ("INTERNATIONAL BUS MACH CORP", "INTL BUSINESS MACHINES CORP"),
        ("Nokia Corporation Sponsored American Depositary Shares", "NOKIA CORP-SPON ADR"),
        ("CARLISLE COMPANIES INC", "CARLISLE COS INC"),
    ],
)
def test_the_share_words_and_legal_forms_come_out_on_both_sides(filed: str, ours: str) -> None:
    assert core_name(filed) == core_name(ours)
    assert name_similarity(filed, None, ours) == 1.0


def test_a_similarity_is_the_best_of_the_names_given() -> None:
    assert (
        name_similarity("Meta Platforms, Inc. - Class A", "FACEBOOK INC", "META PLATFORMS") == 1.0
    )
    assert name_similarity("GE Aerospace Common Stock", "GENERAL ELECTRIC") < 0.35
    assert name_similarity(None, "ANYTHING") == 0.0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alphabet Inc. - Class A Common Stock", "A"),
        ("ALPHABET INC-CL C", "C"),
        ("WORKDAY INC CLA", "A"),
        ("MASTERCARD INC - A", "A"),
        ("ALPHABET INC", None),
        (None, None),
    ],
)
def test_a_share_class_is_read_off_either_spelling(name: str | None, expected: str | None) -> None:
    assert share_class(name) == expected


def test_an_asset_key_ignores_case_spacing_and_punctuation_but_not_the_code() -> None:
    assert asset_key("st", "goog", "Alphabet Inc.  - Class C") == asset_key(
        "ST", "GOOG", "ALPHABET INC CLASS C"
    )
    assert asset_key("ST", None, "Microsoft Corp") != asset_key("CS", None, "Microsoft Corp")
    assert asset_key(None, "BRK/B", "Berkshire") == "|BRK.B|berkshire"
