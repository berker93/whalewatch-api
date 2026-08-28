"""Tests for the units decision and the guards that check it.

The bug this module exists to prevent produces no exception, no warning and no
visibly wrong number: every value in a mis-parsed quarter is wrong by the same
factor, so rankings, percentages and quarter-over-quarter shapes all look
exactly as they should. It surfaces months later as "why does this fund have $40
million in it". So the tests here are mostly not "does the arithmetic work" —
they are "does something fail loudly when the arithmetic is wrong".

Two fixtures, one pair:

``information_table_thousands.xml`` / ``information_table_dollars.xml``
    The same four positions, filed on opposite sides of 2023-01-03. Same CUSIPs,
    same share counts, same money; values 1000x apart because the convention
    changed and the documents do not say so. Every test that matters here is a
    statement about these two producing the same answer, or about what happens
    when they are read as each other.

Cover pages are built in this module rather than committed as XML. The checksum
guards are a function of two integers on the summary page, and a fixture per
combination of those two integers would be six more documents that say nothing
a keyword argument does not.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.db.models.filing import ParseStatus
from app.ingestion.normalisation import (
    DOLLAR_CUTOVER,
    MAX_IMPLIED_PRICE,
    MAX_NOTED_ROWS,
    MIN_IMPLIED_PRICE,
    NormalisedFiling,
    NoteKind,
    ParseNote,
    normalise_filing,
    resolve_value_multiplier,
)
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    InfoTableRow,
    InfoTableWarning,
    PrimaryDoc,
    parse_information_table,
)

FIXTURES = Path(__file__).parent / "fixtures" / "thirteen_f"

# One filing from each side of the line, and the dates they were accepted on.
# 2022-02-14 is a Q4 2021 filing; 2024-05-15 is a Q1 2024 filing.
BEFORE_CUTOVER = datetime(2022, 2, 14, 16, 30, tzinfo=UTC)
AFTER_CUTOVER = datetime(2024, 5, 15, 16, 30, tzinfo=UTC)

# What the paired fixtures report, in dollars, whichever side they were filed
# from. Written out rather than derived, because a test that computes its own
# expectation from the same multiplier it is checking asserts nothing.
PORTFOLIO = {
    "037833100": (Decimal("2040000000.00"), Decimal(12_000_000), Decimal("170.00")),
    "594918104": (Decimal("1190000000.00"), Decimal(3_500_000), Decimal("340.00")),
    "191216100": (Decimal("1500000000.00"), Decimal(25_000_000), Decimal("60.00")),
    "82968B103": (Decimal("200000000.00"), Decimal(40_000_000), Decimal("5.00")),
}
PORTFOLIO_TOTAL_USD = sum(value for value, _, _ in PORTFOLIO.values())


def load(name: str) -> InformationTable:
    return parse_information_table((FIXTURES / f"information_table_{name}.xml").read_bytes())


@pytest.fixture(scope="module")
def thousands() -> InformationTable:
    """The pre-cutover half of the pair: values in thousands of dollars."""
    return load("thousands")


@pytest.fixture(scope="module")
def dollars() -> InformationTable:
    """The post-cutover half: the same positions, in whole dollars."""
    return load("dollars")


def cover(
    *,
    entry_total: int | None = len(PORTFOLIO),
    value_total: int | None = None,
    period: date = date(2024, 3, 31),
    form_type: str = "13F-HR",
) -> PrimaryDoc:
    """A cover page with the two summary-page totals under the test's control.

    ``value_total`` defaults to ``None`` — no declared total, hence no checksum
    — so that a test about the price guard is not quietly also a test about the
    checksum. Tests that want the checksum say what the filer declared, in the
    filer's own units.
    """
    return PrimaryDoc(
        cik="0001067983",
        filer_name="Berkshire Hathaway Inc",
        form_type=form_type,
        period_of_report=period,
        signature_date=None,
        amendment_no=None,
        amendment_kind=None,
        report_type="13F HOLDINGS REPORT",
        table_entry_total=entry_total,
        table_value_total=value_total,
        other_included_managers_count=None,
        confidential_omitted=False,
    )


def row(
    *, cusip: str = "037833100", value: Decimal, shares: Decimal, **overrides: Any
) -> InfoTableRow:
    """One information-table row, as filed. Values in the filing's own units."""
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
        "voting_sole": None,
        "voting_shared": None,
        "voting_none": None,
    }
    return InfoTableRow(**{**fields, **overrides})


