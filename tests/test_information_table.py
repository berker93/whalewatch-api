"""Tests for the 13F information table parser.

This is the table every number in the product is a sum over, so most of what
follows is not about whether a well-formed document parses — it is about the
ways a wrong answer looks right. A portfolio that is missing four positions, or
whose values are off by 1000x, or that counted a put as stock, is still a
plausible-looking portfolio. Each of those has a test here that fails loudly
instead.

Two fixtures, one generator:

``information_table_berkshire.xml``
    41 rows, and the checksum half of the suite. Its rows sum to exactly the
    ``tableValueTotal`` on ``primary_doc_original.xml`` and its row count is
    that document's ``tableEntryTotal``, so the two fixtures are parsed together
    and compared the way the loader will compare them. The values are realistic
    rather than a byte copy of the SEC's own file — the point of the pair is
    that they agree.

``information_table_modern.xml``
    A post-2023 table: whole dollars, a ``<figi>`` column, options and a
    convertible note, an unfamiliar namespace carried on a prefix, and six rows
    that are each broken in one specific way an agent has actually broken one.

``large_table()``
    3,000 rows, generated. Deliberately not committed: 600KB of synthetic XML in
    git tells a reader nothing that six lines of generator do not, and the
    generator can be turned up to 30,000 by anyone investigating a memory
    problem.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    InfoTableRow,
    _info_tables,
    parse_information_table,
    parse_primary_doc,
)

FIXTURES = Path(__file__).parent / "fixtures" / "thirteen_f"


def load(name: str) -> bytes:
    return (FIXTURES / f"information_table_{name}.xml").read_bytes()


@pytest.fixture(scope="module")
def berkshire() -> InformationTable:
    return parse_information_table(load("berkshire"))


@pytest.fixture(scope="module")
def modern() -> InformationTable:
    return parse_information_table(load("modern"))


def only(table: InformationTable, cusip: str) -> InfoTableRow:
    """The single row with this CUSIP, asserting that it is single."""
    matches = [row for row in table.rows if row.cusip == cusip]
    assert len(matches) == 1, f"{cusip} appears {len(matches)} times"
    return matches[0]


# --- the checksum ------------------------------------------------------------


def test_parsed_table_matches_the_cover_pages_own_count() -> None:
    """The check the loader exists to perform, run across the two fixtures.

    ``tableEntryTotal`` is the filer's own count of its rows. A parser that
    silently skipped a malformed ``<infoTable>`` produces a portfolio that is
    merely *smaller* than the real one, which is indistinguishable from a fund
    that sold — and this is the only signal in the filing that says otherwise.
    """
    cover = parse_primary_doc((FIXTURES / "primary_doc_original.xml").read_bytes())
    table = parse_information_table(load("berkshire"))

    assert len(table.rows) == cover.table_entry_total


def test_parsed_values_sum_to_the_cover_pages_own_total() -> None:
    """The same check on dollars, and a free check on the units.

    Both sides are in whatever units the filing used, because neither the parser
    nor this test applies ``value_multiplier``. That is the point: if the parser
    ever starts scaling values, this equality breaks, which is the cheapest
    possible alarm on a 1000x error.
    """
    cover = parse_primary_doc((FIXTURES / "primary_doc_original.xml").read_bytes())
    table = parse_information_table(load("berkshire"))

    assert cover.table_value_total is not None
    assert table.value_total == Decimal(cover.table_value_total)


def test_a_clean_filing_produces_no_warnings(berkshire: InformationTable) -> None:
    assert berkshire.warnings == ()


# --- one row, field by field -------------------------------------------------


def test_every_field_of_a_row_is_read(berkshire: InformationTable) -> None:
    """Asserted field by field so the failure names the field that broke."""
    row = berkshire.rows[0]

    assert row.name_of_issuer == "AMERICAN EXPRESS CO"
    assert row.title_of_class == "COM"
    assert row.cusip == "025816109"
    assert row.figi is None
    assert row.value == Decimal(34519834)
    assert row.shares == Decimal(151610700)
    assert row.sh_prn_type == "SH"
    assert row.put_call is None
    assert row.investment_discretion == "DEFINED"
    assert row.other_managers == "4,8"
    assert row.voting_sole == Decimal(151610700)
    assert row.voting_shared == Decimal(0)
    assert row.voting_none == Decimal(0)


def test_share_count_comes_out_of_its_wrapper(berkshire: InformationTable) -> None:
    """``sshPrnamt`` is nested inside ``<shrsOrPrnAmt>``, not a child of the row.

    A lookup against the row's direct children finds nothing here and yields a
    filing in which every position exists and every share count is missing.
    """
    assert only(berkshire, "037833100").shares == Decimal(789368450)


def test_quantities_are_decimal_and_never_float(berkshire: InformationTable) -> None:
    """Float would make the total depend on the order the rows came back in.

    The equality below is the actual reason this matters: it holds for Decimal
    over 41 rows and is not guaranteed to hold for float over any of them.
    """
    row = berkshire.rows[0]
    assert type(row.value) is Decimal
    assert type(row.shares) is Decimal
    assert berkshire.value_total == sum(r.value for r in reversed(berkshire.rows))


def test_voting_authority_is_read_as_filed(berkshire: InformationTable) -> None:
    """Including when it disagrees with the share count, which filers allow."""
    coke = only(berkshire, "191216100")
    assert (coke.voting_sole, coke.voting_shared, coke.voting_none) == (
        Decimal(390000000),
        Decimal(10000000),
        Decimal(0),
    )

    moodys = only(berkshire, "615369105")
    assert moodys.voting_sole == Decimal(0)
    assert moodys.voting_none == moodys.shares


def test_absent_voting_authority_is_none_and_not_zero(berkshire: InformationTable) -> None:
    """ "Not stated" and "holds no vote" are different claims about a position."""
    nvr = only(berkshire, "62944T105")
    assert nvr.voting_sole is None
    assert nvr.voting_shared is None
    assert nvr.voting_none is None


def test_result_cannot_be_edited(berkshire: InformationTable) -> None:
    with pytest.raises(Exception, match="frozen"):
        berkshire.rows[0].cusip = "000000000"  # type: ignore[misc]


# --- CUSIPs ------------------------------------------------------------------


def test_short_cusip_is_left_padded(berkshire: InformationTable) -> None:
    """A missing leading zero is a spreadsheet artefact, not a different security.

    Left unpadded it would resolve to its own ``security`` row, splitting the
    holders of one instrument across two of them with neither total looking
    wrong.
    """
    assert "060505104" in {row.cusip for row in berkshire.rows}
    assert "60505104" not in {row.cusip for row in berkshire.rows}


def test_cusip_punctuation_and_case_are_normalised(modern: InformationTable) -> None:
    """``67066G-10-4`` and ``67066G104`` are the same nine characters."""
    assert only(modern, "67066G104").name_of_issuer == "NVIDIA CORPORATION"


def test_one_cusip_can_appear_on_several_rows(berkshire: InformationTable) -> None:
    """And the parser does not collapse them. This is a real filing shape.

    A filer reports the same CUSIP once per set of managers sharing it. Those
    rows differ only in ``otherManager``, which is *not* part of ``holding``'s
    natural key — so the loader has to sum them before insert or the second one
    will overwrite the first. Collapsing them here would hide from the loader
    that there was ever anything to sum.
    """
    rows = [row for row in berkshire.rows if row.cusip == "060505104"]

    assert len(rows) == 2
    assert len(berkshire.rows) == 41
    assert len({row.cusip for row in berkshire.rows}) == 40


# --- options and principal amounts -------------------------------------------


def test_put_call_distinguishes_three_rows_sharing_a_cusip(modern: InformationTable) -> None:
    """Common stock, calls and puts on one CUSIP, in one filing. All legal.

    Read the ``putCall`` wrong and the option rows merge into the stock row —
    which is where the natural key's ``put_call`` column comes from, and why
    reading an unknown spelling as "no option" is not an acceptable fallback.
    """
    microsoft = [row for row in modern.rows if row.cusip == "594918104"]

    assert [row.put_call for row in microsoft] == [None, "Call", "Put"]
    assert [row.value for row in microsoft] == [
        Decimal(12345678900),
        Decimal(800000000),
        Decimal(250000000),
    ]


def test_put_call_spellings_are_normalised_to_the_column(modern: InformationTable) -> None:
    """The fixture writes ``CALL`` and ``put``; the check constraint takes neither."""
    assert {row.put_call for row in modern.rows} == {None, "Call", "Put"}


def test_self_closing_put_call_reads_as_common_stock(modern: InformationTable) -> None:
    """``<putCall/>`` is how agents spell "this is not an option"."""
    assert only(modern, "00846U101").put_call is None


def test_principal_amounts_keep_their_own_unit(modern: InformationTable) -> None:
    """A convertible note reports face value in dollars, not a share count.

    Summed with ``SH`` rows it adds dollars to a share count and produces a
    number with no unit at all, which is why the type stays on the row and in
    the natural key.
    """
    note = only(modern, "88160RAG6")
    assert note.sh_prn_type == "PRN"
    assert note.shares == Decimal(25000000)


# --- the FIGI column ---------------------------------------------------------


def test_figi_is_read_and_uppercased(modern: InformationTable) -> None:
    assert modern.rows[0].figi == "BBG000BPH459"
    assert only(modern, "67066G104").figi == "BBG000BBJQV0"


def test_a_bad_figi_warns_without_losing_the_position(modern: InformationTable) -> None:
    """The one tolerated failure, and the asymmetry is the point.

    A FIGI can be looked up again later; a holding that was never returned
    cannot be. Dropping a real position over an optional identifier is how a
    portfolio quietly shrinks.
    """
    airbnb = only(modern, "009066101")
    assert airbnb.figi is None
    assert airbnb.value == Decimal(145000000)

    warning = next(w for w in modern.warnings if w.field == "figi")
    assert warning.dropped is False
    assert warning.cusip == "009066101"
    assert warning.value == "N/A"


# --- warnings ----------------------------------------------------------------


def test_broken_rows_are_reported_rather_than_dropped_silently(
    modern: InformationTable,
) -> None:
    """Each dropped row names its element, its reason, and the security lost.

    Without the CUSIP on the warning the only way to answer "which position did
    we lose" is to open the XML and count ``<infoTable>`` elements by hand.
    """
    dropped = [w for w in modern.warnings if w.dropped]

    assert [(w.field, w.cusip) for w in dropped] == [
        ("value", "911312106"),
        ("sshPrnamt", "92826C839"),
        ("putCall", "404280406"),
        ("cusip", "0378331000"),
        ("cusip", None),
    ]


def test_warnings_account_for_every_row_the_document_contained(
    modern: InformationTable,
) -> None:
    """The arithmetic the loader runs against ``tableEntryTotal``.

    Rows returned plus rows dropped equals rows filed. A shortfall that this
    does not explain is a parser bug or a truncated document, and either way it
    must not be loaded.
    """
    filed = load("modern").count(b"<it:infoTable>")
    dropped = sum(1 for w in modern.warnings if w.dropped)

    assert len(modern.rows) + dropped == filed


def test_warning_positions_are_where_the_row_is(modern: InformationTable) -> None:
    """1-based document order, so the row can be found in the raw file."""
    assert [w.row for w in modern.warnings] == [7, 8, 9, 10, 11, 12]


def test_absent_element_is_distinguishable_from_an_unreadable_one(
    modern: InformationTable,
) -> None:
    """Different bugs: absent is usually a filer, malformed is usually us."""
    absent = next(w for w in modern.warnings if w.field == "sshPrnamt")
    unreadable = next(w for w in modern.warnings if w.field == "value")

    assert absent.value is None
    assert unreadable.value == "N/A"


# --- one row, broken in one way ----------------------------------------------

# A single well-formed row that each case below corrupts in exactly one place.
# A template rather than more fixture files, for the reason given in
# test_primary_doc.py: the defect is legible in a diff of two strings and
# invisible in a diff of two forty-line documents.
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>{issuer}</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>{cusip}</cusip>
    <value>{value}</value>
    <shrsOrPrnAmt>
      <sshPrnamt>{shares}</sshPrnamt>
      <sshPrnamtType>{sh_prn_type}</sshPrnamtType>
    </shrsOrPrnAmt>
    {put_call}
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>
"""


