"""Tests for the Form 4 ownership XML parser.

Two halves. The first runs the eight real filings in ``fixtures/form4`` (see its
README for why each one is there), one per transaction code or shape the AC
names. The second builds small documents inline for the cases no fixture has:
a negated plan footnote, a price written as a range, a namespace, a Form 5.

Most of what can go wrong here produces a plausible-looking feed rather than an
error. An award at "$0", a plan sale shown as a conviction sale, a joint filing
counted as five insiders: each has a test that fails instead.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.form4 import (
    Form4Document,
    Form4Transaction,
    _range,
    _says_plan,
    parse_form4,
)

FIXTURES = Path(__file__).parent / "fixtures" / "form4"


def fixture(slug: str) -> Form4Document:
    return parse_form4((FIXTURES / f"{slug}.xml").read_bytes())


def line(document: Form4Document, number: int) -> Form4Transaction:
    (match,) = [t for t in document.transactions if t.line_number == number]
    return match


# --- P: the purchase the feed exists for ------------------------------------


def test_an_open_market_purchase_is_read_field_by_field() -> None:
    document = fixture("purchase-mastercraft-ceo")

    assert document.document_type == "4"
    assert document.period_of_report == date(2026, 9, 15)
    assert document.issuer.cik == "0001638290"
    assert document.issuer.name == "MasterCraft Boat Holdings, Inc."
    assert document.issuer.trading_symbol == "MCFT"

    first = line(document, 1)
    assert first.is_derivative is False
    assert first.security_title == "Common Stock"
    assert first.transaction_date == date(2026, 9, 15)
    assert first.transaction_code == "P"
    assert first.acquired_disposed == "A"
    assert first.shares == Decimal(2400)
    assert first.price_per_share == Decimal("19.5")
    assert first.shares_owned_after == Decimal(80875)
    assert first.direct_or_indirect == "D"
    assert first.is_10b5_1 is False
    assert first.footnotes == {
        "F1": "This transaction represents an open market purchase of common stock by the "
        "reporing person."
    }
    assert [t.line_number for t in document.transactions] == [1, 2]


def test_an_owner_can_hold_several_roles_at_once() -> None:
    """A CEO on the board checks both boxes. An enum would keep one of them."""
    (owner,) = fixture("purchase-mastercraft-ceo").owners

    assert owner.cik == "0001570383"
    assert owner.name == "Nelson Bradley M."
    assert owner.is_director is True
    assert owner.is_officer is True
    assert owner.is_ten_percent_owner is False
    assert owner.is_other is False
    assert owner.officer_title == "Chief Executive Officer"


# --- S ----------------------------------------------------------------------


def test_a_sale_is_read_and_a_holding_line_is_not_a_transaction() -> None:
    """The holding line reports a balance (1,500 shares in a trust) and no
    trade. It is not in ``transactions`` and takes no line number, so the
    numbering of the trades does not depend on how many balances are listed."""
    document = fixture("sale-everpure-director")

    (sale,) = document.transactions
    assert sale.line_number == 1
    assert sale.transaction_code == "S"
    assert sale.acquired_disposed == "D"
    assert sale.shares == Decimal(4268)
    assert sale.price_per_share == Decimal("156.03")
    assert sale.shares_owned_after == Decimal(23415)
    assert sale.footnotes == {}
    # The footnote only the holding line cites is still the document's.
    assert document.footnotes == {"F1": "Shares are held by Nicholas Brown 2021 Gift Trust."}


def test_a_security_title_is_kept_as_filed() -> None:
    """Including the double space. It is evidence for the security link, and
    tidying it here would hide what the filer wrote."""
    (sale,) = fixture("sale-everpure-director").transactions
    assert sale.security_title == "Class A Common  Stock"


# --- A ----------------------------------------------------------------------


def test_an_award_filed_at_zero_has_no_price() -> None:
    """The filer wrote ``<value>0</value>``. Stored as zero, it would average
    into "insiders bought at $0" and value the award at nothing."""
    (award,) = fixture("award-state-street-director").transactions

    assert award.transaction_code == "A"
    assert award.acquired_disposed == "A"
    assert award.shares == Decimal(784)
    assert award.price_per_share is None
    assert award.footnotes == {"F1": "Payment of prorated Director's stock award."}


# --- M (and F) ----------------------------------------------------------------


def test_an_rsu_vesting_is_read_from_both_tables() -> None:
    """The derivative table's RSUs are disposed of and the common stock they
    convert into is acquired, both under code M. Non-derivative lines first,
    then derivative, which is the order ``line_number`` counts in."""
    document = fixture("exercise-intuitive-rsu")

    assert [
        (t.line_number, t.is_derivative, t.transaction_code) for t in document.transactions
    ] == [
        (1, False, "M"),
        (2, False, "F"),
        (3, True, "M"),
    ]

    stock, withheld, units = document.transactions
    assert stock.acquired_disposed == "A"
    assert stock.price_per_share is None  # filed as 0.0
    assert withheld.acquired_disposed == "D"
    assert withheld.price_per_share == Decimal("423.96")

    assert units.security_title == "Restricted Stock Units - 10-10-2024"
    assert units.acquired_disposed == "D"
    assert units.shares == Decimal(2140)
    assert units.price_per_share is None
    assert units.conversion_or_exercise_price is None  # an RSU has no strike; filed as 0.0
    assert units.underlying_security_title == "Common Stock"
    assert units.underlying_shares == Decimal(2140)
    assert units.shares_owned_after == Decimal(4279)


def test_a_footnote_cited_twice_on_a_line_is_listed_once() -> None:
    units = line(fixture("exercise-intuitive-rsu"), 3)
    assert list(units.footnotes) == ["F2"]


# --- 10b5-1 ------------------------------------------------------------------


def test_a_plan_sale_is_flagged_from_its_own_footnote() -> None:
    document = fixture("plan-sale-liquidia")
    sale = line(document, 2)

    assert document.aff_10b5_1 is True
    assert sale.transaction_code == "S"
    assert sale.is_10b5_1 is True
    assert sale.footnotes["F4"] == (
        "Transaction effected pursuant to a Rule 10b5-1 plan adopted by the Reporting "
        "Person on December 15, 2023."
    )


def test_the_checkbox_does_not_flag_lines_the_footnotes_leave_out() -> None:
    """The checkbox is one per form. When a footnote says which line was the
    plan trade, the PSU exercises beside it were not."""
    document = fixture("plan-sale-liquidia")

    assert line(document, 1).is_10b5_1 is False
    assert line(document, 3).is_10b5_1 is False


def test_the_checkbox_alone_flags_every_line() -> None:
    """No footnote names the plan, so nothing says which line the box means."""
    document = fixture("plan-checkbox-only-robinhood")

    assert document.aff_10b5_1 is True
    assert all("10b5" not in text for text in document.footnotes.values())
    assert [t.is_10b5_1 for t in document.transactions] == [True, True]


def test_no_checkbox_and_no_footnote_is_not_a_plan_trade() -> None:
    document = fixture("purchase-mastercraft-ceo")
    assert document.aff_10b5_1 is False
    assert not any(t.is_10b5_1 for t in document.transactions)


@pytest.mark.parametrize(
    "text",
    [
        "Sold pursuant to a Rule 10b5-1 trading plan adopted on March 6, 2026.",
        "Effected under a 10b-5-1 plan.",
        "Effected under a Rule 10b5\u20131 plan.",
        "Pursuant to a trading plan intended to comply with Rule 10b5-1(c).",
        "The shares were held in trust. The sale was made under a 10b5-1 plan.",
    ],
)
def test_plan_footnotes_are_recognised_however_the_rule_is_spelled(text: str) -> None:
    assert _says_plan(text)


@pytest.mark.parametrize(
    "text",
    [
        "This transaction was not made pursuant to a Rule 10b5-1 trading plan.",
        "The reporting person is no longer party to a 10b5-1 plan.",
        "Sold outside of the reporting person's Rule 10b5-1 plan.",
        "Liability under Rule 10b-5 is disclaimed.",
        "Rule 10b-5 10 day window.",
        "Weighted average price.",
    ],
)
def test_a_negated_or_unrelated_footnote_is_not_a_plan(text: str) -> None:
    assert not _says_plan(text)


def test_a_negated_sentence_does_not_cancel_an_affirmative_one() -> None:
    assert _says_plan(
        "The sale was made under a Rule 10b5-1 plan. The gift was not made under a "
        "Rule 10b5-1 plan."
    )


# --- several owners ------------------------------------------------------------


def test_every_reporting_owner_on_a_joint_filing_is_read_in_order() -> None:
    """Five owners, four sales reported once for all of them. Which owner a
    sale is attributed to is the loader's call; the parser keeps them all."""
    document = fixture("joint-filing-kinetik")

    assert [(o.cik, o.name) for o in document.owners] == [
        ("0001904934", "ISQ Global Fund II GP LLC"),
        ("0001904404", "I Squared Capital, LLC"),
        ("0001904413", "ISQ Holdings, LLC"),
        ("0002020905", "Wahba Sadek"),
        ("0002020876", "Bhandari Gautam"),
    ]
    assert all(o.is_ten_percent_owner for o in document.owners)
    assert len(document.transactions) == 4