def table(*rows: InfoTableRow, warnings: tuple[InfoTableWarning, ...] = ()) -> InformationTable:
    return InformationTable(rows=rows, warnings=warnings)


def notes_of(kind: NoteKind, filing: NormalisedFiling) -> list[ParseNote]:
    """The findings of one guard, so a test can assert on that guard alone."""
    return [note for note in filing.parse_notes if note.kind is kind]


# --- the multiplier ----------------------------------------------------------


def test_a_filing_from_before_the_cutover_is_in_thousands() -> None:
    assert resolve_value_multiplier(BEFORE_CUTOVER) == 1000


def test_a_filing_from_after_the_cutover_is_in_whole_dollars() -> None:
    assert resolve_value_multiplier(AFTER_CUTOVER) == 1


def test_the_cutover_day_itself_is_whole_dollars() -> None:
    """ "On or after", not "after". The rule includes its own boundary date, and
    an off-by-one day here is a 1000x error on every filing accepted that day."""
    eastern_midnight = datetime.combine(
        DOLLAR_CUTOVER, datetime.min.time(), tzinfo=timezone(timedelta(hours=-5))
    )

    assert resolve_value_multiplier(eastern_midnight) == 1
    assert resolve_value_multiplier(eastern_midnight - timedelta(seconds=1)) == 1000


def test_the_line_is_drawn_on_edgars_clock_not_utc() -> None:
    """21:00 on 2 January in New York is 02:00 on the 3rd in UTC.

    EDGAR's filing date is the Eastern one and it accepts submissions until
    22:00 there, so a naive ``filed_at.date() >= DOLLAR_CUTOVER`` on a UTC
    timestamp moves every filing from the last three hours of 2 January across
    the line and reads its thousands as dollars. Three hours of the one day this
    module exists to get right.
    """
    late_on_the_second = datetime(2023, 1, 3, 2, 0, tzinfo=UTC)

    assert late_on_the_second.date() >= DOLLAR_CUTOVER  # what the naive test sees
    assert resolve_value_multiplier(late_on_the_second) == 1000


def test_a_timestamp_with_no_timezone_is_refused() -> None:
    """Rather than guessed at. The two available guesses — UTC and Eastern —
    differ by exactly the hours in which the answer changes, so picking one
    silently is the failure mode itself, dressed as a default."""
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_value_multiplier(datetime(2023, 1, 3, 2, 0))


def test_an_amendment_follows_the_date_it_was_filed_not_the_period_it_describes(
    dollars: InformationTable,
) -> None:
    """The case a ``period_of_report < 2023`` test gets exactly backwards.

    A restatement filed in 2024 for the fourth quarter of 2021 is written in
    whole dollars, because the convention follows the submission. Keying off the
    period would multiply this filing by 1000 — and amendments are the filings
    nobody is watching.
    """
    amendment = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(period=date(2021, 12, 31), form_type="13F-HR/A"),
        table=dollars,
    )

    assert amendment.value_multiplier == 1
    assert amendment.holdings[0].value_usd == PORTFOLIO["037833100"][0]


# --- the property: the same position, either side of the line ----------------


