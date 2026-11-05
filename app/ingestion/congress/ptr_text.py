"""An e-filed House PTR, read from its text layer with pdfplumber.

The Clerk's online form renders every PTR the same way, in three versions so
far (2013-2014, 2015-2019 and 2020 on), all of them a grid under one header::

    ID Owner Asset                   Transaction Date       Notification Amount     Cap.
                                     Type                   Date                    Gains >
                                                                                    $200?
       SP    Alphabet Inc. - Class A P           01/14/2025 01/14/2025   $250,001 - [ ]
             Common Stock (GOOGL)                                        $500,000
             [OP]
             Filing Status: New
             Description: Purchased 50 call options with a strike price of $150 …

Why not pdfplumber's table finder
---------------------------------
The grid has no ruled lines, only a shaded rectangle behind every other row,
and :meth:`pdfplumber.page.Page.find_tables` reads those as cells: it merges a
row with the detail lines under it on one row and splits them on the next. The
columns, though, never move — the header's words sit at the same x on every
page of every PTR since 2013, to the point — so the table is found from the
header instead: each header word starts a column, and every word below it
belongs to the column it starts in. Rows are then lines, grouped by height.

A row starts on a line with at least three of: a type in the Type column, a
date in each date column, a dollar figure in the Amount column. The asset's
name and the amount wrap onto the lines after it; then come its details, each
a label in bold — ``Filing Status:``, ``Subholding Of:``, ``Description:``,
``Comments:``, ``Location:`` — whose text may wrap across the whole row.
Details can carry over a page break, under the next page's header.

The labels' text is mostly unreadable: the 2020 form's bold font maps all but
the first letter of each word to nothing (``F\\x00\\x00\\x00\\x00\\x00 S…:`` is
"Filing Status:"). So a label is known by its initials, which survive.

What the 2013-2019 form gets wrong
----------------------------------
Its font maps some capitals to lower-case letters: ``(AAPl)``, ``danaher
Corporation``, ``Il17``. A ticker and a type code are upper-cased, which
repairs them; an asset's name is kept as printed. Its cap-gains box is a
glyph, ``gfedc`` in the Marlett font, with a ``b`` (a tick) when checked; the
2020 form's is a drawn box with a tick drawn inside it.

The table ends at the first line that starts left of the Owner column without
being a row — the asset-code footnote, or the next section's heading. A row
whose fields do not all read is kept as ``needs_review`` (:mod:`app.ingestion.congress.ptr`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from pdfplumber.page import Page
from pdfplumber.pdf import PDF

from app.ingestion.congress.ptr import (
    Extraction,
    PtrDocument,
    PtrRow,
    RowStatus,
    parse_amount,
    parse_date,
    parse_exact_amount,
    parse_owner,
    parse_transaction_type,
    split_asset,
)

#: The header's words, lower-cased, by the column they start.
_HEADER: Final = {
    "id": "id",
    "owner": "owner",
    "asset": "asset",
    "transaction": "type",
    "date": "date",
    "notification": "notified",
    "amount": "amount",
    "cap.": "cap",
}

#: Words on the header's second and third lines.
_HEADER_TAIL: Final = frozenset({"type", "date", "gains", ">", "$200?"})

#: A detail label's initials, by the field it fills.
_LABELS: Final = {
    "FS": "filing_status",
    "SO": "subholding_of",
    "D": "description",
    "C": "comment",
    "L": "location",
}

#: A label spelled out, lower-cased, by the field it fills: the 2013-2019 form.
_SPELLED: Final = {
    "filing status": "filing_status",
    "subholding of": "subholding_of",
    "description": "description",
    "comments": "comment",
    "comment": "comment",
    "location": "location",
}

#: How a label reads in a row's raw text.
_LABEL_NAMES: Final = {
    "filing_status": "Filing Status",
    "subholding_of": "Subholding Of",
    "description": "Description",
    "comment": "Comments",
    "location": "Location",
}

#: A word may start this far left of its column's header word and be in it.
_SLACK: Final = 4.0

#: Words whose tops are this close are on one line.
_LINE_TOLERANCE: Final = 3.0

_DATE_SHAPE: Final = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_TYPE_SHAPE: Final = re.compile(r"^[PSEpse]$")
_FILING_ID: Final = re.compile(r"Filing\s+I[Dd]\s*#\s*([0-9A-Za-z-]+)")

#: The drawn cap-gains box on the 2020 form is about 9.5 points square; the
#: tick drawn inside a checked one is under 7.
_BOX_MIN: Final = 8.0
_TICK_MAX: Final = 7.5


@dataclass(slots=True)
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    marlett: bool


@dataclass(slots=True)
class _Line:
    words: list[_Word]

    @property
    def top(self) -> float:
        return min(word.top for word in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    def text(self) -> str:
        return " ".join(_clean(word.text) for word in self.words).strip()


@dataclass(slots=True)
class _Columns:
    starts: list[tuple[float, str]]
    """``(x, name)``, left to right."""

    def of(self, word: _Word) -> str:
        name = self.starts[0][1]
        for x, column in self.starts:
            if word.x0 >= x - _SLACK:
                name = column
        return name

    def start(self, name: str) -> float | None:
        return next((x for x, column in self.starts if column == name), None)

    def split(self, line: _Line) -> dict[str, list[_Word]]:
        cells: dict[str, list[_Word]] = {}
        for word in line.words:
            cells.setdefault(self.of(word), []).append(word)
        return cells


@dataclass(slots=True)
class _Row:
    page: int
    top: float
    cells: dict[str, list[str]] = field(default_factory=dict)
    details: dict[str, list[str]] = field(default_factory=dict)
    current: str | None = None
    """The detail field a wrapped line continues; ``None`` before the first."""
    cap: bool | None = None
    raw: list[str] = field(default_factory=list)

    def add(self, column: str, words: Iterable[_Word]) -> None:
        self.cells.setdefault(column, []).extend(_clean(word.text) for word in words)

    def text(self, column: str) -> str:
        return " ".join(self.cells.get(column, [])).strip()


def read_text_layer(pdf: PDF, *, latest: date | None = None) -> PtrDocument:
    """Every row in an e-filed PTR's grid.

    :param latest: The filing's date, past which a transaction date is a typo.
    """
    rows: list[_Row] = []
    columns: _Columns | None = None
    open_table = False
    ended = False
    notes: list[str] = []
    for number, page in enumerate(pdf.pages, start=1):
        if ended:
            break
        lines = _lines(page)
        header_at = next((i for i, line in enumerate(lines) if _is_header(line)), None)
        if header_at is not None:
            columns = _columns(lines[header_at])
            body = _after_header(lines, header_at)
            open_table = True
        elif open_table and columns is not None:
            body = lines
        else:
            continue
        assert columns is not None
        boxes = _cap_boxes(page, columns)
        page_rows: list[_Row] = []
        for line in body:
            if _ends_table(line, columns):
                ended = True
                break
            cells = columns.split(line)
            if _starts_row(cells):
                row = _Row(page=number, top=line.top)
                for column, words in cells.items():
                    row.add(column, words)
                row.cap = _cap_glyph(cells.get("cap", []))
                row.raw.append(line.text())
                rows.append(row)
                page_rows.append(row)
                continue
            if not rows:
                notes.append(f"page {number}: text before the first row: {line.text()!r}")
                continue
            _continue(rows[-1], line, cells)
        _place_boxes(page_rows, boxes)

    filing_id = _filing_id(pdf)
    if columns is None:
        return PtrDocument(
            Extraction.TEXT,
            pages=len(pdf.pages),
            filing_id=filing_id,
            unreadable="the text layer has no transaction table",
        )
    read = tuple(_finish(row, line, latest) for line, row in enumerate(rows, start=1))
    return PtrDocument(
        Extraction.TEXT,
        pages=len(pdf.pages),
        rows=read,
        filing_id=filing_id,
        unreadable=None if read else "the transaction table has no rows",
        notes=tuple(notes),
    )


# --- lines and columns -----------------------------------------------------------------


def _lines(page: Page) -> list[_Line]:
    words = page.extract_words(
        x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False, extra_attrs=["fontname"]
    )
    parsed = [
        _Word(
            text=word["text"],
            x0=float(word["x0"]),
            x1=float(word["x1"]),
            top=float(word["top"]),
            bottom=float(word["bottom"]),
            marlett="marlett" in word["fontname"].lower(),
        )
        for word in words
    ]
    parsed.sort(key=lambda word: (word.top, word.x0))
    lines: list[_Line] = []
    for word in parsed:
        if lines and abs(word.top - lines[-1].top) <= _LINE_TOLERANCE:
            lines[-1].words.append(word)
        else:
            lines.append(_Line([word]))
    for line in lines:
        line.words.sort(key=lambda word: word.x0)
    return lines


def _is_header(line: _Line) -> bool:
    texts = {word.text.lower() for word in line.words}
    return {"owner", "asset", "notification"} <= texts


def _columns(line: _Line) -> _Columns:
    starts: list[tuple[float, str]] = []
    seen: set[str] = set()
    for word in line.words:
        name = _HEADER.get(word.text.lower())
        if name is not None and name not in seen:
            starts.append((word.x0, name))
            seen.add(name)
    starts.sort()
    return _Columns(starts)


def _after_header(lines: Sequence[_Line], at: int) -> list[_Line]:
    rest = list(lines[at + 1 :])
    while rest and {word.text.lower() for word in rest[0].words} <= _HEADER_TAIL:
        rest.pop(0)
    return rest


def _ends_table(line: _Line, columns: _Columns) -> bool:
    """Left of the Owner column and not a row: the footnote, or a section heading."""
    owner = columns.start("owner")
    if owner is None or line.x0 >= owner - _SLACK:
        return False
    first = line.words[0].text
    return not (first.isdigit() and _starts_row(columns.split(line)))


def _starts_row(cells: dict[str, list[_Word]]) -> bool:
    signs = 0
    kind = [word.text for word in cells.get("type", [])]
    if kind and _TYPE_SHAPE.match(kind[0]) and len(kind) <= 2:
        signs += 1
    for column in ("date", "notified"):
        words = cells.get(column, [])
        if words and _DATE_SHAPE.match(words[0].text):
            signs += 1
    amount = cells.get("amount", [])
    if amount and amount[0].text.startswith("$"):
        signs += 1
    return signs >= 3


def _continue(row: _Row, line: _Line, cells: dict[str, list[_Word]]) -> None:
    """A line under a row's first: more of its asset and amount, or its details."""
    label, rest = _label(line)
    if label is not None:
        row.current = label
        row.details.setdefault(label, [])
        text = " ".join(_clean(word.text) for word in rest).strip()
        if text:
            row.details[label].append(text)
        row.raw.append(f"{_LABEL_NAMES[label]}: {text}")
        return
    row.raw.append(line.text())
    if row.current is not None:
        # A detail's text, wrapped: it runs across every column.
        row.details[row.current].append(line.text())
        return
    for column, words in cells.items():
        if column == "cap":
            row.cap = _cap_glyph(words) if row.cap is None else row.cap
        else:
            row.add(column, words)