def test_one_filing_can_report_trades_on_several_dates() -> None:
    document = fixture("joint-filing-kinetik")

    assert [t.transaction_date for t in document.transactions] == [
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 9),
        date(2026, 10, 12),
    ]
    assert document.period_of_report == date(2026, 10, 8)


# --- prices --------------------------------------------------------------------


def test_a_weighted_average_price_keeps_its_range() -> None:
    """The price is the average; the footnote says what it averages."""
    first = line(fixture("joint-filing-kinetik"), 1)

    assert first.price_per_share == Decimal("54.2944")
    assert (first.price_low, first.price_high) == (Decimal("53.66"), Decimal("54.58"))


def test_a_price_without_a_range_footnote_has_no_range() -> None:
    first = line(fixture("purchase-mastercraft-ceo"), 1)
    assert (first.price_low, first.price_high) == (None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("at prices ranging from $53.66 to $54.58, inclusive", ("53.66", "54.58")),
        ("at prices from $25.975 to $26.50.", ("25.975", "26.50")),
        ("between $1,041.10 and $1,042.00", ("1041.10", "1042.00")),
        ("$41.10 - $41.50", ("41.10", "41.50")),
        ("$41.10\u2013$41.50", ("41.10", "41.50")),
    ],
)
def test_price_ranges_are_read_from_footnote_text(text: str, expected: tuple[str, str]) -> None:
    assert _range(text) == (Decimal(expected[0]), Decimal(expected[1]))