def build(**overrides: str) -> bytes:
    defaults = {
        "issuer": "EXAMPLE CORP",
        "cusip": "037833100",
        "value": "1200000",
        "shares": "10000",
        "sh_prn_type": "SH",
        "put_call": "",
    }
    return TEMPLATE.format(**{**defaults, **overrides}).encode()


def test_the_template_itself_parses() -> None:
    """Without this, a broken template makes every case below pass for free."""
    table = parse_information_table(build())

    assert table.warnings == ()
    assert table.rows[0].value == Decimal(1200000)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"value": "N/A"}, "value", id="value-not-a-number"),
        pytest.param({"value": ""}, "value", id="value-empty"),
        pytest.param({"value": "-1200000"}, "value", id="value-negative"),
        pytest.param({"value": "NaN"}, "value", id="value-nan"),
        pytest.param({"value": "Infinity"}, "value", id="value-infinite"),
        pytest.param({"shares": "-10000"}, "sshPrnamt", id="shares-negative"),
        pytest.param({"shares": ""}, "sshPrnamt", id="shares-empty"),
        pytest.param({"sh_prn_type": "SHARES"}, "sshPrnamtType", id="unit-unknown"),
        pytest.param({"sh_prn_type": ""}, "sshPrnamtType", id="unit-empty"),
        pytest.param({"cusip": ""}, "cusip", id="cusip-empty"),
        pytest.param({"cusip": "0378331000"}, "cusip", id="cusip-too-long"),
        pytest.param({"cusip": "037833/00"}, "cusip", id="cusip-not-alphanumeric"),
        pytest.param({"put_call": "<putCall>STRADDLE</putCall>"}, "putCall", id="option-unknown"),
    ],
)
def test_a_broken_row_is_dropped_with_its_reason(overrides: dict[str, str], field: str) -> None:
    """One warning, naming the element as EDGAR spells it, and no row."""
    table = parse_information_table(build(**overrides))

    assert table.rows == ()
    assert len(table.warnings) == 1
    assert table.warnings[0].field == field
    assert table.warnings[0].dropped is True


