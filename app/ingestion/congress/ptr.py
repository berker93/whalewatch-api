"""A House Periodic Transaction Report, read: its rows, and how they were read.

The PDF behind each ``congress_filing`` row of the House's
(``/public_disc/ptr-pdfs/{year}/{doc_id}.pdf``) comes in two kinds, and the doc
id says which:

- **E-filed** (``2…``, the Clerk's online form): generated, with a text layer.
  Read by :mod:`app.ingestion.congress.ptr_text`, exactly, and nearly always.
- **Paper** (``8…`` and ``9…``, about one PTR in eight): a scan of the paper
  form, or of a filer's own spreadsheet in its shape, with no text at all.
  Read by OCR (:mod:`app.ingestion.congress.ptr_ocr`) when Tesseract is
  installed, and never trusted blindly: a row OCR is not sure of is written
  ``needs_review``.

:func:`read_ptr` picks the path by whether the pages have text, not by the doc
id, so an e-filed PTR that is a scan, or a paper one with a text layer, is read
the way it can be.

What a row is
-------------
One line of the form's grid: a trade in one asset on one day, by the filer,
their spouse (``SP``), jointly (``JT``) or a dependent child (``DC``). The
amount is the band the form makes filers pick, never a figure.

A row the reader could not make sense of in full — no date, an amount that is
not a band, a type that is not P, S, S (partial) or E, an OCR read below
:data:`~app.ingestion.congress.ptr_ocr.MIN_CONFIDENCE` — is kept, with what it
could read, as ``needs_review`` and the reasons in :attr:`PtrRow.problems`.
It is never dropped: the admin review queue is where a person settles it.

A document from which no row at all can be read — a scan with OCR not
installed, handwriting, a grid that is not the form's — is :attr:`PtrDocument.unreadable`,
and the run writes one placeholder row for it instead, so the filing shows in
the feed as "not machine-readable — view original" rather than not at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

#: Bumped whenever a change to the readers would read some PDF differently, so
#: that ``ingest-house-ptrs`` reads every filing read by an older one again.
PARSER_VERSION: Final = 1


class Extraction(StrEnum):
    """How a document's rows were read."""

    TEXT = "text"
    OCR = "ocr"
    HTML = "html"
    """From the table of a report filed online, as Senate eFD serves it
    (:mod:`app.ingestion.congress.senate_ptr`): exact, like ``text``."""
    NONE = "none"
    """Not at all: no text layer, and OCR either not installed or unable."""


class RowStatus(StrEnum):
    """A row's ``parse_status``. Text with a ``CHECK``, by the rule in
    :mod:`app.db.models.enums`."""

    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"
    """Read in part, or by OCR without confidence: for the review queue."""
    PLACEHOLDER = "placeholder"
    """Stands in for a filing no row could be read from."""
    REJECTED = "rejected"
    """A person in the review queue said this is not a trade (OCR read a
    stamp, or a ruled line, as one). Kept, and left out of the feed."""


class TransactionType(StrEnum):
    PURCHASE = "purchase"
    SALE = "sale"
    PARTIAL_SALE = "partial_sale"
    """``S (partial)``: some of the holding sold."""
    EXCHANGE = "exchange"


#: Who held the asset, as the form's Owner column spells it. No code is the
#: filer themself.
OWNERS: Final = ("SP", "JT", "DC")

#: The form's amount bands: ``(low, high)``, ``None`` for no upper bound.
#: ``$1,000,001 +`` is the spouse's or a child's "over $1,000,000", which the
#: form has as a band of its own.
AMOUNT_BANDS: Final[tuple[tuple[int, int | None], ...]] = (
    (1_001, 15_000),
    (15_001, 50_000),
    (50_001, 100_000),
    (100_001, 250_000),
    (250_001, 500_000),
    (500_001, 1_000_000),
    (1_000_001, 5_000_000),
    (5_000_001, 25_000_000),
    (25_000_001, 50_000_000),
    (50_000_001, None),
    (1_000_001, None),
)

#: Asset type codes a ticker in parentheses belongs to. A bond, an annuity or a
#: fund interest has a parenthesis too — ``(RILA)``, ``(Owner: SP)`` — that is
#: not a ticker.
TICKER_CODES: Final = frozenset({"ST", "OP", "EF", "MF", "OT"})

_MONEY: Final = r"\$?\s*([0-9][0-9,]*)"
_RANGE: Final = re.compile(rf"^{_MONEY}\s*[-\u2013]\s*{_MONEY}$")  # a hyphen or an en dash
_OVER: Final = re.compile(rf"^(?:spouse/dc\s+)?(?:amount\s+)?over\s+{_MONEY}$", re.IGNORECASE)
_EXACT: Final = re.compile(r"^\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)$")
_CODE: Final = re.compile(r"\s*\[([A-Za-z]{2})\]\s*$")
_TICKER: Final = re.compile(r"\s*\(([A-Za-z][A-Za-z0-9.\-]{0,9})\)\s*$")
_DATE: Final = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{2}|\d{4})$")

#: Dates outside these years are a misread or a typo, not a trade.
_FIRST_YEAR: Final = 2000