@pytest.mark.parametrize(
    "text",
    [
        # An exchange rate, from a real TSMC footnote.
        "translated from NT$2,524.2277 in New Taiwan dollars, at the rate of NT$31.795 to US$1.",
        "from $54.58 to $53.66",  # reversed
        "Weighted average price.",
    ],
)
def test_text_that_is_not_a_price_range_is_not_read_as_one(text: str) -> None:
    assert _range(text) is None


# --- 4/A -----------------------------------------------------------------------


def test_an_amendment_says_what_it_amends() -> None:
    document = fixture("amendment-nvidia")

    assert document.document_type == "4/A"
    assert document.date_of_original_submission == date(2026, 9, 11)
    assert document.remarks is not None
    assert document.remarks.startswith("This amendment is being filed solely to correct")
    (award,) = document.transactions
    assert award.shares == Decimal(160418)


def test_an_original_has_no_original_submission_date() -> None:
    assert fixture("purchase-mastercraft-ceo").date_of_original_submission is None


# --- inline documents ------------------------------------------------------------


def document(
    *,
    document_type: str = "4",
    transactions: str = "",
    footnotes: str = "",
    owners: str | None = None,
    issuer_symbol: str = "ACME",
    extra: str = "",
    root: str = "<ownershipDocument>",
) -> bytes:
    """A minimal Form 4 with the given parts. Only what the schema requires,
    so a test shows the one thing it is about."""
    if owners is None:
        owners = owner()
    return f"""<?xml version="1.0"?>
{root}
  <schemaVersion>X0609</schemaVersion>
  <documentType>{document_type}</documentType>
  <periodOfReport>2026-10-01</periodOfReport>
  <issuer>
    <issuerCik>12345</issuerCik>
    <issuerName>ACME CORP</issuerName>
    <issuerTradingSymbol>{issuer_symbol}</issuerTradingSymbol>
  </issuer>
  {owners}
  {extra}
  <nonDerivativeTable>{transactions}</nonDerivativeTable>
  <footnotes>{footnotes}</footnotes>
</ownershipDocument>""".encode()