def test_nan_never_reaches_a_row() -> None:
    """The most expensive value in the file, and it parses without complaint.

    One NaN in a numeric column makes ``SUM`` return NaN for the whole
    portfolio, silently and for good.
    """
    table = parse_information_table(build(value="NaN"))

    assert table.rows == ()
    assert "finite" in table.warnings[0].reason


def test_negative_quantities_are_refused() -> None:
    """13F is long-only. A minus sign here is an export bug, not a short."""
    table = parse_information_table(build(value="-1200000"))

    assert table.warnings[0].reason == "expected a non-negative number"


def test_thousands_separators_are_accepted() -> None:
    """Filing agents emit them and ``1,200,000`` is not ambiguous."""
    assert parse_information_table(build(value="1,200,000")).rows[0].value == Decimal(1200000)


def test_a_broken_row_does_not_take_its_neighbours_with_it() -> None:
    """The reason this parser reports instead of raising.

    One malformed row in a 3,000-row filing must not cost the other 2,999, and
    must not pass unmentioned either.
    """
    document = load("modern")
    table = parse_information_table(document)

    assert len(table.rows) == 7
    assert sum(1 for w in table.warnings if w.dropped) == 5


# --- namespaces --------------------------------------------------------------


def test_an_unfamiliar_namespace_parses(modern: InformationTable) -> None:
    """The fixture's URI is not one EDGAR serves, and it is prefixed.

    A parser that pins the namespace does not fail loudly on a document it
    cannot read — it matches nothing and returns an empty portfolio, which
    reads as a manager who sold everything.
    """
    assert len(modern.rows) == 7


