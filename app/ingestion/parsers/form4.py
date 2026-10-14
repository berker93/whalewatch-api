"""Form 4 ownership XML parser.

One document per filing, ``ownershipDocument``: who the issuer is, who is
reporting, and two tables of what they did. Simpler than a 13F in structure and
harder in meaning. The structure is a dozen elements; the meaning is in
``transactionCode`` and in the footnotes, and a parser that reads the first and
drops the second produces a feed where a pre-scheduled plan sale and a CEO
dumping stock look the same.

What comes out
--------------
:func:`parse_form4` returns a :class:`Form4Document`: the issuer, every
reporting owner in document order, and every *transaction* line, numbered the
way ``insider_transaction.line_number`` is (non-derivative table first, then
derivative, from 1). The ``...Holding`` lines, which report a balance and no
trade, are skipped and take no number, so a holding line added to an amendment
does not renumber the trades around it.

Each transaction carries the footnotes it cites, with their text, because that
is where Form 4 says what its fields cannot: the price range behind a weighted
average, a 10b5-1 plan, shares held through a trust.

Purity
------
Bytes in, value out, no network, database or clock, for the reason the 13F
parser gives: a parser bug is repaired by re-running it over ``raw_document``.
Nothing here decides which owner a transaction belongs to, or which security an
issuer is. That is the loader's (INS-4), which knows what is in the database.

Namespaces
----------
Form 4 XML is namespace-free in practice, and every one of a week's 280 queued
filings was. Element lookups still match on the local name, the same immunity
the 13F parser buys: a document that does carry a namespace parses instead of
coming back empty.

Failure
-------
Unlike the 13F information table, a bad line fails the whole document
(:class:`~app.ingestion.parsers.errors.FilingParseError`). A 13F has a checksum
that accounts for a dropped row, and 3,000 rows to lose if one row sinks it. A
Form 4 has neither: it is a handful of lines, EDGAR validates it against a
schema on submission, and a line dropped from it is a trade that silently never
happened. A filing that fails is recorded in ``filing.parse_error`` and re-run
once the parser is fixed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from lxml import etree
from pydantic import BaseModel, ConfigDict

from app.ingestion.parsers.errors import FilingParseError

#: The two document types this parser accepts. Forms 3 and 5 use the same
#: ``ownershipDocument`` schema, and parsing one as a Form 4 would load an
#: initial holdings statement as a day of trades.
FORM_TYPES: Final = frozenset({"4", "4/A"})

#: See the 13F parser's ``_TRUE_SPELLINGS``. Form 4 agents write ``1``/``0``
#: and ``true``/``false`` in about equal measure.
_TRUE_SPELLINGS: Final = frozenset({"true", "t", "y", "yes", "1"})
_FALSE_SPELLINGS: Final = frozenset({"false", "f", "n", "no", "0"})

#: Zero-padded width of a CIK, matching ``CHAR(10)`` on ``insider`` and
#: ``insider_transaction``.
_CIK_WIDTH: Final = 10

#: What filers write in ``issuerTradingSymbol`` for an issuer with no ticker.
_NO_TICKER: Final = frozenset({"NONE", "N/A", "NA", "-", "NOT APPLICABLE"})

#: The hyphen, the Unicode hyphen, the en dash and the em dash: what a word
#: processor turns "10b5-1" and "$41.10-$41.50" into before a filer pastes them.
_DASHES: Final = "-\u2010\u2013\u2014"

#: A Rule 10b5-1 reference, however it is spelled: "Rule 10b5-1", "10b-5-1",
#: "10b51", any of them with one of :data:`_DASHES`. The trailing ``(?!\d)`` keeps "Rule 10b-5", the
#: antifraud rule that has nothing to do with plans, from matching when a
#: number happens to follow it.
_RULE_10B5_1: Final = re.compile(
    rf"10\s*b\s*[{_DASHES}]?\s*5\s*[{_DASHES}]?\s*1(?!\d)", re.IGNORECASE
)

#: Words that turn a sentence about a 10b5-1 plan into one that says a trade
#: was *not* under one: "These sales were not made pursuant to a Rule 10b5-1
#: plan." None of a week's 66 plan footnotes said so, and a filer who does say
#: it is saying the opposite of what the regex above found.
_NEGATION: Final = re.compile(r"\b(?:not|no longer|outside(?: of)?)\b", re.IGNORECASE)

#: A price range: "$53.66 to $54.58", "$26.25 - $26.80", "between $41.10 and
#: $41.50". The second dollar sign is optional, the first is not, which keeps
#: "NT$31.795 to US$1" (an exchange rate, in a real TSMC footnote) from reading
#: as a range of $31.795 to $1.
_PRICE_RANGE: Final = re.compile(
    rf"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(?:to|and|through|[{_DASHES}])\s*\$?\s*(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


class Issuer(BaseModel):
    """The company whose securities were traded."""

    model_config = ConfigDict(frozen=True)

    cik: str
    """``issuerCik``, zero-padded to ten characters."""

    name: str
    """``issuerName`` as filed."""

    trading_symbol: str | None
    """``issuerTradingSymbol``, stripped and uppercased. ``None`` when blank or
    one of the "no ticker" spellings (``NONE``, ``N/A``). Otherwise as filed:
    "BRK.B", and occasionally two tickers in one string for a dual-class
    issuer. Evidence for the security link, not a key."""


class ReportingOwner(BaseModel):
    """One ``reportingOwner``: who is filing, and what they are to the issuer.

    The roles are booleans because they are not exclusive. A CEO who chairs the
    board checks both ``isDirector`` and ``isOfficer``.
    """

    model_config = ConfigDict(frozen=True)

    cik: str
    """``rptOwnerCik``, zero-padded. The insider's natural key."""

    name: str
    """``rptOwnerName`` as filed, usually surname first ("BUFFETT WARREN E")."""

    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    """``isDirector``, ``isOfficer``, ``isTenPercentOwner``, ``isOther``.
    Absent reads as ``False``: filers omit the boxes they leave unchecked."""

    officer_title: str | None
    """``officerTitle``, free text: "Chief Executive Officer", "EVP & CFO".
    Not checked against :attr:`is_officer`; filers write one without the
    other."""

    other_text: str | None
    """``otherText``: what "other" means, when it is checked."""