def test_the_same_position_is_the_same_money_on_either_side_of_the_cutover(
    thousands: InformationTable, dollars: InformationTable
) -> None:
    """The headline property, and the reason the fixtures are a matched pair.

    Two filings of the same four positions, one from 2022 and one from 2024,
    whose ``value`` columns differ by 1000x because the convention did. After
    normalisation they are the same portfolio, to the cent.
    """
    old = normalise_filing(filed_at=BEFORE_CUTOVER, cover=cover(), table=thousands)
    new = normalise_filing(filed_at=AFTER_CUTOVER, cover=cover(), table=dollars)

    assert old.value_multiplier == 1000
    assert new.value_multiplier == 1
    assert [holding.value_usd for holding in old.holdings] == [
        holding.value_usd for holding in new.holdings
    ]
    for holding in old.holdings:
        expected, _, _ = PORTFOLIO[holding.row.cusip]
        assert holding.value_usd == expected


@pytest.mark.parametrize("side", ["thousands", "dollars"])
def test_every_implied_price_is_a_price_whichever_side_it_was_filed_from(side: str) -> None:
    """The property stated the way it is worth stating: for a fixed position,
    ``value_usd / shares`` lands on the real share price regardless of which
    convention the document used. A test that only compared the two filings to
    each other would pass just as happily if both were wrong by 1000."""
    filed_at = BEFORE_CUTOVER if side == "thousands" else AFTER_CUTOVER

    normalised = normalise_filing(filed_at=filed_at, cover=cover(), table=load(side))

    for holding in normalised.holdings:
        _, shares, price = PORTFOLIO[holding.row.cusip]
        assert holding.row.shares == shares
        assert holding.implied_price == price
        assert MIN_IMPLIED_PRICE <= price <= MAX_IMPLIED_PRICE
    assert normalised.parse_status is ParseStatus.OK


# --- the implied-price guard -------------------------------------------------


def test_reading_a_pre_cutover_filing_as_whole_dollars_is_caught(
    thousands: InformationTable,
) -> None:
    """The mistake this ticket exists to prevent, made deliberately.

    Note what does *not* fire: the filer's own checksum. ``tableValueTotal`` is
    in the same units as the rows, so it agrees with itself under either
    multiplier and says nothing about which one is right. The implied price is
    the only check that reaches outside the document, which is why it is the one
    that catches both our bug and the filer's.
    """
    misread = normalise_filing(
        filed_at=AFTER_CUTOVER,  # the bug: this filing was accepted in 2022
        cover=cover(value_total=4_930_000),
        table=thousands,
    )

    assert misread.parse_status is ParseStatus.SUSPECT
    assert notes_of(NoteKind.VALUE_TOTAL, misread) == []
    flagged = notes_of(NoteKind.IMPLIED_PRICE, misread)
    assert [note.cusip for note in flagged] == ["82968B103"]
    assert flagged[0].observed == Decimal("0.005")


def test_reading_a_post_cutover_filing_as_thousands_is_caught(
    dollars: InformationTable,
) -> None:
    """The same mistake in the other direction, which the upper bound catches.

    Two of the four positions clear $100,000 a share once multiplied by 1000 and
    two do not, which is the honest shape of this guard: it fires on a filing,
    not on every row of one. One implausible price in a portfolio is enough to
    make the whole filing worth a second look, and that is all ``suspect`` claims.
    """
    misread = normalise_filing(filed_at=BEFORE_CUTOVER, cover=cover(), table=dollars)

    assert misread.parse_status is ParseStatus.SUSPECT
    flagged = notes_of(NoteKind.IMPLIED_PRICE, misread)
    assert sorted(str(note.cusip) for note in flagged) == ["037833100", "594918104"]


def test_a_filer_who_kept_filing_in_thousands_after_the_cutover_is_caught() -> None:
    """The filer's bug rather than ours, and the reason the guard is not just a
    test of ``resolve_value_multiplier``. A handful of managers genuinely filed
    in thousands after 2023-01-03 and had to amend; nothing about the submission
    date says so, and every internal total in the document agrees with itself, so
    this is the only check in the module that can notice."""
    eight_dollar_stock = table(row(value=Decimal(800_000), shares=Decimal(100_000_000)))

    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=1, value_total=800_000),
        table=eight_dollar_stock,
    )

    assert filing.parse_status is ParseStatus.SUSPECT
    assert notes_of(NoteKind.IMPLIED_PRICE, filing)[0].observed == Decimal("0.008")


