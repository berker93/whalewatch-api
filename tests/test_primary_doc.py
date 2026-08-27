"""Tests for the 13F cover-page parser.

The four fixtures in ``tests/fixtures/thirteen_f`` are the four shapes this
document comes in — an original, a restatement amendment, a new-holdings
amendment, and one that withholds positions under confidential treatment — and
each is asserted field by field rather than against a single expected object.
A field-by-field assertion names the field that broke in the failure output,
which for a parser is most of the diagnosis.

``primary_doc_restatement.xml`` carries a namespace URI EDGAR has never served.
That is on purpose: it is the fixture that fails if anyone replaces the
``local-name()`` lookups with a hardcoded namespace, which is a change that
otherwise passes every other test here.
"""

from datetime import date
from pathlib import Path

import pytest

from app.db.models.enums import AmendmentKind
from app.ingestion.parsers.errors import FilingParseError
from app.ingestion.parsers.thirteen_f import PrimaryDoc, parse_primary_doc

FIXTURES = Path(__file__).parent / "fixtures" / "thirteen_f"


def load(name: str) -> bytes:
    """Read a fixture as bytes, which is what the parser takes and why."""
    return (FIXTURES / f"primary_doc_{name}.xml").read_bytes()


@pytest.fixture(scope="module")
def original() -> PrimaryDoc:
    return parse_primary_doc(load("original"))


@pytest.fixture(scope="module")
def restatement() -> PrimaryDoc:
    return parse_primary_doc(load("restatement"))


@pytest.fixture(scope="module")
def new_holdings() -> PrimaryDoc:
    return parse_primary_doc(load("new_holdings"))


@pytest.fixture(scope="module")
def confidential() -> PrimaryDoc:
    return parse_primary_doc(load("confidential"))


# --- the original filing -----------------------------------------------------


def test_original_identifies_the_filing(original: PrimaryDoc) -> None:
    assert original.cik == "0001067983"
    assert original.filer_name == "Berkshire Hathaway Inc"
    assert original.form_type == "13F-HR"
    assert original.report_type == "13F HOLDINGS REPORT"


def test_original_reads_the_period_not_the_signature_date(original: PrimaryDoc) -> None:
    """The two dates are 45 days apart and swapping them is silent.

    Both are asserted in one test because the failure worth catching is not
    "the period is wrong", it is "the period is the signature date" — which
    only a test that knows both values can see.
    """
    assert original.period_of_report == date(2024, 3, 31)
    assert original.signature_date == date(2024, 5, 15)


def test_original_is_not_an_amendment(original: PrimaryDoc) -> None:
    assert original.amendment_kind is None
    assert original.amendment_no is None


def test_original_carries_the_summary_page_checksum(original: PrimaryDoc) -> None:
    assert original.table_entry_total == 41
    assert original.table_value_total == 331661298
    assert original.other_included_managers_count == 3
    assert original.confidential_omitted is False


def test_filer_name_is_the_manager_not_the_signatory(original: PrimaryDoc) -> None:
    """``<name>`` appears twice in the document and only one of them is a filer.

    The other is the officer in the signature block. An unscoped lookup returns
    whichever comes first in document order, so this passes by accident today
    and would keep passing right up until a schema revision reorders the form.
    """
    assert original.filer_name != "Marc D. Hamburg"


# --- the two kinds of amendment ----------------------------------------------


def test_restatement_is_read_as_a_restatement(restatement: PrimaryDoc) -> None:
    assert restatement.amendment_kind is AmendmentKind.RESTATEMENT
    assert restatement.amendment_no == 1
    assert restatement.form_type == "13F-HR/A"


def test_new_holdings_is_read_as_an_addition(new_holdings: PrimaryDoc) -> None:
    assert new_holdings.amendment_kind is AmendmentKind.NEW_HOLDINGS
    assert new_holdings.amendment_no == 2
    assert new_holdings.form_type == "13F-HR/A"


def test_the_two_amendment_kinds_do_not_collapse(
    restatement: PrimaryDoc, new_holdings: PrimaryDoc
) -> None:
    """The assertion this whole module exists for.

    Both amendments above are well-formed 13F-HR/A documents that differ in one
    element. Read them the same way and a restatement gets loaded alongside the
    original it replaces — every position counted twice — or a new-holdings
    amendment wipes the period it was meant to add to. Neither raises anything;
    both produce a portfolio that looks entirely plausible.
    """
    assert restatement.amendment_kind is not new_holdings.amendment_kind


def test_restatement_parses_despite_an_unfamiliar_namespace(
    restatement: PrimaryDoc,
) -> None:
    """The fixture's namespace URI is not one EDGAR serves. Nothing may care."""
    assert restatement.cik == "0001350694"
    assert restatement.filer_name == "Bridgewater Associates, LP"
    assert restatement.period_of_report == date(2023, 6, 30)
    assert restatement.table_entry_total == 793


# --- confidential treatment --------------------------------------------------