class Form4Transaction(BaseModel):
    """One ``nonDerivativeTransaction`` or ``derivativeTransaction``.

    Shaped after ``insider_transaction``: everything the loader needs for a row
    is here, under the column's name. The derivative-only fields at the end are
    extra, kept because a line of the derivative table is unreadable without
    them.
    """

    model_config = ConfigDict(frozen=True)

    line_number: int
    """1-based position among the document's transactions, non-derivative
    table first. The row identity the source does not have; see
    ``insider_transaction.line_number``."""

    is_derivative: bool
    """``True`` for a line of ``derivativeTable``: options, RSUs, warrants,
    convertibles. :attr:`shares` then counts the derivative, and
    :attr:`price_per_share` is what was paid for the derivative itself, which
    for an exercise or a grant is usually nothing. The strike is
    :attr:`conversion_or_exercise_price`."""

    security_title: str | None
    """``securityTitle`` as filed: "Common Stock", "Stock Option (Right to
    Buy)", "Restricted Stock Units - 10-10-2024"."""

    transaction_date: date
    """``transactionDate``. Required: it is in the table's key."""

    transaction_code: str
    """``transactionCode``, one uppercase letter, uninterpreted. P is an
    open-market purchase, S a sale, A a grant, M an exercise, F shares withheld
    for tax, G a gift, C a conversion; the SEC defines the rest."""

    acquired_disposed: str
    """``transactionAcquiredDisposedCode``: ``'A'`` or ``'D'``."""

    shares: Decimal | None
    """``transactionShares``, unsigned. ``None`` when the line reports a value
    instead (``transactionTotalValue``, for some derivatives)."""

    price_per_share: Decimal | None
    """``transactionPricePerShare`` in dollars, or ``None``. **Never zero**:
    filers write ``0`` for a grant, a gift or an RSU vesting, and a zero price
    averages into "insiders bought at $0". ``None`` too when the element is
    absent or carries only a footnote, which is how a price that exists only as
    a range is reported."""

    price_low: Decimal | None
    price_high: Decimal | None
    """The range behind a weighted-average price, when the price's own
    footnotes give one ("sold in multiple transactions at prices ranging from
    $53.66 to $54.58"). Also filled, with :attr:`price_per_share` left
    ``None``, in the rare document whose price value is itself a range. Both or
    neither."""

    shares_owned_after: Decimal | None
    """``sharesOwnedFollowingTransaction``: the holding of this security, under
    this :attr:`direct_or_indirect`, after the trade."""

    direct_or_indirect: str | None
    """``directOrIndirectOwnership``: ``'D'`` or ``'I'``. An indirect holding is
    through a trust, a family member or a fund, and
    :attr:`nature_of_ownership` says which. :attr:`shares_owned_after` is the
    balance of that one holding, not of everything the insider owns."""

    nature_of_ownership: str | None
    """``natureOfOwnership``, free text: "By Trust", "See Footnotes"."""

    is_10b5_1: bool
    """The trade was made under a Rule 10b5-1 plan, adopted months earlier,
    and says nothing about what the insider thinks today.

    From a footnote this line cites, when one says so. Failing that, from the
    form's ``aff10b5One`` checkbox, but only when no line of the filing cites a
    plan footnote: the checkbox is one per form ("a transaction was made
    pursuant to..."), and when the footnotes say which lines it means, the
    other lines are not plan trades. A form that ticks the box and footnotes
    nothing gets every line flagged, because there is nothing to tell them
    apart. See :func:`_flag_plan_trades`."""

    footnotes: dict[str, str]
    """The footnotes this line cites anywhere inside it (price, shares,
    ownership nature...), by the filing's own ids, in first-cited order. Text
    with whitespace collapsed. Empty when it cites none. An id the document
    cites but never defines is left out."""

    conversion_or_exercise_price: Decimal | None
    """Derivative lines: ``conversionOrExercisePrice``, the strike. ``None``
    for a zero, as for :attr:`price_per_share`: an RSU has no strike, and
    filers write ``0``."""

    underlying_security_title: str | None
    underlying_shares: Decimal | None
    """Derivative lines: ``underlyingSecurity``, what the derivative converts
    into and how many shares of it. ``None`` on non-derivative lines."""