def owner(cik: str = "777", relationship: str = "<isDirector>1</isDirector>") -> str:
    return f"""<reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{cik}</rptOwnerCik><rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>{relationship}</reportingOwnerRelationship>
  </reportingOwner>"""


def transaction(
    *,
    code: str = "S",
    when: str = "<transactionDate><value>2026-10-01</value></transactionDate>",
    price: str = "<value>10.50</value>",
    shares: str = "100",
    ad: str = "D",
) -> str:
    return f"""<nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    {when}
    <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare>{price}</transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
    <postTransactionAmounts><sharesOwnedFollowingTransaction><value>900</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
  </nonDerivativeTransaction>"""


def test_a_minimal_document_parses() -> None:
    parsed = parse_form4(document(transactions=transaction()))

    assert parsed.issuer.cik == "0000012345"
    assert parsed.owners[0].cik == "0000000777"
    assert parsed.owners[0].is_director is True
    assert parsed.owners[0].is_officer is False  # absent box
    assert parsed.aff_10b5_1 is False  # absent, as on every pre-2023 filing
    (only,) = parsed.transactions
    assert only.price_per_share == Decimal("10.50")


def test_a_price_given_only_by_footnote_is_null_with_its_range() -> None:
    """How a price that exists only as a range is filed: no value, a footnote."""
    parsed = parse_form4(
        document(
            transactions=transaction(price='<footnoteId id="F1"/>'),
            footnotes='<footnote id="F1">Sold at prices ranging from $10.00 to $11.00.</footnote>',
        )
    )
    (only,) = parsed.transactions
    assert only.price_per_share is None
    assert (only.price_low, only.price_high) == (Decimal("10.00"), Decimal("11.00"))


def test_a_price_value_written_as_a_range_becomes_the_range() -> None:
    """Outside the schema, which types the value as a decimal, but the ticket
    names it. Neither end is the price, so the price is null."""
    parsed = parse_form4(document(transactions=transaction(price="<value>10.00-11.00</value>")))
    (only,) = parsed.transactions
    assert only.price_per_share is None
    assert (only.price_low, only.price_high) == (Decimal("10.00"), Decimal("11.00"))


def test_an_absent_price_element_is_null() -> None:
    parsed = parse_form4(
        document(
            transactions=transaction(price="").replace(
                "<transactionPricePerShare></transactionPricePerShare>", ""
            )
        )
    )
    assert parsed.transactions[0].price_per_share is None


def test_thousands_separators_in_quantities_are_tolerated() -> None:
    parsed = parse_form4(document(transactions=transaction(shares="1,250.5")))
    assert parsed.transactions[0].shares == Decimal("1250.5")


def test_a_namespaced_document_parses_the_same() -> None:
    """Namespace-free in practice; this is the insurance."""
    plain = parse_form4(document(transactions=transaction()))
    namespaced = parse_form4(
        document(
            transactions=transaction(),
            root='<ownershipDocument xmlns="http://www.sec.gov/edgar/ownership">',
        )
    )
    assert namespaced == plain


