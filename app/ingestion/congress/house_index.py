"""The House Clerk's yearly disclosure index: ``{year}FD.zip``, read.

What the ZIP holds
------------------
Two files, the same index twice: ``{year}FD.xml`` and ``{year}FD.txt``
(tab-separated). No PDFs — each filing's PDF is a separate download, under
``/public_disc/ptr-pdfs/{year}/{doc_id}.pdf`` for a PTR. Only the XML is read.
It starts with a byte-order mark and looks like this::

    <FinancialDisclosure>
      <Member>
        <Prefix>Hon.</Prefix>
        <Last>Pelosi</Last>
        <First>Nancy</First>
        <Suffix />
        <FilingType>P</FilingType>
        <StateDst>CA11</StateDst>
        <Year>2026</Year>
        <FilingDate>1/23/2026</FilingDate>
        <DocID>20033725</DocID>
      </Member>
      ...

One ``Member`` per filing, not per member: a member with twelve PTRs has twelve.
``StateDst`` is the office, state and district (``CA11``; ``AK00`` at large).

``Year`` is the year the index files a disclosure under, and it is not the year
it was filed: every year's index keeps gaining late filings — the 2020 index,
fetched in November 2026, lists PTRs filed in 2021, 2022 and 2023 — which is
why a daily run reads more than the current year (:mod:`app.ingestion.congress.filings`).

Filing types
------------
``P`` is a Periodic Transaction Report, the STOCK Act filing a member makes
within 45 days of a trade, and the only type kept. The others are another shape
or carry no trades: ``O`` the annual report, ``A`` an amendment to one, ``C`` a
candidate's report, ``X`` an extension, ``T`` a termination report, ``W``, ``D``,
``E``, ``G``, ``H``, ``B`` other notices. They are counted, not validated: a
non-PTR row with an empty ``StateDst`` or no ``FilingDate`` is common, and none
of our business.

What fails
----------
A ZIP that is not one, or has no XML index, or XML that is not well formed:
:class:`HouseIndexError`, and nothing of the year is read. A single PTR row
missing what a filing needs (a doc id, a last name, a date that is a date) is
left out and listed in :attr:`HouseIndex.malformed`, rather than sinking the
year's other five hundred: the run reports it, and the next run reads the row
again, so a fix at the Clerk's end is picked up.

The index repeats a few rows verbatim (four in 2025's, none of them PTRs).
Identical repeats collapse; a PTR doc id listed twice with different details
keeps the first and lists the second as malformed.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from lxml import etree

#: ``FilingType`` for a Periodic Transaction Report.
PTR_FILING_TYPE: Final = "P"

#: The largest XML index read out of a ZIP. 2025's is 0.8 MB; this is a cap on
#: a ZIP that inflates to something else entirely, not a forecast.
MAX_INDEX_BYTES: Final = 64 * 1024 * 1024

#: What a doc id is: digits, in every year so far. Letters and dashes allowed
#: so that a change of scheme is not a year of malformed rows.
_DOC_ID: Final = re.compile(r"^[0-9A-Za-z-]{1,32}$")


class HouseIndexError(Exception):
    """The ZIP or the XML in it cannot be read as an index."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One PTR, as the index lists it. Text fields are stripped; empty is ``None``."""

    doc_id: str
    filing_type: str
    last_name: str
    first_name: str | None
    prefix: str | None
    suffix: str | None
    office: str | None
    """``StateDst``: ``CA11``."""
    year: int
    filing_date: date


@dataclass(frozen=True, slots=True)
class Malformed:
    """A PTR row left out, and why."""

    position: int
    """1-based, among the index's ``Member`` elements."""
    doc_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class HouseIndex:
    """One year's index: its PTRs, and what else it listed."""

    ptrs: tuple[IndexEntry, ...]
    listed: int
    """Every ``Member``, all types."""
    types: dict[str, int]
    """``Member`` rows by ``FilingType``, as written (``""`` for none)."""
    duplicates: int
    """PTR rows that repeated another verbatim, and collapsed into it."""
    malformed: tuple[Malformed, ...]