def test_a_document_with_no_namespace_at_all_parses() -> None:
    plain = b"""<informationTable>
      <infoTable>
        <nameOfIssuer>EXAMPLE CORP</nameOfIssuer>
        <cusip>037833100</cusip>
        <value>1200000</value>
        <shrsOrPrnAmt><sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""

    assert parse_information_table(plain).rows[0].cusip == "037833100"


# --- documents that are not tables -------------------------------------------


def test_a_document_with_no_rows_is_empty_and_not_an_error() -> None:
    """A 13F NOTICE holds nothing itself. Valid filing, empty holdings set."""
    table = parse_information_table(
        b'<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"/>'
    )

    assert table.rows == ()
    assert table.warnings == ()
    assert table.value_total == Decimal(0)


def test_a_truncated_document_raises_rather_than_returning_a_prefix() -> None:
    """The failure this parser is most likely to meet, and the one row-level
    tolerance must not extend to.

    A cut-off download yields perfectly good rows right up to the cut. Returning
    them loads a portfolio that is a *prefix* of the real one — every position
    correct, the total wrong — which is the exact shape of "the fund sold half
    its book".
    """
    whole = load("berkshire")

    with pytest.raises(FilingParseError) as caught:
        parse_information_table(whole[: len(whole) // 2])

    assert caught.value.field == "xml"


def test_empty_input_raises() -> None:
    with pytest.raises(FilingParseError):
        parse_information_table(b"")


def test_the_wrong_document_yields_nothing_rather_than_guessing() -> None:
    """A cover page has no ``<infoTable>`` in it. Nothing here should invent one."""
    cover = (FIXTURES / "primary_doc_original.xml").read_bytes()

    assert parse_information_table(cover).rows == ()


def test_external_entities_are_not_dereferenced(tmp_path: Path) -> None:
    """``iterparse`` resolves entities *by default*, unlike ``fromstring``.

    So this is not belt and braces: without the explicit setting, a document
    from EDGAR could make this parser read a local file or open a socket, inside
    a function whose whole contract is that it performs no I/O.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("MOAT CAPITAL LP")

    hostile = f"""<?xml version="1.0"?>
<!DOCTYPE informationTable [<!ENTITY xxe SYSTEM "file://{secret}">]>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>&xxe;</nameOfIssuer>
    <cusip>037833100</cusip>
    <value>1200000</value>
    <shrsOrPrnAmt><sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>
""".encode()

    # Either outcome is fine — refusing the document, or parsing it with the
    # entity unresolved. What must not happen is the file's contents appearing.
    try:
        table = parse_information_table(hostile)
    except FilingParseError:
        return
    assert all(row.name_of_issuer != "MOAT CAPITAL LP" for row in table.rows)