def test_the_price_floor_does_not_catch_an_under_scaled_mega_cap() -> None:
    """The guard's blind spot, pinned so that nobody assumes more cover than
    there is.

    Dividing by 1000 in error moves an implied price below one cent only for a
    stock that trades under about $10. A $170 position read as dollars when it
    was filed in thousands implies $0.17 — implausible for that security,
    entirely plausible for *a* security, and this guard cannot tell the
    difference because it does not know which security it is looking at. The
    filer's own totals cannot tell either: they are in the same units as the
    rows and agree with themselves under either multiplier.

    What closes this gap is the check the ingestion spec puts on the enrichment
    step — ``value`` against ``shares x price at period end`` — which needs a
    price feed and so is not a guard this module can run. Until then, the
    multiplier itself is what stands between us and this case, which is why it
    is keyed on a fact and stored on the row rather than inferred.
    """
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,  # the bug again, on a $170 stock this time
        cover=cover(entry_total=1, value_total=1_700_000),
        table=table(row(value=Decimal(1_700_000), shares=Decimal(10_000_000))),
    )

    assert filing.holdings[0].implied_price == Decimal("0.17")
    assert filing.parse_status is ParseStatus.OK


@pytest.mark.parametrize(
    ("price", "flagged"),
    [
        (Decimal("0.009"), True),
        (Decimal("0.01"), False),
        (Decimal("100000"), False),
        (Decimal("100000.01"), True),
    ],
)
def test_the_price_range_is_inclusive_at_both_ends(price: Decimal, flagged: bool) -> None:
    """The bounds are plausible prices, not implausible ones. A stock at exactly
    a cent is a real stock and must not be flagged; the range is what is
    accepted, and only what falls outside it is a finding."""
    shares = Decimal(1_000_000)
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=1),
        table=table(row(value=price * shares, shares=shares)),
    )

    assert bool(notes_of(NoteKind.IMPLIED_PRICE, filing)) is flagged


def test_a_row_reporting_no_shares_does_not_divide_by_zero() -> None:
    """And is not a finding either. A row with no quantity says nothing about
    units in either direction, and flagging it would spend the operator's
    attention on the one thing the guard cannot speak to."""
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=1),
        table=table(row(value=Decimal(1_000_000), shares=Decimal(0))),
    )

    assert filing.holdings[0].implied_price is None
    assert filing.parse_status is ParseStatus.OK


def test_a_position_that_rounded_to_zero_in_thousands_is_not_a_finding() -> None:
    """A holding worth less than $500 reads as ``0`` under the pre-cutover
    convention. That is the format working, not a units error — and reading it
    as a $0.00 share price would make a large share of every pre-2023 filing
    suspect, which is how a flag gets ignored."""
    filing = normalise_filing(
        filed_at=BEFORE_CUTOVER,
        cover=cover(entry_total=1),
        table=table(row(value=Decimal(0), shares=Decimal(400))),
    )

    assert filing.holdings[0].value_usd == Decimal(0)
    assert filing.parse_status is ParseStatus.OK


def test_an_options_notional_value_is_still_checked_against_its_underlying() -> None:
    """A put or call line's value is the notional value of the underlying and
    its quantity is the underlying's shares, so the ratio is still that stock's
    price — and a 1000x error still moves it out of range."""
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=1),
        table=table(
            row(value=Decimal(190_000_000_000), shares=Decimal(1_000_000), put_call="Call")
        ),
    )

    assert notes_of(NoteKind.IMPLIED_PRICE, filing)[0].observed == Decimal(190_000)