def index_xml(archive: bytes, year: int) -> bytes:
    """The XML index out of a yearly ZIP: ``{year}FD.xml``, or its only ``.xml``.

    :raises HouseIndexError: Not a ZIP, no XML index in it, or one over
        :data:`MAX_INDEX_BYTES`.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = [info for info in zipped.infolist() if not info.is_dir()]
            wanted = f"{year}fd.xml"
            chosen = next((m for m in members if m.filename.lower() == wanted), None)
            if chosen is None:
                xmls = [m for m in members if m.filename.lower().endswith(".xml")]
                if len(xmls) != 1:
                    names = ", ".join(m.filename for m in members) or "nothing"
                    raise HouseIndexError(f"the {year} ZIP has no {year}FD.xml: it holds {names}")
                chosen = xmls[0]
            if chosen.file_size > MAX_INDEX_BYTES:
                raise HouseIndexError(
                    f"{chosen.filename} would inflate to {chosen.file_size:,} bytes, over "
                    f"{MAX_INDEX_BYTES:,}"
                )
            with zipped.open(chosen) as handle:
                xml = handle.read(MAX_INDEX_BYTES + 1)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError) as failure:
        raise HouseIndexError(f"the {year} download is not a readable ZIP: {failure}") from failure
    if len(xml) > MAX_INDEX_BYTES:
        raise HouseIndexError(f"{chosen.filename} inflates past {MAX_INDEX_BYTES:,} bytes")
    return xml


def parse_house_index(xml: bytes) -> HouseIndex:
    """The PTRs in one year's XML index.

    :raises HouseIndexError: If the XML is not well formed, or is not a
        ``FinancialDisclosure``.
    """
    root = _parse(xml)
    if _local(root) != "FinancialDisclosure":
        raise HouseIndexError(f"the index's root is <{_local(root)}>, not <FinancialDisclosure>")

    types: Counter[str] = Counter()
    kept: dict[str, IndexEntry] = {}
    malformed: list[Malformed] = []
    duplicates = 0
    listed = 0
    for element in root:
        if _local(element) != "Member":
            continue
        listed += 1
        fields = _fields(element)
        filing_type = fields.get("FilingType") or ""
        types[filing_type] += 1
        if filing_type != PTR_FILING_TYPE:
            continue
        entry = _entry(fields, listed)
        if isinstance(entry, Malformed):
            malformed.append(entry)
            continue
        earlier = kept.get(entry.doc_id)
        if earlier is None:
            kept[entry.doc_id] = entry
        elif earlier == entry:
            duplicates += 1
        else:
            malformed.append(Malformed(listed, entry.doc_id, "listed again with different details"))
    return HouseIndex(
        ptrs=tuple(kept.values()),
        listed=listed,
        types=dict(types),
        duplicates=duplicates,
        malformed=tuple(malformed),
    )


def _entry(fields: dict[str, str | None], position: int) -> IndexEntry | Malformed:
    doc_id = fields.get("DocID")
    if doc_id is None or not _DOC_ID.match(doc_id):
        return Malformed(position, doc_id, f"DocID {doc_id!r} is not a doc id")
    last = fields.get("Last")
    if last is None:
        return Malformed(position, doc_id, "no Last name")
    raw_year = fields.get("Year")
    if raw_year is None or not raw_year.isdigit() or len(raw_year) != 4:
        return Malformed(position, doc_id, f"Year {raw_year!r} is not a year")
    raw_date = fields.get("FilingDate")
    try:
        # Month and day unpadded: 1/23/2026. strptime takes either.
        filed = datetime.strptime(raw_date or "", "%m/%d/%Y").date()
    except ValueError:
        return Malformed(position, doc_id, f"FilingDate {raw_date!r} is not M/D/YYYY")
    return IndexEntry(
        doc_id=doc_id,
        filing_type=PTR_FILING_TYPE,
        last_name=last,
        first_name=fields.get("First"),
        prefix=fields.get("Prefix"),
        suffix=fields.get("Suffix"),
        office=fields.get("StateDst"),
        year=int(raw_year),
        filing_date=filed,
    )


def _fields(member: etree._Element) -> dict[str, str | None]:
    """Each child's text, stripped, by tag. Empty is ``None``; a repeat keeps the first."""
    fields: dict[str, str | None] = {}
    for child in member:
        name = _local(child)
        if name is None or name in fields:
            continue
        text = (child.text or "").strip()
        fields[name] = text or None
    return fields


def _parse(xml: bytes) -> etree._Element:
    """Bytes to a root element, locked down as the 13F and Form 4 parsers are:
    no entities, no DTD, no network. lxml takes the byte-order mark."""
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as failure:
        raise HouseIndexError(f"the index is not well-formed XML: {failure}") from failure


def _local(element: etree._Element) -> str | None:
    tag = element.tag
    return etree.QName(tag).localname if isinstance(tag, str) else None
