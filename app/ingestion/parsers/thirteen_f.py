"""13F parser: primary_doc.xml + infotable.xml.

The two halves are :func:`parse_primary_doc` and
:func:`parse_information_table`, and they live in one module because they are
read as a pair: the cover page's declared totals are the only independent check
on the rows the information table yields.

``primary_doc.xml`` is the document that says what the filing *is* — which
period, which manager, whether it amends something and in what way — and, on
its summary page, the filer's own count and total of the rows in the
information table. The information table is the filing's *content*: one
``<infoTable>`` per position, and the source of every number the product
reports.

That last part is the reason to parse this document first rather than treat it
as metadata. ``tableEntryTotal`` and ``tableValueTotal`` are a checksum written
by the filer: parse the information table, compare, and a truncated download or
a parser that silently skipped a malformed ``<infoTable>`` fails loudly instead
of loading a portfolio that is merely *smaller* than the real one. A short
portfolio looks exactly like a fund that sold, which is to say it looks like
data.

Purity
------
Both functions take bytes and return a value. No network, no
database, no clock. That is what makes a parser bug repairable by re-running it
over documents already in ``raw_document`` rather than by re-crawling EDGAR for
a week, and it is why the units decision — see
:attr:`~app.db.models.filing.Filing.value_multiplier` — is not made here: it
depends on ``filed_at``, which is EDGAR's fact about the submission and appears
nowhere in either document. :attr:`PrimaryDoc.table_value_total` and
:attr:`InfoTableRow.value` are therefore returned exactly as filed, in whatever
units the filing used, and the two are comparable to each other for that
reason.

Namespaces
----------
Every lookup below is a ``local-name()`` XPath. The 13F namespace URI has
changed across EDGAR schema versions, and a parser that hardcodes one of them
does not fail on the documents it cannot read — it matches nothing, and every
field comes back ``None``. Matching on the local name is immune to that, at the
cost of not distinguishing two elements that share a name in different
namespaces, which this schema does not do.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Final, cast

from lxml import etree
from pydantic import BaseModel, ConfigDict

from app.db.models.enums import AmendmentKind
from app.ingestion.parsers.errors import FilingParseError

#: EDGAR writes cover-page dates American-style, ``05-15-2024``. The ISO
#: spelling is accepted as well because it appears in a minority of older
#: documents, and because the two cannot be confused: a four-digit component
#: leads in one and trails in the other.
_DATE_FORMATS: Final = ("%m-%d-%Y", "%Y-%m-%d")

#: ``<amendmentType>`` normalised to the enum the database stores. Two entries,
#: because these are the two boxes on EDGAR's cover page — see
#: :class:`~app.db.models.enums.AmendmentKind` for why guessing between them is
#: the most expensive mistake in this pipeline.
_AMENDMENT_KINDS: Final = {
    "RESTATEMENT": AmendmentKind.RESTATEMENT,
    "NEW HOLDINGS": AmendmentKind.NEW_HOLDINGS,
}

#: How the XML booleans are spelled in practice. The schema says ``xs:boolean``,
#: which permits ``true``/``false``/``1``/``0``; filing agents also emit the
#: ``Y``/``N`` of the paper form. Anything outside both sets is a parse error
#: rather than a falsy default — ``isConfidentialOmitted`` is the flag that says
#: positions are missing from this filing on purpose, and reading an unfamiliar
#: spelling of "yes" as "no" hides exactly the filings that later get amended.
_TRUE_SPELLINGS: Final = frozenset({"true", "t", "y", "yes", "1"})
_FALSE_SPELLINGS: Final = frozenset({"false", "f", "n", "no", "0"})

#: Zero-padded width of a CIK, matching ``CHAR(10)`` on the tables that store
#: one. See :attr:`~app.db.models.filer.FilerCik.cik`.
_CIK_WIDTH: Final = 10

#: Width of a CUSIP, matching ``CHAR(9)`` on ``holding`` and ``security``. A
#: shorter value is left-padded with zeros — leading zeros are routinely eaten
#: by whatever spreadsheet the filing agent passed the file through, and
#: ``37833100`` and ``037833100`` are the same instrument. A longer one is not
#: truncated to fit: nine characters of a ten-character string is a different
#: security, and inventing one is worse than dropping the row.
_CUSIP_WIDTH: Final = 9

#: Characters a CUSIP is made of. Deliberately not a checksum test — the ninth
#: digit is a check digit and it does not always validate on real filings, which
#: makes it a fine thing to report on and a terrible thing to reject on.
_CUSIP_ALPHABET: Final = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#")

#: Width of a FIGI, matching ``CHAR(12)`` on
#: :attr:`~app.db.models.security.Security.figi`.
_FIGI_WIDTH: Final = 12

#: ``<putCall>`` normalised to the two spellings ``holding.put_call`` permits.
#: Filing agents write ``PUT``, ``Put`` and ``put``; the check constraint on the
#: column accepts exactly one of those, so the normalisation happens here rather
#: than at the loader, which would otherwise have to know about it.
_PUT_CALL: Final = {"PUT": "Put", "CALL": "Call"}

#: ``SH`` for a share count, ``PRN`` for a principal amount. Not an enum in the
#: database and not one here, but a closed set all the same: a third value would
#: fail ``sshprnamt_type_is_known`` at insert time, having already been summed
#: into someone's share total on the way there.
_SH_PRN_TYPES: Final = frozenset({"SH", "PRN"})


class PrimaryDoc(BaseModel):
    """The 13F cover page and summary page, as parsed.

    Frozen: this is the output of a pure function over bytes we have already
    archived, and there is no correct reason to edit one after the fact. If a
    field is wrong, the parser is wrong, and the fix is to re-parse — mutating
    the result would put a value in the database that no re-run reproduces.

    Deliberately not a ``Filing``. This object knows only what the document
    says; ``accession_no``, ``filed_at``, ``filer_id`` and ``value_multiplier``
    are facts about the *submission* that arrive from the index and the
    resolution step, and joining the two is the loader's job.
    """

    model_config = ConfigDict(frozen=True)

    cik: str
    """The filing manager's CIK, zero-padded to ten characters.

    Padded here rather than at the loader, so that the one place that reads the
    raw text is the one place that normalises it. See
    :attr:`~app.db.models.filer.FilerCik.cik` for why the padding is kept.
    """

    filer_name: str
    """``<name>`` under ``<filingManager>``, verbatim.

    Scoped to that parent on purpose. ``<name>`` also appears in the signature
    block, where it is the name of a *person* — the officer who signed. An
    unscoped lookup would find whichever came first in document order and write
    a natural person into ``filer.name`` the day a schema revision reorders the
    form.
    """

    form_type: str
    """``13F-HR`` or ``13F-HR/A``, from ``<submissionType>``, raw as EDGAR spells it."""

    period_of_report: date
    """The quarter end this filing describes — never the date it was filed.

    The two are 45 days or more apart, and every aggregate in the API groups on
    this one.
    """

    signature_date: date | None
    """When the officer signed. Informational; nothing joins or groups on it."""

    amendment_no: int | None
    """Which amendment this is, for a ``/A``. ``None`` on an original filing.

    A period can be amended more than once and the order matters, so this is the
    sequence number rather than a flag.
    """

    amendment_kind: AmendmentKind | None
    """Restatement or addition. ``None`` when this is not an amendment.

    An unrecognised ``<amendmentType>`` raises rather than falling back to
    ``None``: the column is a Postgres enum, so an unknown value could not be
    stored anyway, and quietly calling an amendment "not an amendment" loads it
    on top of the original.
    """

    report_type: str | None
    """``13F HOLDINGS REPORT``, ``13F NOTICE`` or ``13F COMBINATION REPORT``.

    Kept as filed, with no enum and no normalisation, matching
    :attr:`~app.db.models.filing.Filing.report_type`: the cover-page wording has
    changed before, and a new variant should reach the table for someone to look
    at rather than fail the ingest.
    """

    table_entry_total: int | None
    """The filer's own count of ``<infoTable>`` rows. The parse checksum.

    ``None``, along with the two fields below, when the document has no summary
    page — which is normal and not an error. A ``13F NOTICE`` reports no
    holdings at all, so it has nothing to summarise, and a caller comparing
    against a parsed information table has to treat "no declared total" as "no
    check available" rather than as zero.
    """

    table_value_total: int | None
    """The filer's own total of the ``value`` column, **in the units it filed in**.

    Not normalised to dollars here, because the convention changed on
    2023-01-03 and which side of it a filing falls on is decided by ``filed_at``
    — a fact about the submission that this document does not contain. See
    :attr:`~app.db.models.filing.Filing.value_multiplier`.

    Comparing this against a parsed information table is therefore a check on
    the *sum*, in whichever units both sides are still in, and doing it before
    applying the multiplier is what makes it a free check on the multiplier too.
    """

    other_included_managers_count: int | None
    """How many other managers' holdings are reported inside this filing.

    Non-zero means this filing covers positions that another manager may also
    report, which is the double-counting hazard behind
    :attr:`report_type`.
    """

    confidential_omitted: bool
    """The filer withheld positions under a confidential treatment request.

    ``True`` means this filing is knowingly incomplete and the missing positions
    will arrive later — dated to *this* period — in a ``NEW HOLDINGS``
    amendment. Absent is read as ``False``: the element is optional, and its
    absence is how the overwhelming majority of filings say "nothing withheld".
    """


def parse_primary_doc(xml: bytes) -> PrimaryDoc:
    """Parse a 13F ``primary_doc.xml`` into a :class:`PrimaryDoc`.

    :param xml: The document exactly as EDGAR served it. Bytes, not ``str``:
        the file carries an XML declaration naming its own encoding, and lxml
        refuses to parse a decoded string that does — correctly, since by then
        the declaration is either redundant or a lie.
    :raises FilingParseError: If the document is not well-formed XML, or if a
        field required to key the filing is missing, or if any field present is
        unreadable. The exception names the offending element.
    """
    root = _parse(xml)

    return PrimaryDoc(
        cik=_padded_cik(_required(root, "cik", within="credentials", fallback=True)),
        filer_name=_required(root, "name", within="filingManager"),
        form_type=_required(root, "submissionType"),
        period_of_report=_as_date(_required(root, "periodOfReport"), "periodOfReport"),
        signature_date=_optional_date(root, "signatureDate", within="signatureBlock"),
        amendment_no=_optional_int(root, "amendmentNo"),
        amendment_kind=_amendment_kind(root),
        report_type=_text(root, "reportType"),
        table_entry_total=_optional_int(root, "tableEntryTotal"),
        table_value_total=_optional_int(root, "tableValueTotal"),
        other_included_managers_count=_optional_int(root, "otherIncludedManagersCount"),
        confidential_omitted=_optional_bool(root, "isConfidentialOmitted"),
    )


# --- XML access --------------------------------------------------------------


def _parse(xml: bytes) -> etree._Element:
    """Bytes to a root element, with the parser locked down.

    The parser is built per call rather than shared as a module constant.
    ``etree.XMLParser`` instances are not safe to use from more than one thread,
    and this function is called from worker threads; the construction cost is
    noise next to parsing the document.

    ``resolve_entities`` and ``load_dtd`` are off and ``no_network`` is on
    because this input arrived over HTTP from outside. A document declaring an
    external entity can otherwise have the parser dereference it — reading a
    local file, or opening a connection to a host of the document's choosing,
    from inside a function whose whole contract is that it performs no I/O.

    Current lxml already declines to resolve external entities by default, so
    these are belt and braces. They are set anyway because that default changed
    within lxml's own 5.x series: a parser whose safety comes from a library
    default is one version pin away from not having it, and nothing about this
    function would look different on the day it stopped.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        # A document big enough to need this is not a 13F cover page.
        huge_tree=False,
    )
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        # No field name to give: the failure is the document, not an element in
        # it. The XML declaration is the closest thing to a location.
        raise FilingParseError(field="xml", reason=f"not well-formed XML: {exc}") from exc