class Form4Document(BaseModel):
    """A parsed Form 4 or 4/A.

    Frozen, and not a ``Filing``, for the reasons
    :class:`~app.ingestion.parsers.thirteen_f.PrimaryDoc` gives: this is what
    the document says, and the accession number and filing time come from the
    index.
    """

    model_config = ConfigDict(frozen=True)

    document_type: str
    """``4`` or ``4/A``, from ``documentType``."""

    period_of_report: date
    """``periodOfReport``: the earliest transaction date on the form, by the
    SEC's definition. Not the filing date."""

    date_of_original_submission: date | None
    """``dateOfOriginalSubmission``, on a 4/A: when the Form 4 it amends was
    filed. The document names no accession number, so this and the issuer and
    owner are what the loader has to find the original by."""

    not_subject_to_section_16: bool
    """``notSubjectToSection16``: the box a former insider ticks to say the
    filing is voluntary."""

    aff_10b5_1: bool
    """``aff10b5One``, the form's Rule 10b5-1 checkbox, as filed. On the form
    since April 2023; ``False`` when absent, as on every earlier filing. The
    per-line verdict is :attr:`Form4Transaction.is_10b5_1`."""

    issuer: Issuer

    owners: tuple[ReportingOwner, ...]
    """Every reporting owner, in document order, at least one. A joint filing
    names several (a fund, its general partner, the people behind both) and
    reports its transactions once for all of them."""

    transactions: tuple[Form4Transaction, ...]
    """Every transaction line, in :attr:`Form4Transaction.line_number` order.
    Empty for a form that reports only holdings."""

    footnotes: dict[str, str]
    """Every footnote the document defines, by id, including the ones no
    transaction cites (an owner's or the remarks')."""

    remarks: str | None
    """``remarks``, whitespace collapsed. A 4/A usually says here what it
    corrects."""


def parse_form4(xml: bytes) -> Form4Document:
    """Parse a Form 4 ``ownershipDocument`` into a :class:`Form4Document`.

    :param xml: The document exactly as EDGAR served it. Bytes, for the reason
        given on :func:`~app.ingestion.parsers.thirteen_f.parse_primary_doc`.
    :raises FilingParseError: If the document is not well-formed XML, is not a
        Form 4 or 4/A, lacks an issuer or reporting owner, or has a transaction
        whose date, code or acquired/disposed flag is missing or unreadable.
        The exception names the element and, for a transaction, its line.
    """
    root = _parse(xml)

    document_type = _required_text(root, "documentType")
    if document_type not in FORM_TYPES:
        raise FilingParseError(
            field="documentType", reason="expected 4 or 4/A", value=document_type
        )

    footnotes = {
        note_id: text
        for note in _descendants(root, "footnote")
        if (note_id := (note.get("id") or "").strip()) and (text := _collapse(note)) is not None
    }

    owners = tuple(_owner(element) for element in _children(root, "reportingOwner"))
    if not owners:
        raise FilingParseError(field="reportingOwner", reason="is required but was missing")

    aff_10b5_1 = _optional_bool(_child_text(root, "aff10b5One"), "aff10b5One")
    transactions = _flag_plan_trades(list(_transactions(root, footnotes)), checkbox=aff_10b5_1)

    remarks_element = _child(root, "remarks")
    return Form4Document(
        document_type=document_type,
        period_of_report=_as_date(_required_text(root, "periodOfReport"), "periodOfReport"),
        date_of_original_submission=_optional_date(
            _child_text(root, "dateOfOriginalSubmission"), "dateOfOriginalSubmission"
        ),
        not_subject_to_section_16=_optional_bool(
            _child_text(root, "notSubjectToSection16"), "notSubjectToSection16"
        ),
        aff_10b5_1=aff_10b5_1,
        issuer=_issuer(root),
        owners=owners,
        transactions=tuple(transactions),
        footnotes=footnotes,
        remarks=None if remarks_element is None else _collapse(remarks_element),
    )


