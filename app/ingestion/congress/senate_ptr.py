"""What Senate eFD serves for Periodic Transaction Reports, read: search pages and reports.

Two shapes, both as :class:`~app.ingestion.congress.senate_efd.SenateEfd`
archives them:

- **A search page**: DataTables JSON, ``{"recordsFiltered": n, "data": [row, …]}``,
  each row ``[first, last, office, link, date]``. The link is an ``<a>`` to
  ``/search/view/ptr/{uuid}/`` — a report filed online — or
  ``/search/view/paper/{uuid}/`` — a paper one, served as scanned page images.
  Its text is the report's title, ``Periodic Transaction Report for 10/21/2026``,
  with `` (Amendment 1)`` on an amendment. :func:`parse_search_page`.
- **A report filed online**: an HTML page with one table of transactions, nine
  columns (``#``, Transaction Date, Owner, Ticker, Asset Name, Asset Type,
  Type, Amount, Comment), and a line above it counting them: ``(7
  transactions total)``. :func:`parse_report`.

Both were checked against 583 reports filed online by senators from January
2021 to October 2026, as archived from eFD by a public scraper: every one had
the one table, the nine columns, nine cells a row and the count, and every
value of Owner, Asset Type, Type and Amount was one listed below.

A layout change is an error, never zero rows
--------------------------------------------
eFD is structured, so anything off its shape is not a filer's slip but eFD
having changed: a table with other columns, a row of eight cells, a count that
does not match the rows, a search row that is not five fields, a link to a
kind of report this does not know. Each raises :class:`SenateLayoutError`
rather than reading what it can, because what it can is often nothing — and a
feed that silently has no Senate trades looks exactly like a quiet week. The
run stops, says which archived page, and tells ops
(:mod:`app.ops.alerts`); a parser fix then re-reads the archive.

A *value* eFD has not used before — an asset type, an owner, an amount that
is not one of the bands — is different: the row is kept as ``needs_review``
with the reason, as a House row would be, and the rest of the report is read.
A vocabulary grows; a layout that moves is broken.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

import lxml.html
from lxml.html import HtmlElement

from app.ingestion.congress.ptr import (
    Extraction,
    PtrDocument,
    PtrRow,
    RowStatus,
    TransactionType,
    parse_amount,
    parse_date,
    parse_exact_amount,
)

#: Bumped whenever a change here would read some report differently, so that
#: ``ingest-senate-ptrs`` reads every Senate filing read by an older one again —
#: from the archive, without a fetch.
SENATE_PARSER_VERSION: Final = 1

#: The report table's header, exactly.
COLUMNS: Final = (
    "#",
    "Transaction Date",
    "Owner",
    "Ticker",
    "Asset Name",
    "Asset Type",
    "Type",
    "Amount",
    "Comment",
)

#: eFD's Owner column to ``congress_transaction.owner``: the House's codes.
#: ``Self`` is the senator, which is no code.
OWNERS: Final[dict[str, str | None]] = {
    "Self": None,
    "Spouse": "SP",
    "Joint": "JT",
    "Child": "DC",
    "Dependent Child": "DC",
}

TRANSACTION_TYPES: Final[dict[str, TransactionType]] = {
    "Purchase": TransactionType.PURCHASE,
    "Sale (Full)": TransactionType.SALE,
    "Sale (Partial)": TransactionType.PARTIAL_SALE,
    "Sale": TransactionType.SALE,
    "Exchange": TransactionType.EXCHANGE,
}

#: eFD's Asset Type to the House's two-letter code, so ``asset_type`` means one
#: thing across chambers. eFD's own words stay in the row's ``raw_text``.
ASSET_TYPES: Final[dict[str, str]] = {
    "Stock": "ST",
    "Stock Option": "OP",
    "Corporate Bond": "CS",
    "Municipal Security": "GS",
    "Government Security": "GS",
    "Commodities/Futures Contract": "FU",
    "Non-Public Stock": "PS",
    "Cryptocurrency": "CT",
    "Mutual Fund": "MF",
    "Other Securities": "OT",
    "Other": "OT",
}

#: The report kinds a search link can name: online, or scanned paper.
ELECTRONIC: Final = "ptr"
PAPER: Final = "paper"

_LINK: Final = re.compile(
    r"^/search/view/(?P<kind>[a-z]+)/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)
_TITLE: Final = re.compile(
    r"^Periodic Transaction Report(?: for (?P<date>\d{1,2}/\d{1,2}/\d{4}))?"
    r"(?: \(Amendment(?: (?P<amendment>\d+))?\))?$"
)
_ROLE: Final = re.compile(r"\(([^()]+)\)\s*$")
_TOTAL: Final = re.compile(r"\((\d+) transactions? total\)")
_TICKER: Final = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,11}$")
#: An exchange's Asset Name cell: two names, one above the other.
_EXCHANGE: Final = re.compile(r"^(?P<given>.+?) \(Exchanged\) (?P<received>.+?) \(Received\)$")
#: What eFD prints in an empty cell.
_EMPTY: Final = frozenset({"", "--", "\u2014", "\u2013", "N/A"})


class SenateLayoutError(Exception):
    """What eFD served is not the shape this reads: eFD changed, or served
    something else as a 200. Never a reason to return no rows."""

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message if key is None else f"{message} (archived at {key})")
        self.key = key


# --- search pages ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchRow:
    """One report a search listed."""

    doc_id: str
    """eFD's id for the report, lower-cased: a UUID."""
    kind: str
    """:data:`ELECTRONIC` or :data:`PAPER`."""
    link: str
    """The path eFD links it at: ``/search/view/ptr/{id}/``."""
    first_name: str | None
    last_name: str
    office: str
    """The filer's role, as eFD puts it in parentheses: ``Senator``,
    ``Former Senator``."""
    filing_date: date
    report_date: date | None
    """The day the title says the report is "for": on an amendment, the
    original's."""
    amendment: int | None
    """``1`` for ``(Amendment 1)``; ``1`` too for a bare ``(Amendment)``, which
    is the first. ``None`` for an original."""


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a search."""

    total: int
    """``recordsFiltered``: the reports the whole search matches."""
    rows: tuple[SearchRow, ...]


def parse_search_page(body: bytes, *, key: str | None = None) -> SearchPage:
    """One archived search page, read.

    :param key: Where it is archived, for the error.
    :raises SenateLayoutError: Not the JSON eFD serves, or a row not its shape.
    """
    try:
        page = json.loads(body)
    except ValueError as failure:
        raise SenateLayoutError(f"a search page is not JSON: {failure}", key=key) from None
    if not isinstance(page, dict):
        raise SenateLayoutError("a search page is not a JSON object", key=key)
    data, total = page.get("data"), page.get("recordsFiltered")
    if not isinstance(data, list):
        raise SenateLayoutError("a search page has no 'data' list", key=key)
    total = _count(total)
    if total is None:
        raise SenateLayoutError("a search page has no 'recordsFiltered' count", key=key)
    rows = []
    for position, row in enumerate(data, start=1):
        try:
            rows.append(_search_row(row))
        except SenateLayoutError as failure:
            raise SenateLayoutError(f"search row {position}: {failure}", key=key) from None
    return SearchPage(total=total, rows=tuple(rows))


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _search_row(row: Any) -> SearchRow:
    if not isinstance(row, list) or len(row) != 5 or not all(isinstance(v, str) for v in row):
        raise SenateLayoutError(f"not five strings: {str(row)[:200]!r}")
    first, last, office, link_html, filed = row
    try:
        fragment = lxml.html.fragment_fromstring(link_html, create_parent="div")
    except Exception:  # lxml raises a parser error of its own on junk
        raise SenateLayoutError(f"the link is not HTML: {link_html[:200]!r}") from None
    anchors = _find(fragment, ".//a[@href]")
    if len(anchors) != 1:
        raise SenateLayoutError(f"not one link: {link_html[:200]!r}")
    href = anchors[0].get("href", "").strip()
    link = _LINK.match(href)
    if link is None:
        raise SenateLayoutError(f"a link to something other than a report: {href!r}")
    kind = link["kind"].lower()
    if kind not in (ELECTRONIC, PAPER):
        raise SenateLayoutError(f"a report of a kind not known: {kind!r} at {href!r}")
    report_date, amendment = _title(_text(anchors[0]))
    filing_date = _efd_date(filed)
    if filing_date is None:
        raise SenateLayoutError(f"a filing date that is not MM/DD/YYYY: {filed!r}")
    last_name = " ".join(last.split())
    if not last_name:
        raise SenateLayoutError("no last name")
    return SearchRow(
        doc_id=link["id"].lower(),
        kind=kind,
        link=f"/search/view/{kind}/{link['id'].lower()}/",
        first_name=" ".join(first.split()) or None,
        last_name=last_name,
        office=_role(office),
        filing_date=filing_date,
        report_date=report_date,
        amendment=amendment,
    )


def _title(title: str) -> tuple[date | None, int | None]:
    """``Periodic Transaction Report for 12/21/2022 (Amendment 1)`` → the day, 1."""
    match = _TITLE.match(title)
    if match is None:
        raise SenateLayoutError(f"a title that is not a PTR's: {title!r}")
    day = None
    if match["date"] is not None:
        day = _efd_date(match["date"])
        if day is None:
            raise SenateLayoutError(f"a title with a date that is not one: {title!r}")
    amendment = None
    if "(Amendment" in title:
        amendment = int(match["amendment"]) if match["amendment"] else 1
    return day, amendment


def _role(office: str) -> str:
    """``Boozman, John (Senator)`` → ``Senator``; a paper filing's bare
    ``Senator`` as it is."""
    office = " ".join(office.split())
    match = _ROLE.search(office)
    return match[1].strip() if match else office


def _efd_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


# --- reports filed online -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SenateReport:
    """One report, read: its rows, and what its heading says."""

    document: PtrDocument
    report_date: date | None
    amendment: int | None


def parse_report(html: bytes, *, filing_date: date, key: str | None = None) -> SenateReport:
    """A report page filed online, read into rows.

    :param filing_date: When it was filed, the latest a transaction date can
        be: a date after it is a typo, and the row goes to review.
    :param key: Where it is archived, for the error.
    :raises SenateLayoutError: The page is not a report's shape. See the
        module docstring for what is checked.
    """
    try:
        page = lxml.html.document_fromstring(html)
    except Exception:  # lxml's own parser error, or an empty document
        raise SenateLayoutError("a report page that is not HTML", key=key) from None

    headings = _find(page, "//h1")
    if len(headings) != 1:
        raise SenateLayoutError(f"a report page with {len(headings)} h1 headings", key=key)
    try:
        report_date, amendment = _title(_text(headings[0]))
    except SenateLayoutError as failure:
        raise SenateLayoutError(f"the report's heading: {failure}", key=key) from None

    totals = [int(m[1]) for m in _TOTAL.finditer(_text(page))]
    if len(totals) != 1:
        raise SenateLayoutError("a report page without one '(N transactions total)' line", key=key)
    total = totals[0]

    tables = [table for table in _find(page, "//table") if _header(table) == COLUMNS]
    if not tables:
        if total == 0:
            document = PtrDocument(
                Extraction.HTML, pages=0, unreadable="the report lists no transactions"
            )
            return SenateReport(document, report_date, amendment)
        found = [_header(table) for table in _find(page, "//table")]
        raise SenateLayoutError(
            f"no transaction table with the columns {', '.join(COLUMNS)}; tables found: "
            f"{found or 'none'}",
            key=key,
        )
    if len(tables) > 1:
        raise SenateLayoutError(f"{len(tables)} transaction tables, not one", key=key)

    cells_by_row = []
    for tr in _find(tables[0], "./tbody/tr | ./tr[td]"):
        cells = _find(tr, "./td")
        if len(cells) != len(COLUMNS):
            raise SenateLayoutError(
                f"a transaction row of {len(cells)} cells, not {len(COLUMNS)}", key=key
            )
        cells_by_row.append(cells)
    if len(cells_by_row) != total:
        raise SenateLayoutError(
            f"{len(cells_by_row)} transaction rows where the page counts {total}", key=key
        )

    numbers = [_text(cells[0]) for cells in cells_by_row]
    if sorted(numbers, key=lambda n: (len(n), n)) != [str(n) for n in range(1, total + 1)]:
        raise SenateLayoutError(
            f"the # column is not 1 to {total}: {', '.join(numbers[:20])}", key=key
        )

    numbered = zip(numbers, cells_by_row, strict=True)
    rows = sorted(
        (_row(int(number), cells, filing_date) for number, cells in numbered),
        key=lambda row: row.line,
    )
    return SenateReport(
        PtrDocument(Extraction.HTML, pages=0, rows=tuple(rows)), report_date, amendment
    )


def _header(table: HtmlElement) -> tuple[str, ...]:
    return tuple(_text(th) for th in _find(table, "./thead/tr/th | ./tr[th]/th"))


def _row(line: int, cells: Sequence[HtmlElement], filing_date: date) -> PtrRow:
    texts = [_text(cell) for cell in cells]
    _, day, owner, ticker, _, asset_type, kind, amount, comment = texts
    name, description = _asset(cells[4])
    problems: list[str] = []

    transaction_date = parse_date(day, latest=filing_date)
    if transaction_date is None:
        problems.append(f"transaction date {day!r} is not a date")
    elif transaction_date > filing_date:
        problems.append(f"transaction date {day} is after the filing date {filing_date}")

    if owner in OWNERS:
        owner_code = OWNERS[owner]
    else:
        owner_code = None
        problems.append(f"owner {owner!r} is not one eFD was known to use")

    tickers = [None if t in _EMPTY else t.upper() for t in ticker.split()] or [None]
    exchange = _EXCHANGE.match(name or "") if len(tickers) <= 2 else None
    if exchange is not None:
        # One line for both sides, one above the other in each cell: the asset
        # given up, and the one received. The first is the line's.
        name = exchange["given"]
        got = tickers[1] if len(tickers) == 2 else None
        received = exchange["received"] + (f" ({got})" if got else "")
        description = "; ".join(filter(None, (f"Received: {received}", description)))
    elif len(tickers) > 1:
        problems.append(f"ticker {ticker!r} is more than one")
    ticker_value = tickers[0]
    if ticker_value is not None and not _TICKER.match(ticker_value):
        problems.append(f"ticker {ticker!r} does not look like one")

    asset_code = ASSET_TYPES.get(asset_type)
    if asset_code is None and asset_type not in _EMPTY:
        problems.append(f"asset type {asset_type!r} is not one eFD was known to use")

    transaction_type = TRANSACTION_TYPES.get(kind)
    if transaction_type is None:
        problems.append(f"type {kind!r} is not one eFD was known to use")

    band = parse_amount(amount)
    exact = None if band is not None else parse_exact_amount(amount)
    if band is None and exact is None:
        problems.append(f"amount {amount!r} is not one of the bands")

    if name is None:
        # A parsed line names its asset (the parsed_is_complete CHECK); a
        # reviewer can, from the ticker.
        problems.append("no asset name")

    return PtrRow(
        line=line,
        page=None,
        status=RowStatus.NEEDS_REVIEW if problems else RowStatus.PARSED,
        owner=owner_code,
        asset_name=name,
        ticker=ticker_value,
        asset_type=asset_code,
        transaction_type=transaction_type,
        transaction_date=transaction_date,
        amount_low=band[0] if band else None,
        amount_high=band[1] if band else None,
        amount_exact=exact,
        description=description,
        comment=None if comment in _EMPTY else comment,
        problems=tuple(problems),
        raw_text=" | ".join(texts),
    )


def _asset(cell: HtmlElement) -> tuple[str | None, str | None]:
    """The Asset Name cell: the name, and the muted lines under it — an
    option's type, strike and expiry, a bond's coupon and maturity, a private
    company's description — as one line."""
    details = []
    for muted in _find(cell, ".//div[contains(concat(' ', @class, ' '), ' text-muted ')]"):
        details.append(_text(muted))
        muted.drop_tree()
    name = _text(cell)
    detail = "; ".join(d for d in details if d)
    return (None if name in _EMPTY else name), (detail or None)


def _find(node: HtmlElement, path: str) -> list[HtmlElement]:
    """The elements ``path`` selects under ``node``: lxml types an XPath
    result as anything an XPath can return."""
    return [found for found in node.xpath(path) if isinstance(found, HtmlElement)]


def _text(element: HtmlElement) -> str:
    return " ".join(element.text_content().split())