def _text(root: etree._Element, name: str, *, within: str | None = None) -> str | None:
    """First non-empty text of ``name``, optionally scoped to a ``within`` ancestor.

    "First non-empty" rather than "first": filing agents emit self-closing and
    whitespace-only elements for fields they have nothing to put in, and an
    empty ``<amendmentNo/>`` shadowing a populated one further down would turn a
    present value into a missing one.

    The name is passed as an XPath variable rather than interpolated into the
    expression. The names here are all module literals so nothing hostile can
    reach it, but an expression built by concatenation is one refactor away from
    taking its element name from the document it is reading.
    """
    if within is None:
        nodes = root.xpath("//*[local-name()=$name]", name=name)
    else:
        nodes = root.xpath(
            "//*[local-name()=$within]//*[local-name()=$name]",
            within=within,
            name=name,
        )

    for node in cast(list[etree._Element], nodes):
        value = (node.text or "").strip()
        if value:
            return value
    return None


def _required(
    root: etree._Element,
    name: str,
    *,
    within: str | None = None,
    fallback: bool = False,
) -> str:
    """:func:`_text`, but a missing value is a parse failure.

    Reserved for the four fields that identify the filing — without any one of
    them there is nothing to key a ``filing`` row on, and a row keyed on a
    guess is worse than no row. Everything else is optional and comes back
    ``None``, because a filing that is merely missing its signature date is
    still a filing.

    ``fallback`` widens a scoped lookup to the whole document when the scope
    finds nothing, for the one field where that is safe: ``cik`` sits under
    ``<credentials>`` in current documents but not in every older one, and no
    other element in this schema is named ``cik``. It is deliberately not
    offered for ``name``, where the unscoped match is a person.
    """
    value = _text(root, name, within=within)
    if value is None and fallback and within is not None:
        value = _text(root, name)
    if value is None:
        raise FilingParseError(field=name, reason="is required but was missing or empty")
    return value