@pytest.mark.parametrize("symbol", ["", "NONE", "none", "N/A"])
def test_an_issuer_without_a_ticker_has_none(symbol: str) -> None:
    parsed = parse_form4(document(issuer_symbol=symbol))
    assert parsed.issuer.trading_symbol is None


def test_a_ticker_is_uppercased() -> None:
    assert parse_form4(document(issuer_symbol=" brk.b ")).issuer.trading_symbol == "BRK.B"


def test_a_form_with_only_holdings_has_no_transactions() -> None:
    assert parse_form4(document()).transactions == ()


def test_lower_case_codes_are_normalised() -> None:
    parsed = parse_form4(document(transactions=transaction(code="p", ad="a")))
    assert (parsed.transactions[0].transaction_code, parsed.transactions[0].acquired_disposed) == (
        "P",
        "A",
    )


def test_a_date_with_a_timezone_suffix_is_read_as_the_day() -> None:
    parsed = parse_form4(
        document(
            transactions=transaction(
                when="<transactionDate><value>2026-10-01-05:00</value></transactionDate>"
            )
        )
    )
    assert parsed.transactions[0].transaction_date == date(2026, 10, 1)


def test_a_footnote_with_markup_is_read_as_text() -> None:
    parsed = parse_form4(
        document(
            transactions=transaction(price='<value>10</value><footnoteId id="F1"/>'),
            footnotes='<footnote id="F1">Sold   under a\n <b>Rule 10b5-1</b> plan.</footnote>',
        )
    )
    (only,) = parsed.transactions
    assert only.footnotes == {"F1": "Sold under a Rule 10b5-1 plan."}
    assert only.is_10b5_1 is True


def test_a_cited_footnote_the_document_never_defines_is_left_out() -> None:
    parsed = parse_form4(
        document(transactions=transaction(price='<value>10</value><footnoteId id="F9"/>'))
    )
    assert parsed.transactions[0].footnotes == {}


# --- failures --------------------------------------------------------------------


def test_malformed_xml_is_a_parse_error() -> None:
    with pytest.raises(FilingParseError) as caught:
        parse_form4(b"<ownershipDocument><issuer>")
    assert caught.value.field == "xml"


@pytest.mark.parametrize("form", ["3", "5", "5/A"])
def test_another_ownership_form_is_refused(form: str) -> None:
    """Forms 3 and 5 share the schema. A Form 3 parsed as a Form 4 loads an
    initial holdings statement as a day of trades."""
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(document_type=form))
    assert caught.value.field == "documentType"
    assert caught.value.value == form


def test_a_filing_without_a_reporting_owner_is_refused() -> None:
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(owners=""))
    assert caught.value.field == "reportingOwner"


def test_a_transaction_without_a_date_fails_the_filing_and_names_the_line() -> None:
    """Not a dropped line: a Form 4 has no checksum, so a dropped trade is one
    that silently never happened."""
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(transactions=transaction() + transaction(when="")))
    assert caught.value.field == "transactionDate"
    assert "line 2" in caught.value.reason


def test_an_unknown_acquired_disposed_code_is_refused() -> None:
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(transactions=transaction(ad="X")))
    assert caught.value.field == "transactionAcquiredDisposedCode"


def test_a_negative_share_count_is_refused() -> None:
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(transactions=transaction(shares="-100")))
    assert caught.value.field == "transactionShares"


def test_an_unknown_boolean_spelling_is_refused() -> None:
    """Reading an unfamiliar "yes" as "no" would hide a role."""
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(owners=owner(relationship="<isDirector>maybe</isDirector>")))
    assert caught.value.field == "isDirector"


def test_a_non_numeric_cik_is_refused() -> None:
    with pytest.raises(FilingParseError) as caught:
        parse_form4(document(owners=owner(cik="abc")))
    assert caught.value.field == "rptOwnerCik"