# --- the checksum guards -----------------------------------------------------


def test_a_short_row_count_against_the_cover_page_is_suspect(
    dollars: InformationTable,
) -> None:
    """The signal that separates "a fund sold most of its positions" from "we
    lost most of its positions", which are the same thing in the data."""
    filing = normalise_filing(filed_at=AFTER_CUTOVER, cover=cover(entry_total=41), table=dollars)

    assert filing.parse_status is ParseStatus.SUSPECT
    note = notes_of(NoteKind.ENTRY_COUNT, filing)[0]
    assert (note.observed, note.expected) == (Decimal(4), Decimal(41))


def test_a_declared_total_the_rows_do_not_reach_is_suspect(
    dollars: InformationTable,
) -> None:
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(value_total=int(PORTFOLIO_TOTAL_USD) * 2),
        table=dollars,
    )

    assert filing.parse_status is ParseStatus.SUSPECT
    assert notes_of(NoteKind.VALUE_TOTAL, filing)[0].observed == PORTFOLIO_TOTAL_USD


def test_a_declared_total_the_rows_nearly_reach_is_not(dollars: InformationTable) -> None:
    """Filers round. The summary page is often computed from a spreadsheet that
    carried more precision than the rows printed from it, so a fraction of a
    percent across a filing is not a finding — and no rounding error is ever
    1000x."""
    off_by_half_a_percent = int(PORTFOLIO_TOTAL_USD * Decimal("1.005"))

    filing = normalise_filing(
        filed_at=AFTER_CUTOVER, cover=cover(value_total=off_by_half_a_percent), table=dollars
    )

    assert filing.parse_status is ParseStatus.OK


def test_the_declared_total_is_compared_in_the_filings_own_units(
    thousands: InformationTable,
) -> None:
    """The checksum scales the cover page by the same multiplier as the rows.

    Comparing a pre-cutover filing's declared total — 4,930,000, in thousands —
    against a sum already normalised to dollars would report a 1000x discrepancy
    on every filing before 2023, which is a guard that fires on everything and
    therefore on nothing.
    """
    filing = normalise_filing(
        filed_at=BEFORE_CUTOVER, cover=cover(value_total=4_930_000), table=thousands
    )

    assert filing.parse_status is ParseStatus.OK
    assert filing.holdings[0].value_usd == PORTFOLIO["037833100"][0]


def test_a_cover_page_with_no_summary_totals_is_no_check_rather_than_a_failure() -> None:
    """A ``13F NOTICE`` reports no holdings and has no summary page, so both
    totals are absent. Treating a missing declared total as zero would make
    every one of them suspect."""
    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=None, value_total=None),
        table=table(),
    )

    assert filing.parse_status is ParseStatus.OK
    assert filing.parse_notes == ()


# --- suspect means flagged, not rejected -------------------------------------


def test_a_suspect_filing_still_produces_every_holding(dollars: InformationTable) -> None:
    """The whole disposition of this module. A filing that fails every guard is
    still the only disclosure that manager made for that quarter, and dropping
    it leaves a hole indistinguishable from a manager who filed nothing."""
    filing = normalise_filing(
        filed_at=BEFORE_CUTOVER,  # wrong side: two prices go over $100,000
        cover=cover(entry_total=99, value_total=1),
        table=dollars,
    )

    assert filing.parse_status is ParseStatus.SUSPECT
    assert len(filing.holdings) == len(dollars.rows)
    assert {note.kind for note in filing.parse_notes} == {
        NoteKind.IMPLIED_PRICE,
        NoteKind.ENTRY_COUNT,
        NoteKind.VALUE_TOTAL,
    }


# --- what lands in the column ------------------------------------------------