def _label(line: _Line) -> tuple[str | None, list[_Word]]:
    """A ``Label:`` of one to three words at the start of the line, and the rest.

    Known by its initials where the font has blanked the other letters, and by
    its words where it has not — so that "Sold: 100 shares" in a wrapped
    description is not a label.
    """
    for at, word in enumerate(line.words[:3]):
        if not word.text.endswith(":"):
            continue
        label = line.words[: at + 1]
        if any("\x00" in part.text for part in label):
            initials = "".join(part.text[:1].upper() for part in label)
            name = _LABELS.get(initials)
        else:
            spelled = " ".join(part.text for part in label).rstrip(":").lower()
            name = _SPELLED.get(spelled)
        return (name, line.words[at + 1 :]) if name else (None, line.words)
    return None, line.words


# --- the cap-gains box -----------------------------------------------------------------


def _cap_glyph(words: Sequence[_Word]) -> bool | None:
    """The 2013-2019 form's Marlett box: ``gfedc``, ticked ``gfedcb``."""
    glyphs = "".join(word.text for word in words if word.marlett)
    if "gfedc" not in glyphs:
        return None
    return "b" in glyphs.replace("gfedc", "", 1) or glyphs.endswith("b")


@dataclass(frozen=True, slots=True)
class _Box:
    top: float
    ticked: bool