def test_confidential_omission_is_flagged(confidential: PrimaryDoc) -> None:
    """``True`` here means the filing is knowingly incomplete.

    The withheld positions arrive later in a NEW HOLDINGS amendment dated to
    this same period, which is why the flag has to survive the parse rather
    than be inferred from a later filing that may not have arrived yet.
    """
    assert confidential.confidential_omitted is True
    assert confidential.report_type == "13F COMBINATION REPORT"
    assert confidential.amendment_kind is None


def test_confidential_filing_still_reports_its_own_totals(
    confidential: PrimaryDoc,
) -> None:
    """The checksum covers what was filed, not what was held."""
    assert confidential.table_entry_total == 28
    assert confidential.table_value_total == 4113920000
    assert confidential.other_included_managers_count == 2


def test_confidential_original_and_its_amendment_share_a_period(
    confidential: PrimaryDoc, new_holdings: PrimaryDoc
) -> None:
    """The pair is the reason ingestion cannot be write-once.

    Two filings, months apart, describing the same quarter for the same filer.
    Anything that treats a period as final once loaded loses the second one.
    """
    assert confidential.period_of_report == new_holdings.period_of_report
    assert confidential.cik == new_holdings.cik
    assert confidential.signature_date != new_holdings.signature_date


# --- the result is frozen ----------------------------------------------------


def test_result_cannot_be_edited(original: PrimaryDoc) -> None:
    """A parse result that can be patched is a value no re-parse reproduces."""
    with pytest.raises(Exception, match="frozen"):
        original.table_entry_total = 0  # type: ignore[misc]


# --- malformed input ---------------------------------------------------------

# A minimal well-formed cover page, namespaced like the real thing, that each
# test below breaks in exactly one way. Built as a template rather than as more
# fixture files because the point of each case is the single element it
# corrupts, and that is legible in a diff of two strings but not in a diff of
# two forty-line documents.
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>{submission_type}</submissionType>
    <filerInfo>
      <filer><credentials><cik>{cik}</cik></credentials></filer>
      <periodOfReport>{period}</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      {amendment}
      <filingManager><name>{manager}</name></filingManager>
      <reportType>13F HOLDINGS REPORT</reportType>
    </coverPage>
    <summaryPage>
      <tableEntryTotal>{entries}</tableEntryTotal>
      <tableValueTotal>1000</tableValueTotal>
      <isConfidentialOmitted>{confidential}</isConfidentialOmitted>
    </summaryPage>
  </formData>
</edgarSubmission>
"""


def build(**overrides: str) -> bytes:
    defaults = {
        "submission_type": "13F-HR",
        "cik": "0001067983",
        "period": "03-31-2024",
        "amendment": "<isAmendment>false</isAmendment>",
        "manager": "Example Capital LP",
        "entries": "12",
        "confidential": "false",
    }
    return TEMPLATE.format(**{**defaults, **overrides}).encode()


def test_the_template_itself_parses() -> None:
    """Without this, a broken template makes every case below pass for free."""
    parsed = parse_primary_doc(build())
    assert parsed.cik == "0001067983"
    assert parsed.table_entry_total == 12
    assert parsed.signature_date is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"period": ""}, "periodOfReport", id="period-empty"),
        pytest.param({"period": "31-03-2024"}, "periodOfReport", id="period-day-first"),
        pytest.param({"period": "Q1 2024"}, "periodOfReport", id="period-not-a-date"),
        pytest.param({"cik": ""}, "cik", id="cik-empty"),
        pytest.param({"cik": "BERKSHIRE"}, "cik", id="cik-not-digits"),
        pytest.param({"cik": "123456789012"}, "cik", id="cik-too-wide"),
        pytest.param({"manager": ""}, "name", id="manager-empty"),
        pytest.param({"submission_type": ""}, "submissionType", id="form-type-empty"),
        pytest.param({"entries": "forty one"}, "tableEntryTotal", id="total-not-a-number"),
        pytest.param({"entries": "-1"}, "tableEntryTotal", id="total-negative"),
        pytest.param({"confidential": "maybe"}, "isConfidentialOmitted", id="bool-unknown"),
        pytest.param(
            {"amendment": "<amendmentInfo><amendmentType>PARTIAL</amendmentType></amendmentInfo>"},
            "amendmentType",
            id="amendment-kind-unknown",
        ),
    ],
)
def test_malformed_field_raises_naming_itself(overrides: dict[str, str], field: str) -> None:
    """Every failure names the element, spelled as EDGAR spells it.

    The name is the whole value of the exception. It lands in
    ``filing.parse_error`` and is the difference between "re-read this document"
    and "look at this line of it".
    """
    with pytest.raises(FilingParseError) as caught:
        parse_primary_doc(build(**overrides))
    assert caught.value.field == field
    assert field in str(caught.value)


def test_absent_element_is_distinguishable_from_an_unreadable_one() -> None:
    """``value`` is ``None`` for a missing element and the text for a bad one.

    Different bugs: a missing element usually means the wrong document was
    fetched, a malformed one means the parser is behind a schema change.
    """
    with pytest.raises(FilingParseError) as missing:
        parse_primary_doc(build(period=""))
    assert missing.value.value is None

    with pytest.raises(FilingParseError) as unreadable:
        parse_primary_doc(build(period="Q1 2024"))
    assert unreadable.value.value == "Q1 2024"


def test_truncated_document_raises_rather_than_returning_partial_fields() -> None:
    """A download cut short is the failure this parser is most likely to meet."""
    whole = load("original")
    with pytest.raises(FilingParseError) as caught:
        parse_primary_doc(whole[: len(whole) // 2])
    assert caught.value.field == "xml"


def test_empty_input_raises() -> None:
    with pytest.raises(FilingParseError):
        parse_primary_doc(b"")


# --- tolerated variation -----------------------------------------------------
#
# Each of these is a spelling filing agents actually emit. They are tolerated
# rather than rejected because none of them is ambiguous, and rejecting an
# unambiguous value fails an ingest over punctuation.


@pytest.mark.parametrize("spelling", ["true", "TRUE", "Y", "1"])
def test_confidential_flag_accepts_the_spellings_agents_use(spelling: str) -> None:
    assert parse_primary_doc(build(confidential=spelling)).confidential_omitted is True


@pytest.mark.parametrize("spelling", ["false", "FALSE", "N", "0"])
def test_confidential_flag_reads_every_no_as_no(spelling: str) -> None:
    assert parse_primary_doc(build(confidential=spelling)).confidential_omitted is False


def test_iso_dates_are_accepted() -> None:
    """A minority of older documents use YYYY-MM-DD, which cannot be confused."""
    assert parse_primary_doc(build(period="2024-03-31")).period_of_report == date(2024, 3, 31)


def test_thousands_separators_in_totals_are_accepted() -> None:
    assert parse_primary_doc(build(entries="1,234")).table_entry_total == 1234


def test_unpadded_cik_is_padded() -> None:
    """EDGAR's own URLs disagree about padding; the database does not."""
    assert parse_primary_doc(build(cik="1067983")).cik == "0001067983"