def test_a_clean_filing_writes_no_notes_at_all(dollars: InformationTable) -> None:
    """``None`` rather than ``[]``, so that "the guards found nothing" has one
    spelling in the column rather than two every query has to handle."""
    filing = normalise_filing(filed_at=AFTER_CUTOVER, cover=cover(), table=dollars)

    assert filing.parse_notes == ()
    assert filing.parse_notes_json is None


def test_notes_serialise_with_their_numbers_as_strings(thousands: InformationTable) -> None:
    """A JSON number is an IEEE 754 double. A column whose job is to record a
    suspected 1000x error is the last place to introduce a second rounding of
    the figure in question."""
    filing = normalise_filing(filed_at=AFTER_CUTOVER, cover=cover(), table=thousands)

    notes = filing.parse_notes_json
    assert notes is not None
    assert notes[0]["kind"] == "implied_price"
    assert notes[0]["observed"] == "0.005"
    assert notes[0]["cusip"] == "82968B103"
    assert "expected" not in notes[0]  # the price guard checks a range, not a number


def test_a_filing_that_is_wrong_in_every_row_does_not_write_a_note_for_every_row() -> None:
    """The cap, and the case it is for: a units error puts *every* position out
    of range, so the unbounded version of this column is one JSON object per
    holding — three thousand of them, on exactly the filings someone is most
    likely to open. The summary note carries the real count."""
    rows = [
        row(cusip=f"00000{index:04d}", value=Decimal(1), shares=Decimal(1_000_000))
        for index in range(MAX_NOTED_ROWS * 3)
    ]

    filing = normalise_filing(
        filed_at=AFTER_CUTOVER, cover=cover(entry_total=len(rows)), table=table(*rows)
    )

    flagged = notes_of(NoteKind.IMPLIED_PRICE, filing)
    assert len(flagged) == MAX_NOTED_ROWS + 1
    assert flagged[-1].observed == Decimal(len(rows))
    assert flagged[-1].row is None


def test_a_row_the_parser_dropped_is_named_in_the_notes(dollars: InformationTable) -> None:
    """So that "two rows short" becomes "these two rows, this CUSIP, this
    field". The count guard says a filing lost something; only this says what."""
    dropped = InfoTableWarning(
        row=3,
        field="value",
        reason="expected a number",
        value="N/A",
        cusip="911312106",
        dropped=True,
    )

    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(entry_total=len(dollars.rows) + 1),
        table=table(*dollars.rows, warnings=(dropped,)),
    )

    note = notes_of(NoteKind.DROPPED_ROW, filing)[0]
    assert (note.row, note.cusip) == (3, "911312106")
    assert "expected a number" in note.detail


def test_a_tolerated_warning_is_not_a_note(dollars: InformationTable) -> None:
    """A malformed ``<figi>`` costs no value and no share count — the row is
    kept with the FIGI nulled. Recording it here would fill the column that
    exists for missing money with findings about enrichment."""
    tolerated = InfoTableWarning(
        row=1,
        field="figi",
        reason="expected twelve characters",
        value="N/A",
        cusip="037833100",
        dropped=False,
    )

    filing = normalise_filing(
        filed_at=AFTER_CUTOVER,
        cover=cover(),
        table=table(*dollars.rows, warnings=(tolerated,)),
    )

    assert filing.parse_notes == ()


# --- precision ---------------------------------------------------------------


def test_the_normalised_value_is_a_decimal_quantised_to_the_cent() -> None:
    """``holding.value_usd`` is ``numeric(20, 2)``. Quantising here means the
    number in a note is the number in the column, and never a float: these
    values are summed across thousands of rows, and in binary floating point the
    total depends on the order the rows arrive in."""
    filing = normalise_filing(
        filed_at=BEFORE_CUTOVER,
        cover=cover(entry_total=1),
        table=table(row(value=Decimal("1234567"), shares=Decimal(1_000_000))),
    )

    value = filing.holdings[0].value_usd
    assert isinstance(value, Decimal)
    assert value == Decimal("1234567000.00")
    assert value.as_tuple().exponent == -2
