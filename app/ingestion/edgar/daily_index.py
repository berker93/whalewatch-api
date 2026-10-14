"""Form 4 discovery via the EDGAR daily index.

Every business day EDGAR publishes ``form.{yyyymmdd}.idx``: one line per
filing per party to it, sorted by form type. A Form 4 is listed once under its
issuer and once under each reporting owner, every line with that party's CIK
and a path into that party's archive directory. So "Form 4s about Apple"
is the lines whose CIK is Apple's, and nothing has to be fetched to find them.

Parsing
-------
Fixed width, not delimited. Company names contain spaces, commas and digits,
and form types contain spaces too (``SC 13D``), so splitting on whitespace
mis-assigns both. The header's first line gives where the form type, company
name and CIK columns start. It is the only header line that can be used: its
second, ``      Date Filed  File Name``, is a continuation wrapped onto a new
line, and its offsets are not the columns'. The last three fields (CIK, date,
path) have no spaces in them, so they are the line's last three tokens. Taken
from the right rather than from the CIK column's offset, so that a company
name long enough to run into that column costs nothing but its own tail.

Decoded as Latin-1, so a column offset in characters is one in bytes. The
file is ASCII in practice, and a stray UTF-8 name decoded as UTF-8 would move
every column after it.

Which days have an index
------------------------
None on weekends or holidays, and none for today until about 10pm New York
time. A missing one is not a 404: ``daily-index/`` is served from S3, which
answers a missing key with a 403 (see
:class:`~app.ingestion.edgar.client.EdgarNotFound`). Rather than ask for files
that may not exist, :func:`fetch_published_days` reads the quarter's directory
listing once and says which days do. A day missing from it is a holiday if a
later day's index exists, and not published yet if none does; telling those
apart is :mod:`app.ingestion.insider_discovery`'s job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from app.core.logging import get_logger
from app.ingestion.edgar.client import SEC_ARCHIVES_BASE, EdgarClient, EdgarNotFound

logger = get_logger(__name__)

#: The forms the insider pipeline reads: a Form 4 and its amendment. Matched
#: exactly, because the index has ``4``, ``4/A`` and also ``40-F``, ``424B2``
#: and ``40-APP``, which a prefix match would take.
FORM_4_TYPES: Final = frozenset({"4", "4/A"})

#: The header line whose labels sit where their columns start.
_HEADER: Final = re.compile(r"^Form Type\s+Company Name\s+CIK\s*$")

#: ``edgar/data/66740/0001127602-24-024825.txt``: the party's CIK, then the
#: accession number, dashed.
_FILE_NAME: Final = re.compile(r"edgar/data/(\d{1,10})/(\d{10}-\d{2}-\d{6})\.txt")

#: A daily index file's name in a directory listing.
_INDEX_NAME: Final = re.compile(r"form\.(\d{8})\.idx")


class DailyIndexMalformedError(ValueError):
    """The index is not laid out as expected. Nothing from it is used."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One line of a daily index: one party to one filing."""

    form_type: str
    company_name: str
    """The party's name as EDGAR has it: the issuer's, or a reporting owner's
    ("ABBASI SOHAIB")."""

    cik: str
    """The party's CIK, zero-padded. The archive directory under
    :attr:`file_name`, so the filing is fetchable from it."""

    date_filed: date
    accession_no: str
    """Dashed, as everywhere else."""

    file_name: str
    """As listed, relative to the Archives root."""


def daily_index_url(day: date) -> str:
    """``.../daily-index/2024/QTR4/form.20241002.idx``."""
    return f"{_quarter_directory(day.year, _quarter(day))}form.{day:%Y%m%d}.idx"


def quarter_listing_url(year: int, quarter: int) -> str:
    """The directory listing for one quarter's daily indexes, as JSON."""
    return f"{_quarter_directory(year, quarter)}index.json"


