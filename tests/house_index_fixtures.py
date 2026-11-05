"""House Clerk yearly ZIPs, built to order, in the shape the real ones have.

The real ``{year}FD.zip`` (checked against 2020 to 2026, November 2026) holds
``{year}FD.xml`` and ``{year}FD.txt``; the XML starts with a byte-order mark,
writes an empty field as ``<Suffix />``, and has one ``Member`` per filing::

    house_zip(2026, member("20033725", "Pelosi", "Nancy", filed="1/23/2026"))
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

_FIELDS = ("Prefix", "Last", "First", "Suffix", "FilingType", "StateDst", "Year", "FilingDate")


def member(
    doc_id: str,
    last: str,
    first: str = "Pat",
    *,
    filing_type: str = "P",
    office: str = "CA11",
    year: int = 2026,
    filed: str = "3/2/2026",
    prefix: str = "Hon.",
    suffix: str = "",
) -> dict[str, str]:
    return {
        "Prefix": prefix,
        "Last": last,
        "First": first,
        "Suffix": suffix,
        "FilingType": filing_type,
        "StateDst": office,
        "Year": str(year),
        "FilingDate": filed,
        "DocID": doc_id,
    }


def house_xml(*members: dict[str, str]) -> bytes:
    lines = ['﻿<?xml version="1.0" encoding="utf-8"?>', "<FinancialDisclosure>"]
    for fields in members:
        lines.append("  <Member>")
        for name in (*_FIELDS, "DocID"):
            value = fields.get(name, "")
            lines.append(f"    <{name}>{value}</{name}>" if value else f"    <{name} />")
        lines.append("  </Member>")
    lines.append("</FinancialDisclosure>")
    return "\r\n".join(lines).encode("utf-8")


def house_zip(
    year: int, *members: dict[str, str], built: datetime = datetime(2026, 11, 4, 9, 0)
) -> bytes:
    """The ZIP, with the XML and a TXT beside it. ``built`` is the entries'
    timestamp: the Clerk's nightly rebuild changes it, and with it the bytes."""
    xml = house_xml(*members)
    stamp = built.timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(zipfile.ZipInfo(f"{year}FD.txt", stamp), b"Prefix\tLast\r\n")
        zipped.writestr(zipfile.ZipInfo(f"{year}FD.xml", stamp), xml)
    return buffer.getvalue()