def _cap_boxes(page: Page, columns: _Columns) -> list[_Box]:
    """The 2020 form's drawn boxes in the Cap. column, ticked or not."""
    left = columns.start("cap")
    if left is None:
        return []
    curves: list[dict[str, Any]] = [
        curve for curve in page.curves if float(curve["x0"]) >= left - _SLACK
    ]
    boxes = [
        curve
        for curve in curves
        if _BOX_MIN <= float(curve["x1"]) - float(curve["x0"]) <= _BOX_MIN * 2
    ]
    ticks = [
        curve
        for curve in curves
        if float(curve["x1"]) - float(curve["x0"]) <= _TICK_MAX
        and float(curve["bottom"]) - float(curve["top"]) <= _TICK_MAX
    ]
    found: list[_Box] = []
    for box in boxes:
        top = float(box["top"])
        if any(abs(existing.top - top) < 1 for existing in found):
            continue  # the box is drawn as three curves: shadow, fill, outline
        ticked = any(
            float(box["x0"]) - 1 <= float(tick["x0"])
            and float(tick["x1"]) <= float(box["x1"]) + 1
            and top - 1 <= float(tick["top"])
            and float(tick["bottom"]) <= float(box["bottom"]) + 1
            for tick in ticks
        )
        found.append(_Box(top, ticked))
    return found