# --- the parts ---------------------------------------------------------------


def _issuer(root: etree._Element) -> Issuer:
    issuer = _child(root, "issuer")
    if issuer is None:
        raise FilingParseError(field="issuer", reason="is required but was missing")
    symbol = _child_text(issuer, "issuerTradingSymbol")
    if symbol is not None:
        symbol = symbol.upper()
        if symbol in _NO_TICKER:
            symbol = None
    return Issuer(
        cik=_padded_cik(_required_text(issuer, "issuerCik"), "issuerCik"),
        name=_required_text(issuer, "issuerName"),
        trading_symbol=symbol,
    )


def _owner(element: etree._Element) -> ReportingOwner:
    identity = _child(element, "reportingOwnerId")
    if identity is None:
        raise FilingParseError(field="reportingOwnerId", reason="is required but was missing")
    relationship = _child(element, "reportingOwnerRelationship")

    def flag(name: str) -> bool:
        if relationship is None:
            return False
        return _optional_bool(_child_text(relationship, name), name)

    return ReportingOwner(
        cik=_padded_cik(_required_text(identity, "rptOwnerCik"), "rptOwnerCik"),
        name=_required_text(identity, "rptOwnerName"),
        is_director=flag("isDirector"),
        is_officer=flag("isOfficer"),
        is_ten_percent_owner=flag("isTenPercentOwner"),
        is_other=flag("isOther"),
        officer_title=None if relationship is None else _child_text(relationship, "officerTitle"),
        other_text=None if relationship is None else _child_text(relationship, "otherText"),
    )


def _transactions(root: etree._Element, footnotes: dict[str, str]) -> Iterator[Form4Transaction]:
    """Every transaction line, numbered: the non-derivative table, then the
    derivative table, each in document order.

    By table rather than by plain document order because the schema fixes the
    tables in that order anyway, and numbering by table keeps the rule true of
    a document that someday does not.
    """
    line_number = 0
    for table, row, is_derivative in (
        ("nonDerivativeTable", "nonDerivativeTransaction", False),
        ("derivativeTable", "derivativeTransaction", True),
    ):
        for container in _children(root, table):
            for element in _children(container, row):
                line_number += 1
                yield _transaction(element, line_number, is_derivative, footnotes)