@pytest.mark.parametrize("spelling", ["NEW HOLDINGS", "New Holdings", "NEW  HOLDINGS"])
def test_amendment_type_tolerates_case_and_spacing(spelling: str) -> None:
    amendment = f"<amendmentInfo><amendmentType>{spelling}</amendmentType></amendmentInfo>"
    parsed = parse_primary_doc(build(amendment=amendment))
    assert parsed.amendment_kind is AmendmentKind.NEW_HOLDINGS


# --- absent optional data ----------------------------------------------------


def test_a_notice_without_a_summary_page_parses() -> None:
    """A 13F NOTICE reports nothing, so it has nothing to summarise.

    Valid filing, empty holdings set, no checksum available. The totals come
    back ``None`` and not zero, because a caller comparing them against a
    parsed information table has to be able to tell "no rows" from "no declared
    total" — zero would silently satisfy a check that was never made.
    """
    notice = b"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>13F-NT</submissionType>
    <filerInfo>
      <filer><credentials><cik>0001067983</cik></credentials></filer>
      <periodOfReport>03-31-2024</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <isAmendment>false</isAmendment>
      <filingManager><name>Example Capital LP</name></filingManager>
      <reportType>13F NOTICE</reportType>
    </coverPage>
  </formData>
</edgarSubmission>
"""

    parsed = parse_primary_doc(notice)

    assert parsed.form_type == "13F-NT"
    assert parsed.table_entry_total is None
    assert parsed.table_value_total is None
    assert parsed.other_included_managers_count is None
    assert parsed.confidential_omitted is False


def test_self_closing_elements_read_as_absent_not_as_empty() -> None:
    """Agents emit ``<amendmentNo/>`` for fields they have nothing to put in."""
    parsed = parse_primary_doc(build(amendment="<amendmentNo/><isAmendment>false</isAmendment>"))
    assert parsed.amendment_no is None
    assert parsed.amendment_kind is None


# --- no I/O ------------------------------------------------------------------


def test_external_entities_are_not_dereferenced(tmp_path: Path) -> None:
    """The parser's contract is that it performs no I/O. Input is from the web.

    A document declaring an external entity can have the parser fetch it —
    reading a local file, or opening a connection to a host the document names.
    Current lxml declines to by default, so this passes today on the default
    settings too; it is here to fail the day someone passes
    ``resolve_entities=True`` to make some other document parse, which does leak
    the file below.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("Chief Investment Officer")

    hostile = f"""<?xml version="1.0"?>
<!DOCTYPE edgarSubmission [<!ENTITY xxe SYSTEM "file://{secret}">]>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>13F-HR</submissionType>
    <filerInfo>
      <filer><credentials><cik>0001067983</cik></credentials></filer>
      <periodOfReport>03-31-2024</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <filingManager><name>&xxe;</name></filingManager>
    </coverPage>
  </formData>
</edgarSubmission>
""".encode()

    # Either outcome is acceptable — refusing the document, or parsing it with
    # the entity left unresolved. What must not happen is the file's contents
    # appearing in the result.
    try:
        parsed = parse_primary_doc(hostile)
    except FilingParseError:
        return
    assert "Chief Investment Officer" not in parsed.filer_name