# --- conversion --------------------------------------------------------------


def _padded_cik(value: str) -> str:
    """``"1067983"`` -> ``"0001067983"``, rejecting anything that is not a CIK.

    The width check is not pedantry. The column is ``CHAR(10)``, so an
    eleven-digit value does not round-trip; catching it here names the field,
    while letting it through surfaces as a database error at flush time, several
    filings later, with no indication of which document caused it.
    """
    try:
        digits = f"{int(value):0{_CIK_WIDTH}d}"
    except ValueError as exc:
        raise FilingParseError(field="cik", reason="expected digits", value=value) from exc
    if len(digits) > _CIK_WIDTH:
        raise FilingParseError(
            field="cik", reason=f"expected at most {_CIK_WIDTH} digits", value=value
        )
    return digits


def _as_date(value: str, field: str) -> date:
    """Cover-page date text to a ``date``, trying each accepted spelling."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise FilingParseError(field=field, reason="expected MM-DD-YYYY", value=value)


def _optional_date(root: etree._Element, name: str, *, within: str | None = None) -> date | None:
    """Absent is fine; present and unreadable is not.

    The distinction is the rule the whole parser follows: presence is only
    demanded of the fields that key the filing, but anything the document does
    contain has to be readable. Silently dropping a malformed value would mean
    the parser's output and the archived bytes disagree, with nothing recording
    that they do.
    """
    value = _text(root, name, within=within)
    return None if value is None else _as_date(value, name)


def _optional_int(root: etree._Element, name: str) -> int | None:
    """A non-negative integer, or ``None`` if the element is absent.

    Thousands separators are tolerated because filing agents emit them; a
    negative value is not, because every integer on this cover page is a count
    or a total of market values and neither has a meaningful negative. A
    negative ``tableEntryTotal`` compared against a parsed information table
    would fail the checksum anyway — but as an unexplained mismatch rather than
    as the malformed field it is.
    """
    value = _text(root, name)
    if value is None:
        return None
    try:
        number = int(value.replace(",", ""))
    except ValueError as exc:
        raise FilingParseError(field=name, reason="expected an integer", value=value) from exc
    if number < 0:
        raise FilingParseError(field=name, reason="expected a non-negative integer", value=value)
    return number


def _optional_bool(root: etree._Element, name: str) -> bool:
    """XML boolean to ``bool``, absent meaning ``False``."""
    value = _text(root, name)
    if value is None:
        return False
    folded = value.casefold()
    if folded in _TRUE_SPELLINGS:
        return True
    if folded in _FALSE_SPELLINGS:
        return False
    raise FilingParseError(field=name, reason="expected a boolean", value=value)


def _amendment_kind(root: etree._Element) -> AmendmentKind | None:
    """``<amendmentType>`` to the enum, absent meaning "not an amendment".

    Read from ``amendmentType`` alone and not from ``isAmendment``. The two can
    disagree — an original filing carries ``<isAmendment>false</isAmendment>``
    and no ``<amendmentType>``, but amendments exist that omit ``isAmendment``
    entirely — and the field that decides how the filing is *loaded* is this
    one. ``isAmendment`` adds nothing this cannot answer.

    Whitespace inside the value is collapsed before lookup, so that a
    pretty-printed ``NEW  HOLDINGS`` matches; the case fold is for the agents
    that title-case it.
    """
    value = _text(root, "amendmentType")
    if value is None:
        return None
    normalised = " ".join(value.upper().split())
    try:
        return _AMENDMENT_KINDS[normalised]
    except KeyError as exc:
        raise FilingParseError(
            field="amendmentType",
            reason=f"expected one of {', '.join(sorted(_AMENDMENT_KINDS))}",
            value=value,
        ) from exc


# --- the information table ---------------------------------------------------


class InfoTableRow(BaseModel):
    """One ``<infoTable>`` element: this filer held this much of this security.

    Frozen for the same reason :class:`PrimaryDoc` is — it is the output of a
    pure function over archived bytes, and a value that can be patched after
    the fact is one no re-parse reproduces.

    Every quantity is a :class:`~decimal.Decimal` and none of them is ever a
    ``float``. Not caution in the abstract: these numbers are summed across
    thousands of rows and compared for equality across quarters, and binary
    floating point makes the sum depend on the order the rows arrive in. See
    :class:`~app.db.models.holding.Holding`, whose columns are ``numeric`` for
    the same reason — parsing to float here would throw the precision away
    before the column ever saw it.
    """

    model_config = ConfigDict(frozen=True)

    name_of_issuer: str | None
    """``<nameOfIssuer>``, verbatim, and optional despite the schema.

    Filer-supplied, abbreviated to fit the form ("BERKSHIRE HATHAWAY INC DEL"),
    and only ever used to give an unresolved security something displayable —
    :attr:`~app.db.models.security.Security.name` is nullable for that reason.
    A row whose issuer name is blank still names a CUSIP and a dollar value, and
    dropping it would delete a real position over a cosmetic field.
    """

    title_of_class: str | None
    """``COM``, ``CL A``, ``NOTE 2.375% 3/1``. Optional, and not an identifier.

    The CUSIP already distinguishes the share class; this is the filer's own
    description of it, and it is inconsistent enough between filers that
    grouping on it produces one bucket per filing agent.
    """

    cusip: str
    """Nine characters, uppercased and left-padded. See :func:`_cusip`.

    The only identifier a 13F gives, and the key the loader resolves a
    :class:`~app.db.models.security.Security` from.
    """

    figi: str | None
    """``<figi>``, twelve characters, or ``None``.

    A column that only exists on filings from 2023 onward, and even there not
    every agent populates it. When it is present it is better evidence than an
    OpenFIGI lookup — the filer is the one who knows what they bought — which is
    what :attr:`~app.db.models.security.Security.resolution_source` records.

    Malformed values become ``None`` **and the row is still returned**, with a
    warning attached. A FIGI is enrichment; a CUSIP, a value and a share count
    are the position. Losing the latter over the former is the mistake
    :attr:`~app.db.models.security.Security.ticker` exists to warn about.
    """

    value: Decimal
    """The position's value **exactly as filed, in the filing's own units**.

    Deliberately not normalised to dollars here. The convention changed on
    2023-01-03 — before it, this column is thousands of dollars; after it, whole
    dollars — and which side a filing falls on is decided by ``filed_at``, a
    fact about the submission that appears nowhere in this document. Applying a
    multiplier here would mean guessing it from the period, which gets
    amendments exactly backwards: an amendment filed in 2024 for a 2019 period
    is in whole dollars.

    Leaving it raw is also what makes the checksum free. This is in the same
    units as :attr:`PrimaryDoc.table_value_total`, so the two can be compared
    before anything is scaled, and the comparison then validates the multiplier
    as well as the parse. See
    :attr:`~app.db.models.filing.Filing.value_multiplier`.

    One thing this number is not: comparable across :attr:`put_call`. An
    option's value is the notional value of the underlying, not the premium.
    """

    shares: Decimal
    """``<sshPrnamt>`` — a share count *or* a principal amount.

    :attr:`sh_prn_type` says which, and the two are not addable. ``Decimal``
    rather than ``int`` because ``PRN`` rows report a face value and fractional
    share counts do appear after corporate actions; truncating either loses real
    quantity.

    Read out of the ``<shrsOrPrnAmt>`` wrapper that contains it, not from the
    top level of the row — see :func:`_row_fields`.
    """

    sh_prn_type: str
    """``SH`` or ``PRN``, uppercased. Which unit :attr:`shares` is counted in."""

    put_call: str | None
    """``Put``, ``Call``, or ``None`` for the underlying security itself.

    ``None`` is the overwhelmingly common case and it is a real distinction, not
    a missing value: the same CUSIP can appear three times in one filing as
    common stock, calls and puts. That is why it is part of ``holding``'s
    natural key, and why that key needs ``NULLS NOT DISTINCT``.
    """

    investment_discretion: str | None
    """``SOLE``, ``DEFINED`` or ``SHARED``, as filed and not normalised."""

    other_managers: str | None
    """``<otherManager>``, verbatim — usually ``"1"`` or ``"1,2"``.

    Sequence numbers pointing into the cover page's list of other managers, and
    the reason a filing can report the same CUSIP on two rows: one per set of
    managers sharing the position. Kept as text rather than split into
    identifiers because agents also put free text here, and because nothing
    downstream joins on it yet. Repeated elements are joined with ``", "``.
    """

    voting_sole: Decimal | None
    voting_shared: Decimal | None
    voting_none: Decimal | None
    """The ``<votingAuthority>`` breakdown. Nullable: not every filer fills it in.

    Not checked against :attr:`shares`. Filers get this wrong often enough that
    enforcing the identity would reject valid filings, and the disagreement is
    itself worth keeping — see :class:`~app.db.models.holding.Holding`.
    """


class InfoTableWarning(BaseModel):
    """One row the parser could not fully read, and what it did about it.

    The existence of this type is the whole reason :func:`parse_information_table`
    does not simply raise. A 3,000-row filing with one malformed row presents
    two bad options if the only outcomes are "raise" and "skip": failing the
    whole filing over one line loses 2,999 good positions, and skipping the line
    silently loads a portfolio that is merely *smaller* than the real one —
    which is indistinguishable from a fund that sold. Neither is acceptable
    unattended, so the parser reports instead: the rows it could read, and an
    itemised account of the ones it could not.

    What the loader does with these is a policy decision that belongs to the
    loader, not here. The useful shape of it: compare ``len(rows)`` plus the
    number of dropped warnings against :attr:`PrimaryDoc.table_entry_total`, and
    a filing whose arithmetic closes is one whose losses are all accounted for.
    """

    model_config = ConfigDict(frozen=True)

    row: int
    """1-based position of the ``<infoTable>`` in document order.

    Not an identifier of anything — it is how you find the element in the raw
    document, which is the only way to see what actually went wrong.
    """

    field: str
    """Local element name, spelled as EDGAR spells it, for grepping the source."""

    reason: str
    """What was expected, in the same imperative as :class:`FilingParseError`."""

    value: str | None
    """The text found, or ``None`` when the element was absent entirely.

    Same distinction the cover-page parser draws: absent usually means a filer
    who omits an optional field, malformed means the parser is behind a schema
    change.
    """

    cusip: str | None
    """The row's raw ``<cusip>`` text, when it had one.

    Present even on dropped rows, and the reason it is worth carrying: it is
    what lets someone ask "which security did we lose" without opening the XML.
    ``None`` when the CUSIP itself was the missing field.
    """

    dropped: bool
    """Whether the row was left out of :attr:`InformationTable.rows`.

    ``True`` for a missing or unreadable required field, ``False`` for a
    tolerated one — today only a malformed ``<figi>``, which is nulled while the
    position it belongs to is kept. Without this flag a caller cannot tell
    whether a warning explains a shortfall against ``tableEntryTotal`` or not.
    """


class InformationTable(BaseModel):
    """The parsed rows, plus an account of the ones that did not parse.

    Returned instead of a bare ``list[InfoTableRow]`` because the warnings have
    to go *somewhere*. A list return type leaves two choices for a malformed
    row — raise, or drop it silently — and the second one is how a portfolio
    quietly shrinks. Callers that only want the rows can say ``table.rows``;
    callers that are about to write to the database should look at
    :attr:`warnings` first.
    """

    model_config = ConfigDict(frozen=True)

    rows: tuple[InfoTableRow, ...]
    """Every row that parsed, in document order.

    Order is preserved and duplicates are not collapsed. A filer may report the
    same CUSIP on several rows — split by ``putCall``, by ``sshPrnamtType``, or
    by which other managers share the position — and only the first two of those
    are distinguished by ``holding``'s natural key. Rows that differ *only* by
    :attr:`InfoTableRow.other_managers` therefore have to be summed by the
    loader before insert; collapsing them here would hide from the loader that
    there was ever anything to sum.
    """

    warnings: tuple[InfoTableWarning, ...]
    """Rows that were dropped or amended, in document order. Usually empty."""

    @property
    def value_total(self) -> Decimal:
        """Sum of :attr:`InfoTableRow.value` over :attr:`rows`, unscaled.

        The left-hand side of the checksum against
        :attr:`PrimaryDoc.table_value_total`, which is why it is not normalised
        to dollars: both sides are in the filing's own units, and comparing them
        before the multiplier is applied checks the multiplier too.
        """
        return sum((row.value for row in self.rows), start=Decimal(0))


def parse_information_table(xml: bytes) -> InformationTable:
    """Parse a 13F information table into rows, plus warnings for what failed.

    :param xml: The document exactly as EDGAR served it, for the reason given
        on :func:`parse_primary_doc` — the XML declaration names an encoding,
        and lxml is right to refuse a decoded string that carries one.
    :returns: The rows in document order and an account of anything that did not
        make it into them. A document with no ``<infoTable>`` elements yields an
        empty table and no warnings: that is what a ``13F NOTICE`` amounts to,
        and it is a valid filing rather than a failure.
    :raises FilingParseError: Only for a failure of the *document* — malformed
        XML, or a download cut short. A failure of a single row is a warning,
        never an exception; see :class:`InfoTableWarning` for why.

    Streamed with ``iterparse`` and cleared as it goes, so peak memory is one
    row rather than one document. This matters at exactly the moment it is
    hardest to observe: a backfill runs these concurrently, and the largest
    filings — an index manager reporting five thousand positions — are the ones
    that arrive at the same time as all the others.
    """
    rows: list[InfoTableRow] = []
    warnings: list[InfoTableWarning] = []

    for position, element in enumerate(_info_tables(xml), start=1):
        fields = _row_fields(element)
        cusip = _field(fields, "cusip")
        figi, figi_problem = _figi(_field(fields, "figi"))

        try:
            row = _row(fields, figi=figi)
        except FilingParseError as problem:
            # Caught rather than propagated, and caught narrowly: nothing inside
            # _row raises a document-level failure, so everything that lands
            # here is one row's problem. The exception type is shared with the
            # document-level one because the fields are identical and a caller
            # that decides to escalate a warning should get the same shape back.
            warnings.append(_warning(position, problem, cusip=cusip, dropped=True))
            continue

        rows.append(row)
        if figi_problem is not None:
            warnings.append(_warning(position, figi_problem, cusip=row.cusip, dropped=False))

    return InformationTable(rows=tuple(rows), warnings=tuple(warnings))


# --- streaming ---------------------------------------------------------------


def _info_tables(xml: bytes) -> Iterator[etree._Element]:
    """Yield each ``<infoTable>`` element, then free it before reading the next.

    The clearing is the point. Without it this is a slower ``fromstring`` that
    holds the whole tree anyway; with it, the parser's memory is flat in the
    number of rows. Two steps are needed and only doing the first is the common
    mistake: ``element.clear()`` empties the row, but the row itself stays
    attached to its parent, so a 5,000-position filing still ends up holding
    5,000 empty elements plus the namespace declarations each one carries.
    Deleting the preceding siblings is what actually releases them.

    ``tag="{*}infoTable"`` is the streaming equivalent of the ``local-name()``
    XPaths in the rest of this module, and it is here for the same reason: the
    13F namespace URI has changed between schema versions, and a parser that
    pins one of them does not fail loudly on a document it cannot read — it
    matches nothing and returns an empty portfolio.

    ``resolve_entities=False`` is *not* redundant here, unlike in
    :func:`_parse`. ``iterparse`` defaults it to ``True``, so a document
    declaring an external entity would have this function open a file or a
    socket — inside a parser whose entire contract is that it performs no I/O,
    on input that arrived over HTTP from outside.
    """
    context = etree.iterparse(
        BytesIO(xml),
        events=("end",),
        tag="{*}infoTable",
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        # A 13F information table is large but not adversarially so, and
        # huge_tree=True disables the limits that keep a hostile document from
        # exhausting memory in a worker.
        huge_tree=False,
    )

    try:
        for _, element in context:
            yield element
            _release(element)
    except etree.XMLSyntaxError as exc:
        # Raised during iteration rather than at construction, because nothing
        # has been read yet when the context is built. A truncated download
        # therefore surfaces here, after some rows have already been yielded —
        # which is exactly why this is an exception and not a warning: the rows
        # already collected are a prefix of the filing, and loading a prefix is
        # the failure this parser exists to prevent.
        raise FilingParseError(field="xml", reason=f"not well-formed XML: {exc}") from exc


def _release(element: etree._Element) -> None:
    """Drop a finished row and everything before it from the in-memory tree."""
    element.clear()
    parent = element.getparent()
    if parent is None:
        return
    while element.getprevious() is not None:
        del parent[0]


# --- reading one row ---------------------------------------------------------


def _row_fields(element: etree._Element) -> dict[str, str]:
    """Flatten one ``<infoTable>`` into ``{lowercased local name: text}``.

    Flattened on purpose, because the row is not flat and its nesting is a trap.
    ``<sshPrnamt>`` and ``<sshPrnamtType>`` live inside a ``<shrsOrPrnAmt>``
    wrapper, and ``Sole``/``Shared``/``None`` inside ``<votingAuthority>``;
    a lookup against the row's direct children finds none of them and returns a
    row with no share count. Reading every descendant makes the wrapper
    irrelevant — and it stays irrelevant the next time the schema adds or
    removes one.

    Lowercased because the voting elements are ``<Sole>``, ``<Shared>`` and
    ``<None>`` in the schema — capitalised, unlike every other element in the
    document — and agents disagree about that. Case-folding the key means the
    parser does not have to be right about which of them is right.

    Safe to flatten only because these names do not collide inside one row: no
    two elements under an ``<infoTable>`` share a local name, with the single
    exception of a repeated ``<otherManager>``, which is joined rather than
    overwritten. Empty and whitespace-only elements are skipped so that a
    self-closing ``<putCall/>`` reads as absent rather than as a value.
    """
    fields: dict[str, str] = {}

    for node in element.iter():
        if not isinstance(node.tag, str):
            # Comments and processing instructions, whose .tag is a callable.
            continue
        text = (node.text or "").strip()
        if not text:
            continue

        name = node.tag.rpartition("}")[2].casefold()
        if name not in fields:
            fields[name] = text
        elif name == "othermanager":
            fields[name] = f"{fields[name]}, {text}"

    return fields


def _row(fields: dict[str, str], *, figi: str | None) -> InfoTableRow:
    """Build a row, raising on the first field that makes it unloadable.

    Required here means required by the *database*: ``cusip``, ``value``,
    ``sshPrnamt`` and ``sshPrnamtType`` are the four that reach ``NOT NULL``
    columns on :class:`~app.db.models.holding.Holding`, three of them inside its
    natural key. A row missing any of them cannot be stored, and a row storing a
    guess in place of one is worse than no row at all.

    Everything else is optional and comes back ``None`` when absent, but is
    still rejected when present and unreadable — the same rule the cover page
    follows. A ``<votingAuthority>`` of ``"N/A"`` is a fact about the document,
    and dropping it on the floor would leave the parser's output and the
    archived bytes disagreeing with nothing to say that they do.
    """
    return InfoTableRow(
        name_of_issuer=_field(fields, "nameOfIssuer"),
        title_of_class=_field(fields, "titleOfClass"),
        cusip=_cusip(_required_field(fields, "cusip")),
        figi=figi,
        value=_quantity(_required_field(fields, "value"), "value"),
        shares=_quantity(_required_field(fields, "sshPrnamt"), "sshPrnamt"),
        sh_prn_type=_sh_prn_type(_required_field(fields, "sshPrnamtType")),
        put_call=_put_call(_field(fields, "putCall")),
        investment_discretion=_field(fields, "investmentDiscretion"),
        other_managers=_field(fields, "otherManager"),
        voting_sole=_optional_quantity(_field(fields, "Sole"), "Sole"),
        voting_shared=_optional_quantity(_field(fields, "Shared"), "Shared"),
        voting_none=_optional_quantity(_field(fields, "None"), "None"),
    )


def _field(fields: dict[str, str], name: str) -> str | None:
    """Look a field up by its EDGAR spelling.

    The spelling is kept at the call sites rather than in the dictionary because
    it is what :attr:`InfoTableWarning.field` reports, and a warning naming
    ``sshprnamt`` sends someone grepping for a string the document does not
    contain.
    """
    return fields.get(name.casefold())


def _required_field(fields: dict[str, str], name: str) -> str:
    """:func:`_field`, but absent is a row failure."""
    value = _field(fields, name)
    if value is None:
        raise FilingParseError(field=name, reason="is required but was missing or empty")
    return value


def _warning(
    position: int,
    problem: FilingParseError,
    *,
    cusip: str | None,
    dropped: bool,
) -> InfoTableWarning:
    """Turn a caught row failure into the record of it that the caller sees."""
    return InfoTableWarning(
        row=position,
        field=problem.field,
        reason=problem.reason,
        value=problem.value,
        cusip=cusip,
        dropped=dropped,
    )


# --- row conversion ----------------------------------------------------------


def _cusip(value: str) -> str:
    """Normalise a CUSIP to the nine characters the database stores.

    Uppercased, stripped of the spaces and hyphens agents use to group it
    (``037833-10-0``), and left-padded with zeros. Every one of those is about
    the same failure: ``holding.cusip`` and ``security.cusip`` are ``CHAR(9)``
    and the join between a holding and its security is this string, so two
    spellings of one CUSIP are two securities, each holding half the filers who
    own it, and neither total is wrong in a way that looks wrong.

    Padding rather than rejecting a short value because leading zeros go missing
    for a mundane reason — a spreadsheet somewhere in the filing agent's
    pipeline read the column as a number — and ``37833100`` is unambiguous.
    Anything longer than nine characters is rejected instead of truncated: the
    first nine characters of a ten-character string identify a different
    security, and inventing one is worse than losing the row.
    """
    compact = value.upper().replace(" ", "").replace("-", "")
    if not compact:
        raise FilingParseError(field="cusip", reason="is required but was missing or empty")
    if len(compact) > _CUSIP_WIDTH:
        raise FilingParseError(
            field="cusip", reason=f"expected at most {_CUSIP_WIDTH} characters", value=value
        )
    if not set(compact) <= _CUSIP_ALPHABET:
        raise FilingParseError(field="cusip", reason="expected an alphanumeric CUSIP", value=value)
    return compact.rjust(_CUSIP_WIDTH, "0")


def _figi(value: str | None) -> tuple[str | None, FilingParseError | None]:
    """Normalise a FIGI, reporting a bad one without losing the row it is on.

    The only field in this parser whose failure is not fatal to its row, and the
    asymmetry is deliberate. A FIGI is a mapping to an outside identifier that
    can be looked up again later — :attr:`~app.db.models.security.Security.figi`
    is nullable and unresolved is a normal state — while the CUSIP, the value
    and the share count are the position itself. Dropping a real holding because
    an agent wrote ``N/A`` in an optional column would shrink a portfolio to
    protect a field that nothing depends on.

    :returns: The FIGI and ``None``, or ``None`` and the problem to warn about.
    """
    if value is None:
        return None, None

    compact = value.upper().replace(" ", "")
    if len(compact) != _FIGI_WIDTH or not compact.isalnum():
        return None, FilingParseError(
            field="figi", reason=f"expected {_FIGI_WIDTH} alphanumeric characters", value=value
        )
    return compact, None


def _quantity(value: str, field: str) -> Decimal:
    """Parse a value or a share count to ``Decimal``. Never ``float``.

    Thousands separators are tolerated, because filing agents emit them and
    ``1,200,000`` is not ambiguous.

    Negatives are rejected. 13F is long-only — there are no short positions in
    this dataset and never will be — so a minus sign here is a sign error in a
    filing agent's export, not a bearish bet, and the check constraints on
    ``holding`` would refuse it at insert time regardless. Catching it here
    names the row instead of failing a batch.

    The finiteness check is not defensive programming. ``Decimal("NaN")`` and
    ``Decimal("Infinity")`` both parse happily, and either one poisons every
    aggregate it ever reaches: a NaN in a column makes ``SUM`` return NaN for
    the whole portfolio, silently and permanently.
    """
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise FilingParseError(field=field, reason="expected a number", value=value) from exc
    if not number.is_finite():
        raise FilingParseError(field=field, reason="expected a finite number", value=value)
    if number < 0:
        raise FilingParseError(field=field, reason="expected a non-negative number", value=value)
    return number


def _optional_quantity(value: str | None, field: str) -> Decimal | None:
    """:func:`_quantity`, with absent meaning absent rather than zero.

    The distinction matters on ``<votingAuthority>``: a filer who does not
    report the breakdown and a filer who reports no voting authority at all are
    saying different things, and zeroing the first turns "not stated" into
    "holds no vote", which is a claim about a real position.
    """
    return None if value is None else _quantity(value, field)


def _sh_prn_type(value: str) -> str:
    """``SH`` or ``PRN``, uppercased, and nothing else.

    The check is worth having because the failure it prevents is arithmetic
    rather than structural: ``PRN`` rows report a bond's face value in dollars
    and ``SH`` rows report a share count, so an unrecognised third value that
    reached the database would be summed into one or the other and produce a
    number with no unit at all. The column's check constraint would reject it —
    but by then the row has no filing context left to report.
    """
    folded = value.upper()
    if folded not in _SH_PRN_TYPES:
        raise FilingParseError(
            field="sshPrnamtType",
            reason=f"expected one of {', '.join(sorted(_SH_PRN_TYPES))}",
            value=value,
        )
    return folded


def _put_call(value: str | None) -> str | None:
    """``Put``, ``Call``, or ``None`` for the underlying security itself.

    ``None`` is not a missing value here, it is the common case: most rows in
    the table are common stock, and the element is simply absent on them.

    An unrecognised spelling fails the row rather than falling back to ``None``,
    and that is the expensive direction to get wrong. Reading an option line as
    common stock adds the notional value of the underlying to a portfolio total
    and counts the shares as owned; the position it describes may be a hedge
    against the very thing it appears to be a bet on.
    """
    if value is None:
        return None
    try:
        return _PUT_CALL[value.upper()]
    except KeyError as exc:
        raise FilingParseError(
            field="putCall",
            reason=f"expected one of {', '.join(sorted(_PUT_CALL.values()))}",
            value=value,
        ) from exc