async def fetch_published_days(edgar: EdgarClient, year: int, quarter: int) -> frozenset[date]:
    """The days in one quarter that have a daily index, from its directory listing.

    Fresh, never from the development cache: the current quarter's listing
    grows every evening, and a cached one would say nothing new was published.
    A quarter whose directory does not exist yet, which is the first minutes
    of a new quarter, has no days.

    :raises DailyIndexMalformedError: The listing is not EDGAR's directory JSON.
    """
    url = quarter_listing_url(year, quarter)
    try:
        listing = await edgar.get_json(url, fresh=True)
    except EdgarNotFound:
        logger.info("daily_index.no_quarter_directory", year=year, quarter=quarter)
        return frozenset()
    return parse_listing(listing)


def parse_listing(listing: Any) -> frozenset[date]:
    """The days a quarter's ``index.json`` lists a ``form.*.idx`` for."""
    try:
        items = listing["directory"]["item"]
        names = [item["name"] for item in items]
    except (KeyError, TypeError) as failure:
        raise DailyIndexMalformedError(f"not an EDGAR directory listing: {failure!r}") from failure
    days = set()
    for name in names:
        match = _INDEX_NAME.fullmatch(str(name))
        if match is not None:
            days.add(_yyyymmdd(match.group(1)))
    return frozenset(days)


async def fetch_daily_index(edgar: EdgarClient, day: date) -> list[IndexEntry]:
    """Every Form 4 and 4/A line of one day's index.

    From the development cache when it has the day: a published index does
    not change.

    :raises EdgarNotFound: The day has no index.
    :raises DailyIndexMalformedError: See :func:`parse_daily_index`.
    """
    body = await edgar.get_bytes(daily_index_url(day))
    return parse_daily_index(body, forms=FORM_4_TYPES)


def parse_daily_index(body: bytes, *, forms: frozenset[str] = FORM_4_TYPES) -> list[IndexEntry]:
    """The lines of a ``form.*.idx`` whose form type is one of ``forms``.

    Pure. Only those lines are checked: a malformed line of another form is
    not a reason to lose a day's Form 4s. A malformed line of one of ``forms``
    is, because it is a filing that would otherwise be silently missed.

    :raises DailyIndexMalformedError: No header line, or a line of one of
        ``forms`` that does not have a CIK, a date and a path where they belong.
    """
    lines = body.decode("latin-1").splitlines()
    header_at = next((at for at, line in enumerate(lines) if _HEADER.match(line)), None)
    if header_at is None:
        raise DailyIndexMalformedError("no 'Form Type   Company Name   CIK' header line")
    company_at = lines[header_at].index("Company Name")

    entries = []
    # Past the header: its wrapped second line, then a rule of dashes.
    for line in lines[header_at + 1 :]:
        form_type = line[:company_at].strip()
        if form_type not in forms:
            continue
        entries.append(_entry(line, form_type, company_at=company_at))
    return entries


def _entry(line: str, form_type: str, *, company_at: int) -> IndexEntry:
    fields = line[company_at:].rsplit(None, 3)
    if len(fields) != 4:
        raise DailyIndexMalformedError(f"expected a name, CIK, date and file name: {line!r}")
    name, cik, filed, file_name = fields
    path = _FILE_NAME.fullmatch(file_name)
    if not cik.isdigit() or not re.fullmatch(r"\d{8}", filed) or path is None:
        raise DailyIndexMalformedError(f"unreadable index line: {line!r}")
    if int(path.group(1)) != int(cik):
        raise DailyIndexMalformedError(f"CIK {cik} lists a file under another CIK: {line!r}")
    return IndexEntry(
        form_type=form_type,
        company_name=name.strip(),
        cik=f"{int(cik):010d}",
        date_filed=_yyyymmdd(filed),
        accession_no=path.group(2),
        file_name=file_name,
    )


def _yyyymmdd(value: str) -> date:
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError as failure:
        raise DailyIndexMalformedError(f"{value!r} is not a date") from failure


def _quarter(day: date) -> int:
    return (day.month - 1) // 3 + 1


def _quarter_directory(year: int, quarter: int) -> str:
    return f"{SEC_ARCHIVES_BASE}/daily-index/{year}/QTR{quarter}/"