# --- a filing the size of a real one -----------------------------------------

LARGE_ROWS = 3000


def large_table(rows: int = LARGE_ROWS) -> bytes:
    """A filing the size an index manager or Bridgewater actually files.

    Generated rather than committed. The CUSIPs are synthetic but distinct and
    nine characters wide, and every row carries the full field set, which is
    what makes the row count rather than the document's shape the variable.
    """
    body = "\n".join(
        f"""  <infoTable>
    <nameOfIssuer>ISSUER {index:04d} INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>{index:09d}</cusip>
    <value>{index * 1000}</value>
    <shrsOrPrnAmt>
      <sshPrnamt>{index * 10}</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>{index * 10}</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>"""
        for index in range(1, rows + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">\n'
        f"{body}\n</informationTable>\n"
    ).encode()


def test_three_thousand_rows_all_arrive() -> None:
    """Nothing about the parser is per-filing-size, and this proves it once."""
    table = parse_information_table(large_table())

    assert len(table.rows) == LARGE_ROWS
    assert table.warnings == ()
    assert table.rows[0].cusip == "000000001"
    assert table.rows[-1].cusip == "000003000"


def test_three_thousand_values_sum_exactly() -> None:
    """Decimal over 3,000 rows, against the arithmetic done in integers.

    The number this produces is what a filing's ``tableValueTotal`` gets
    compared against, so an accumulated rounding error here is a checksum
    failure on a filing that is perfectly fine.
    """
    expected = Decimal(sum(index * 1000 for index in range(1, LARGE_ROWS + 1)))

    assert parse_information_table(large_table()).value_total == expected


def test_memory_does_not_grow_with_the_size_of_the_filing() -> None:
    """The reason for ``iterparse`` — asserted on the tree, not on a memory number.

    Peak RSS would be the honest measurement and a useless test: it is noisy,
    and even a 12,000-row document fits in memory, so the non-streaming version
    passes it too. What actually separates the two is whether finished rows stay
    attached to their parent, so that is what is measured — how many rows the
    tree holds at once, at two document sizes twenty-four times apart.

    The bound is libxml2's read buffer rather than anything this module chooses,
    which is why the assertion is that the two sizes agree rather than that the
    number is any particular value. A parser that kept its rows would report 500
    and 12,000 here.

    It matters during a backfill rather than in this test: filings are parsed
    concurrently, and the largest ones arrive alongside all the others.
    """

    def rows_held_at_once(count: int) -> int:
        widths = []
        for element in _info_tables(large_table(count)):
            parent = element.getparent()
            assert parent is not None
            widths.append(len(parent))
        assert len(widths) == count
        return max(widths)

    small, large = rows_held_at_once(500), rows_held_at_once(12000)

    assert small == large
    assert large < 500