def _transaction(
    element: etree._Element,
    line_number: int,
    is_derivative: bool,
    footnotes: dict[str, str],
) -> Form4Transaction:
    where = f"line {line_number}"

    coding = _child(element, "transactionCoding")
    code = None if coding is None else _child_text(coding, "transactionCode")
    if code is None:
        raise FilingParseError(field="transactionCode", reason=f"is required on {where}")
    code = code.upper()
    if len(code) != 1 or not code.isalpha():
        raise FilingParseError(
            field="transactionCode", reason=f"expected one letter on {where}", value=code
        )

    amounts = _child(element, "transactionAmounts")
    acquired_disposed = _value(amounts, "transactionAcquiredDisposedCode")
    if acquired_disposed is None:
        raise FilingParseError(
            field="transactionAcquiredDisposedCode", reason=f"is required on {where}"
        )
    acquired_disposed = acquired_disposed.upper()
    if acquired_disposed not in ("A", "D"):
        raise FilingParseError(
            field="transactionAcquiredDisposedCode",
            reason=f"expected A or D on {where}",
            value=acquired_disposed,
        )

    transaction_date = _value(element, "transactionDate")
    if transaction_date is None:
        raise FilingParseError(field="transactionDate", reason=f"is required on {where}")

    price_element = None if amounts is None else _child(amounts, "transactionPricePerShare")
    price, price_low, price_high = _price(price_element, footnotes, where)

    post = _child(element, "postTransactionAmounts")
    nature = _child(element, "ownershipNature")
    underlying = _child(element, "underlyingSecurity")

    return Form4Transaction(
        line_number=line_number,
        is_derivative=is_derivative,
        security_title=_value(element, "securityTitle"),
        transaction_date=_as_date(transaction_date, "transactionDate"),
        transaction_code=code,
        acquired_disposed=acquired_disposed,
        shares=_decimal(_value(amounts, "transactionShares"), "transactionShares", where),
        price_per_share=price,
        price_low=price_low,
        price_high=price_high,
        shares_owned_after=_decimal(
            _value(post, "sharesOwnedFollowingTransaction"),
            "sharesOwnedFollowingTransaction",
            where,
        ),
        direct_or_indirect=_value(nature, "directOrIndirectOwnership"),
        nature_of_ownership=_value(nature, "natureOfOwnership"),
        is_10b5_1=False,  # decided across the whole filing, by _flag_plan_trades
        footnotes=_cited(element, footnotes),
        conversion_or_exercise_price=_positive_or_none(
            _decimal(
                _value(element, "conversionOrExercisePrice"), "conversionOrExercisePrice", where
            )
        ),
        underlying_security_title=_value(underlying, "underlyingSecurityTitle"),
        underlying_shares=_decimal(
            _value(underlying, "underlyingSecurityShares"), "underlyingSecurityShares", where
        ),
    )