@dataclass(frozen=True, slots=True)
class PtrRow:
    """One transaction line, as read. ``None`` is "not there, or not legible"."""

    line: int
    """1-based, in the order the document lists them."""
    page: int | None
    """1-based page the row starts on; ``None`` for a row of an HTML report,
    which has no pages."""
    status: RowStatus
    owner: str | None = None
    """``SP``, ``JT`` or ``DC``; ``None`` for the filer."""
    asset_name: str | None = None
    """Without the ticker and the type code: ``Alphabet Inc. - Class A Common Stock``."""
    ticker: str | None = None
    asset_type: str | None = None
    """The two-letter code in brackets: ``ST`` stock, ``OP`` option, ``GS``
    government security. Not on the oldest e-filed form, nor on paper."""
    transaction_type: TransactionType | None = None
    transaction_date: date | None = None
    notification_date: date | None = None
    amount_low: int | None = None
    amount_high: int | None = None
    """``None`` with a low: an open band, "over $50,000,000"."""
    amount_exact: Decimal | None = None
    """A figure the filer wrote instead of a band — "$710.52", "$15.00" cash in
    lieu — which the e-filed form lets through for small amounts. Never with a
    band."""
    cap_gains_over_200: bool | None = None
    """The form's "Cap. Gains > $200?" box, where the document has one."""
    filing_status: str | None = None
    """``new`` or ``amended``, lower-cased."""
    amended_id: str | None = None
    """The ID column: on an amended row, the id of the transaction it amends."""
    subholding_of: str | None = None
    """The account or vehicle it sits in: ``Schwab IRA``."""
    description: str | None = None
    """Free text. On an option: "Purchased 50 call options with a strike price of $150 …"."""
    comment: str | None = None
    location: str | None = None
    confidence: float | None = None
    """0 to 1, for an OCR read. ``None`` for text, which is exact."""
    problems: tuple[str, ...] = ()
    """Why it is ``needs_review``."""
    raw_text: str = ""
    """What the row said, as read, for the reviewer: every line of it."""


@dataclass(frozen=True, slots=True)
class PtrDocument:
    """One PTR, read."""

    extraction: Extraction
    pages: int
    rows: tuple[PtrRow, ...] = ()
    filing_id: str | None = None
    """The "Filing ID #…" the document prints, when it does."""
    unreadable: str | None = None
    """Why no row could be read: the filing gets a placeholder."""
    notes: tuple[str, ...] = field(default_factory=tuple)
    """What the reader noticed and did not act on, for the run's log."""


class PtrPdfError(Exception):
    """The bytes are not a PDF that can be opened at all."""


# --- fields ----------------------------------------------------------------------------


def parse_transaction_type(text: str) -> TransactionType | None:
    """``P``, ``S``, ``S (partial)``, ``E``, any case (the 2013-2019 form's font
    turns some capitals to lower case); also the paper form's words."""
    squeezed = re.sub(r"\s+", "", text).lower()
    return {
        "p": TransactionType.PURCHASE,
        "purchase": TransactionType.PURCHASE,
        "s": TransactionType.SALE,
        "sale": TransactionType.SALE,
        "s(partial)": TransactionType.PARTIAL_SALE,
        "e": TransactionType.EXCHANGE,
        "exchange": TransactionType.EXCHANGE,
    }.get(squeezed)


def parse_amount(text: str) -> tuple[int, int | None] | None:
    """A band, ``(low, high)``, if ``text`` is one of :data:`AMOUNT_BANDS`."""
    cleaned = " ".join(text.split())
    if match := _RANGE.match(cleaned):
        band: tuple[int, int | None] = (_money(match[1]), _money(match[2]))
    elif match := _OVER.match(cleaned):
        band = (_money(match[1]) + 1, None)
    else:
        return None
    return band if band in AMOUNT_BANDS else None


def parse_exact_amount(text: str) -> Decimal | None:
    """A single figure, ``$710.52``, where the form wants a band; ``None`` otherwise."""
    match = _EXACT.match(" ".join(text.split()))
    if match is None:
        return None
    amount = Decimal(match[1].replace(",", ""))
    return amount if amount > 0 else None


def _money(digits: str) -> int:
    return int(digits.replace(",", ""))


def parse_date(text: str, *, latest: date | None = None) -> date | None:
    """``MM/DD/YYYY``, unpadded or not; ``MM/DD/YY`` as the paper form asks;
    dashes or dots for the slashes, as some filers' spreadsheets have them.

    ``None`` for anything else, or a year before 2000 or after ``latest``'s year.
    """
    match = _DATE.match(text.strip())
    if match is None:
        return None
    month, day, year = int(match[1]), int(match[3]), int(match[4])
    if year < 100:
        year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    ceiling = (latest or datetime.now().date()).year + 1
    return parsed if _FIRST_YEAR <= parsed.year <= ceiling else None


def split_asset(text: str) -> tuple[str | None, str | None, str | None]:
    """``Alphabet Inc. - Class A Common Stock (GOOGL) [OP]`` → name, ticker, code.

    The code is upper-cased, and so is the ticker: the 2013-2019 form prints
    ``(AAPl)`` for AAPL. A parenthesis is taken for a ticker only on a code in
    :data:`TICKER_CODES`, or with no code at all (the oldest form had none).
    """
    rest = " ".join(text.split())
    code = None
    if match := _CODE.search(rest):
        code = match[1].upper()
        rest = rest[: match.start()]
    ticker = None
    if (code is None or code in TICKER_CODES) and (match := _TICKER.search(rest)):
        ticker = match[1].upper()
        rest = rest[: match.start()]
    name = rest.strip(" -") or None
    return name, ticker, code


def parse_owner(text: str) -> str | None:
    """``SP``, ``JT``, ``DC``; ``None`` for anything else (blank is the filer)."""
    code = text.strip().upper()
    return code if code in OWNERS else None
