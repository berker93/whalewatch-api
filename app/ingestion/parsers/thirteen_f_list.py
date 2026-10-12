"""The SEC's Official List of Section 13(f) Securities, from its text file.

The list is published each quarter as a PDF and, since 2020Q1, as a plain-text
file. There is no XLSX. Only the text file is parsed here: it is the same data
as the PDF without page furniture. Every quarter the database holds has one, and
a quarter that only has a PDF (2019Q4 and earlier) is refused before it is
fetched (:mod:`app.ingestion.edgar.thirteen_f_list`).

The format
----------
Fixed width, 80 characters a line, no header, no trailer. The SEC documents the
layout on the page that links the files::

    1-9    CUSIP
    10     option indicator: '*' or blank
    11-40  issuer name (28 characters, then blank padding)
    41-67  issuer description (16 characters, then blank padding)
    68-70  status: '*A*' added, '*D*' deleted, or blank
    71-79  blank
    80     "unused"

Column 80 is not unused: it carries a letter (``E``, ``N``, ``P``, ``U``, ``S``,
``A`` in 2020; only ``E`` from 2026). The SEC documents no meaning for it, so
it is ignored here. The archived file keeps it.

Format drift fails the parse
----------------------------
A line that does not fit the layout raises :class:`ThirteenFListParseError`,
naming the line number. A partial import is not an option: a list with one line
in a new shape probably has thousands, and importing the rest would leave a
quarter that looks complete and is not. So would a 200 that was an error page,
which this also catches, since HTML does not start with a CUSIP. An empty file
fails too.

The CUSIP is checked for shape (eight alphanumerics and a digit), not for its
check digit. From 2026Q1 the list carries rows for listed calls and puts, under
synthetic CUSIPs with a ``9`` in the seventh place, and about half of them fail
the check.

The same CUSIP more than once
-----------------------------
Those option CUSIPs collide. An option CUSIP is derived from the issuer's
six-character prefix, and a trust with many funds (``ALPS ETF TR``, ``00162Q``)
gets one ``CALL`` row per fund, all printing ``00162Q905`` and none naming the
fund. A 2026 list prints about a thousand CUSIPs more than once. Ninety percent
of the repeated lines are identical. The rest differ in status, and very rarely
in name.

:func:`parse_13f_list` collapses each CUSIP to one row, because the table is
one row per CUSIP per quarter and the repeats carry nothing a row could hold:

* **Status.** The status the copies agree on. If they disagree, ``listed``.
  At the CUSIP's level that is what happened: one underlying added and another
  deleted (or still listed) means the nine characters were on the last list and
  are on this one.
* **Name and description.** From the first copy not marked deleted, or the
  first copy if all are.
* **Option indicator.** Set if any copy has it.

:attr:`ThirteenFList.collapsed` counts the lines folded away, so the import's
summary can say so.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from app.db.models.sec_13f_security import ListStatus

#: The published line width. A line may be shorter (trailing blanks trimmed by
#: whoever saved it), never longer.
LINE_WIDTH: Final = 80

_CUSIP: Final = re.compile(r"[0-9A-Z]{8}[0-9]")

_STATUSES: Final = {"   ": ListStatus.LISTED, "*A*": ListStatus.ADDED, "*D*": ListStatus.DELETED}

# Zero-based slices of the documented one-based columns.
_CUSIP_COLUMNS: Final = slice(0, 9)
_OPTION_COLUMN: Final = 9
_NAME_COLUMNS: Final = slice(10, 40)
_DESCRIPTION_COLUMNS: Final = slice(40, 67)
_STATUS_COLUMNS: Final = slice(67, 70)


class ThirteenFListParseError(Exception):
    """A line of the list is not in the published layout, or there are none.

    Its own type, as :class:`~app.ingestion.parsers.errors.FilingParseError` is
    for filings, so the command can report a malformed list as one line rather
    than as a bug in this module. Not that type, because a list is not a filing
    and has no ``parse_error`` column to land in.
    """

    def __init__(self, reason: str, *, line_no: int | None = None, line: str | None = None) -> None:
        detail = reason if line_no is None else f"line {line_no}: {reason}"
        if line is not None:
            detail += f" (got {line!r})"
        super().__init__(detail)
        self.line_no = line_no


@dataclass(frozen=True, slots=True)
class ListedSecurity:
    """One CUSIP as the list prints it, after collapsing repeats."""

    cusip: str
    issuer_name: str
    issuer_description: str | None
    status: ListStatus
    has_listed_options: bool


@dataclass(frozen=True, slots=True)
class ThirteenFList:
    """Everything one quarter's text file says, one entry per CUSIP."""

    securities: tuple[ListedSecurity, ...]
    """In the order each CUSIP first appears in the file."""

    lines: int
    """Data lines read. ``lines - collapsed == len(securities)``."""

    collapsed: int
    """Lines folded into an earlier line for the same CUSIP."""

    def count(self, status: ListStatus) -> int:
        return sum(1 for security in self.securities if security.status is status)