def _price(
    element: etree._Element | None, footnotes: dict[str, str], where: str
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """``transactionPricePerShare`` as (price, low, high).

    A zero is no price. A value that is itself a range ("41.10-41.50") is no
    single price either, and becomes the range. The range otherwise comes from
    the footnotes the price element cites, which is where a weighted average
    explains itself.
    """
    if element is None:
        return None, None, None
    raw = _child_text(element, "value")
    if raw is not None and not _is_number(raw):
        literal = _range(raw if "$" in raw else f"${raw}")
        if literal is not None:
            return None, *literal
    price = _positive_or_none(_decimal(raw, "transactionPricePerShare", where))
    cited = " ".join(footnotes.get(note_id, "") for note_id in _cited_ids(element))
    low_high = _range(cited)
    if low_high is None:
        return price, None, None
    return price, *low_high


def _range(text: str) -> tuple[Decimal, Decimal] | None:
    """The first plausible "$low to $high" in ``text``: both positive, in
    order. A pair in the wrong order is not a range of anything."""
    for match in _PRICE_RANGE.finditer(text):
        try:
            low, high = (Decimal(group.replace(",", "")) for group in match.groups())
        except InvalidOperation:
            continue
        if 0 < low <= high:
            return low, high
    return None


def _flag_plan_trades(
    transactions: list[Form4Transaction], *, checkbox: bool
) -> list[Form4Transaction]:
    """Set :attr:`Form4Transaction.is_10b5_1` on each line.

    A line is a plan trade when a footnote it cites says so. When none does,
    the form's own checkbox applies to every line, since it cannot say which
    one it meant. In a week of real filings, 247 plan sales were footnoted and
    6 sales had only the checkbox; the M exercises beside footnoted sales,
    unfootnoted, are the case the "only when none does" rule is for.
    """
    by_footnote = [
        any(_says_plan(text) for text in transaction.footnotes.values())
        for transaction in transactions
    ]
    fallback = checkbox and not any(by_footnote)
    return [
        transaction.model_copy(update={"is_10b5_1": flagged or fallback})
        for transaction, flagged in zip(transactions, by_footnote, strict=True)
    ]


def _says_plan(text: str) -> bool:
    """A footnote says a trade was under a 10b5-1 plan: some sentence of it
    names the rule and does not negate it."""
    return any(
        _RULE_10B5_1.search(sentence) and not _NEGATION.search(sentence)
        for sentence in re.split(r"(?<=[.;])\s+", text)
    )


# --- XML access --------------------------------------------------------------


def _parse(xml: bytes) -> etree._Element:
    """Bytes to a root element, with the parser locked down.

    The same settings as the 13F parser, for the same reasons: the input came
    over HTTP, so no entities, no DTD, no network. Built per call because an
    ``XMLParser`` is not thread-safe.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FilingParseError(field="xml", reason=f"not well-formed XML: {exc}") from exc


def _local(element: etree._Element) -> str | None:
    """The element's name without its namespace. ``None`` for a comment or a
    processing instruction, whose ``tag`` is not a string."""
    tag = element.tag
    return etree.QName(tag).localname if isinstance(tag, str) else None


def _children(element: etree._Element, name: str) -> Iterator[etree._Element]:
    return (child for child in element if _local(child) == name)


def _child(element: etree._Element | None, name: str) -> etree._Element | None:
    if element is None:
        return None
    return next(_children(element, name), None)


def _descendants(element: etree._Element, name: str) -> Iterator[etree._Element]:
    return (node for node in element.iter() if _local(node) == name)


def _child_text(element: etree._Element, name: str) -> str | None:
    """Stripped text of the first ``name`` child, ``None`` when absent or empty.

    Empty is absent: agents emit ``<transactionTimeliness><value></value>`` and
    ``<rptOwnerStreet2/>`` for fields they have nothing to put in.
    """
    child = _child(element, name)
    if child is None:
        return None
    text = (child.text or "").strip()
    return text or None


def _value(element: etree._Element | None, name: str) -> str | None:
    """``<name><value>...</value></name>``: how the transaction tables wrap
    every field, so that a footnote can sit beside the value it explains."""
    wrapper = _child(element, name)
    return None if wrapper is None else _child_text(wrapper, "value")


def _required_text(element: etree._Element, name: str) -> str:
    value = _child_text(element, name)
    if value is None:
        raise FilingParseError(field=name, reason="is required but was missing or empty")
    return value


def _collapse(element: etree._Element) -> str | None:
    """All text inside ``element``, whitespace collapsed; ``None`` if blank.
    ``itertext`` rather than ``.text`` because a footnote can carry markup."""
    text = " ".join("".join(str(part) for part in element.itertext()).split())
    return text or None


def _cited_ids(element: etree._Element) -> Iterator[str]:
    for reference in _descendants(element, "footnoteId"):
        note_id = (reference.get("id") or "").strip()
        if note_id:
            yield note_id


def _cited(element: etree._Element, footnotes: dict[str, str]) -> dict[str, str]:
    """The footnotes cited anywhere inside ``element``, first-cited first."""
    return {
        note_id: footnotes[note_id]
        for note_id in dict.fromkeys(_cited_ids(element))
        if note_id in footnotes
    }


# --- conversion --------------------------------------------------------------


def _padded_cik(value: str, field: str) -> str:
    """``"1067983"`` -> ``"0001067983"``. See the 13F parser's ``_padded_cik``."""
    try:
        digits = f"{int(value):0{_CIK_WIDTH}d}"
    except ValueError as exc:
        raise FilingParseError(field=field, reason="expected digits", value=value) from exc
    if len(digits) > _CIK_WIDTH or int(value) < 0:
        raise FilingParseError(
            field=field, reason=f"expected at most {_CIK_WIDTH} digits", value=value
        )
    return digits


def _as_date(value: str, field: str) -> date:
    """An ``xs:date``: ISO, optionally with a timezone suffix, which is dropped.
    A transaction happened on a calendar day; the offset says nothing more."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if match is not None:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    raise FilingParseError(field=field, reason="expected YYYY-MM-DD", value=value)


def _optional_date(value: str | None, field: str) -> date | None:
    return None if value is None else _as_date(value, field)


def _optional_bool(value: str | None, field: str) -> bool:
    """XML boolean to ``bool``, absent meaning ``False``; an unknown spelling
    is an error rather than a guess."""
    if value is None:
        return False
    folded = value.casefold()
    if folded in _TRUE_SPELLINGS:
        return True
    if folded in _FALSE_SPELLINGS:
        return False
    raise FilingParseError(field=field, reason="expected a boolean", value=value)


def _is_number(value: str) -> bool:
    try:
        Decimal(value.replace(",", ""))
    except InvalidOperation:
        return False
    return True


def _decimal(value: str | None, field: str, where: str) -> Decimal | None:
    """A non-negative decimal, or ``None`` when absent. Thousands separators
    are tolerated; a negative is not, since every quantity and price here is
    unsigned and the A/D flag carries the direction."""
    if value is None:
        return None
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise FilingParseError(
            field=field, reason=f"expected a number on {where}", value=value
        ) from exc
    if not number.is_finite() or number < 0:
        raise FilingParseError(
            field=field, reason=f"expected a non-negative number on {where}", value=value
        )
    return number


def _positive_or_none(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value > 0 else None