def _place_boxes(rows: Sequence[_Row], boxes: Sequence[_Box]) -> None:
    """Each box to the row whose first line it sits beside."""
    for box in boxes:
        candidates = [row for row in rows if abs(row.top - box.top) <= 8 and row.cap is None]
        if candidates:
            nearest = min(candidates, key=lambda row: abs(row.top - box.top))
            nearest.cap = box.ticked


# --- a row's fields --------------------------------------------------------------------


def _finish(row: _Row, line: int, latest: date | None) -> PtrRow:
    problems: list[str] = []
    asset_text = row.text("asset")
    name, ticker, code = split_asset(asset_text)
    if name is None:
        problems.append("no asset name")

    type_text = row.text("type")
    kind = parse_transaction_type(type_text)
    if kind is None:
        problems.append(f"transaction type {type_text!r} is not P, S, S (partial) or E")

    dates: dict[str, date | None] = {}
    for column, label in (("date", "transaction date"), ("notified", "notification date")):
        text = row.text(column)
        dates[column] = parse_date(text, latest=latest) if text else None
        if dates[column] is None and (text or column == "date"):
            problems.append(f"{label} {text!r} is not a date")

    amount_text = row.text("amount")
    band = parse_amount(amount_text)
    # A figure instead of a band is what the filer reported, and exact.
    exact = parse_exact_amount(amount_text) if band is None else None
    if band is None and exact is None:
        problems.append(f"amount {amount_text!r} is not one of the form's bands")

    owner_text = row.text("owner")
    owner = parse_owner(owner_text)
    if owner_text and owner is None:
        problems.append(f"owner {owner_text!r} is not SP, JT or DC")

    detail = {name: " ".join(parts).strip() or None for name, parts in row.details.items()}
    status = detail.get("filing_status")
    return PtrRow(
        line=line,
        page=row.page,
        status=RowStatus.NEEDS_REVIEW if problems else RowStatus.PARSED,
        owner=owner,
        asset_name=name,
        ticker=ticker,
        asset_type=code,
        transaction_type=kind,
        transaction_date=dates["date"],
        notification_date=dates["notified"],
        amount_low=band[0] if band else None,
        amount_high=band[1] if band else None,
        amount_exact=exact,
        cap_gains_over_200=row.cap,
        filing_status=status.lower() if status else None,
        amended_id=row.text("id") or None,
        subholding_of=detail.get("subholding_of"),
        description=detail.get("description"),
        comment=detail.get("comment"),
        location=detail.get("location"),
        problems=tuple(problems),
        raw_text="\n".join(row.raw),
    )


def _filing_id(pdf: PDF) -> str | None:
    if not pdf.pages:
        return None
    match = _FILING_ID.search(pdf.pages[0].extract_text() or "")
    return match[1] if match else None


def _clean(text: str) -> str:
    return text.replace("\x00", "")