def parse_13f_list(body: bytes) -> ThirteenFList:
    """Parse one quarter's text file into one :class:`ListedSecurity` per CUSIP.

    :raises ThirteenFListParseError: A line out of layout, or no lines at all.
    """
    rows = [_parse_line(line_no, line) for line_no, line in _lines(body)]
    if not rows:
        raise ThirteenFListParseError("the file has no securities in it")

    by_cusip: defaultdict[str, list[ListedSecurity]] = defaultdict(list)
    for row in rows:
        by_cusip[row.cusip].append(row)
    securities = tuple(_collapse(copies) for copies in by_cusip.values())
    return ThirteenFList(
        securities=securities, lines=len(rows), collapsed=len(rows) - len(securities)
    )


def _lines(body: bytes) -> list[tuple[int, str]]:
    """Non-blank lines and their one-based numbers.

    UTF-8 if it decodes as such, Latin-1 if not: every file so far is plain
    ASCII, and a stray accented byte should cost one odd character in one name,
    not the import.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    return [
        (line_no, line.rstrip("\r"))
        for line_no, line in enumerate(text.split("\n"), start=1)
        if line.strip()
    ]


def _parse_line(line_no: int, line: str) -> ListedSecurity:
    if len(line) > LINE_WIDTH:
        raise ThirteenFListParseError(
            f"longer than {LINE_WIDTH} characters", line_no=line_no, line=line
        )
    line = line.ljust(LINE_WIDTH)

    cusip = line[_CUSIP_COLUMNS]
    if not _CUSIP.fullmatch(cusip):
        raise ThirteenFListParseError(
            "columns 1-9 are not a CUSIP", line_no=line_no, line=line.rstrip()
        )
    option = line[_OPTION_COLUMN]
    if option not in ("*", " "):
        raise ThirteenFListParseError(
            "column 10 is not '*' or blank", line_no=line_no, line=line.rstrip()
        )
    status = _STATUSES.get(line[_STATUS_COLUMNS])
    if status is None:
        raise ThirteenFListParseError(
            "columns 68-70 are not '*A*', '*D*' or blank", line_no=line_no, line=line.rstrip()
        )
    name = " ".join(line[_NAME_COLUMNS].split())
    if not name:
        raise ThirteenFListParseError("no issuer name", line_no=line_no, line=line.rstrip())
    description = " ".join(line[_DESCRIPTION_COLUMNS].split()) or None

    return ListedSecurity(
        cusip=cusip,
        issuer_name=name,
        issuer_description=description,
        status=status,
        has_listed_options=option == "*",
    )


def _collapse(copies: list[ListedSecurity]) -> ListedSecurity:
    """One row for a CUSIP the file printed ``len(copies)`` times; see the module
    docstring for why each field is chosen as it is."""
    if len(copies) == 1:
        return copies[0]
    statuses = {copy.status for copy in copies}
    status = statuses.pop() if len(statuses) == 1 else ListStatus.LISTED
    named = next((copy for copy in copies if copy.status is not ListStatus.DELETED), copies[0])
    return ListedSecurity(
        cusip=named.cusip,
        issuer_name=named.issuer_name,
        issuer_description=named.issuer_description,
        status=status,
        has_listed_options=any(copy.has_listed_options for copy in copies),
    )
