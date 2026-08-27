"""13F parser: primary_doc.xml + infotable.xml.

This module holds the cover page half. ``primary_doc.xml`` is the document that
says what the filing *is* — which period, which manager, whether it amends
something and in what way — and, on its summary page, the filer's own count and
total of the rows in the information table.

That last part is the reason to parse this document first rather than treat it
as metadata. ``tableEntryTotal`` and ``tableValueTotal`` are a checksum written
by the filer: parse the information table, compare, and a truncated download or
a parser that silently skipped a malformed ``<infoTable>`` fails loudly instead
of loading a portfolio that is merely *smaller* than the real one. A short
portfolio looks exactly like a fund that sold, which is to say it looks like
data.

Purity
------
:func:`parse_primary_doc` takes bytes and returns a value. No network, no
database, no clock. That is what makes a parser bug repairable by re-running it
over documents already in ``raw_document`` rather than by re-crawling EDGAR for
a week, and it is why the units decision — see
:attr:`~app.db.models.filing.Filing.value_multiplier` — is not made here: it
depends on ``filed_at``, which is EDGAR's fact about the submission and appears
nowhere in the document itself. :attr:`PrimaryDoc.table_value_total` is
therefore returned exactly as filed, in whatever units the filing used.

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

from datetime import date, datetime
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
