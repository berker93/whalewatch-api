"""A paper House PTR, read by OCR: the form's grid found in the scan, cell by cell.

A paper PTR is a scan of the Clerk's printed form — or of a filer's own
spreadsheet laid out like it — at 200 dpi, with no text layer::

    | JT |                  | TYPE OF TRANS- | DATE OF | DATE     | AMOUNT OF TRANSACTION       |
    | SP | FULL ASSET NAME  |    ACTION      | TRANS-  | NOTIFIED | A | B | C | … | J | (K) |
    | DC |                  | P  |  S  |  E  | ACTION  |          |   |   |   |   |   |     |
    | JT | Tractor Supply Co|    |  X  |     | 06/09/26| 07/01/26 | X |   |   |   |   |     |

The type and the amount are not written but ticked: an X in one of three
boxes, and in one of ten (eleven, on the spreadsheets that add the spouse's
"over $1,000,000" as K). OCR of the page as a whole reads none of it — the
stamps, the rotated headings and the rules swamp it — so the page is read as
the grid it is:

1. **Rules.** Dark runs a sizeable fraction of the page long, horizontal and
   vertical, found by projection (:func:`_rules`). The vertical rules that run
   through the body are the columns; the horizontal ones that span the grid are
   the rows.
2. **Columns, by shape.** Not by their headings, which are rotated, small and
   stamped over: from the right, the run of narrow amount boxes (10 or 11);
   then two date columns; then the three type boxes; then the asset, the
   widest; then the owner. A grid that is not that shape is not read.
3. **Rows.** The header is the tall bands at the top. The form's printed
   example (``Example: Mega Corp Common Stock``) is skipped, as are rows with
   nothing in them.
4. **Cells.** Owner, asset and the two dates are OCR'd by Tesseract a column at
   a time — the column's cells stacked into one image, one call each — with
   its word confidences. A box is ticked if ink covers enough of it, and the
   row's type and amount are the ticked box when exactly one in the group is.

Confidence
----------
A row's :attr:`~app.ingestion.congress.ptr.PtrRow.confidence` is the least of:
Tesseract's mean confidence in the asset and in each date, and for the type
and the amount, how far the ticked box's ink stands above the next one's. A
row under :data:`MIN_CONFIDENCE`, or with a field that does not parse, is
``needs_review``. Handwriting reads under it nearly always, which is the point:
OCR'd handwriting is a guess, and a person makes it.

Ticker and type code are never read: the form asks for the asset's full name,
"not ticker symbol", and has no code column.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import pairwise, product
from typing import Final, Protocol

import numpy as np
import numpy.typing as npt
import pypdfium2
from PIL import Image

from app.ingestion.congress.ptr import (
    AMOUNT_BANDS,
    Extraction,
    PtrDocument,
    PtrRow,
    RowStatus,
    TransactionType,
    parse_date,
    parse_owner,
)

#: A row OCR is less sure of than this is ``needs_review``.
MIN_CONFIDENCE: Final = 0.80

#: Pages are rendered at this resolution: Tesseract reads best at about 300.
RENDER_DPI: Final = 300

#: The most pages read from one scan: the longest paper PTR so far is 41.
MAX_PAGES: Final = 80

#: A pixel darker than this is ink.
_INK: Final = 160

#: A horizontal rule runs at least this share of the page's width; a vertical
#: one at least this share of the grid's body.
_H_RULE: Final = 0.30
_V_RULE: Final = 0.60

#: Rules this close together (pixels at :data:`RENDER_DPI`) are one rule.
_MERGE: Final = 6

#: Pixels trimmed off each side of a cell before reading it: the rule, and
#: the scan's blur around it.
_INSET: Final = 7

#: A straight run this share of a box's side long is print, not a mark.
_STRAIGHT: Final = 0.30

#: A box is ticked when a mark covers this share of it — a typed x in a
#: thin row is about 0.025, an empty box's leftover print up to 0.03 …
_TICKED: Final = 0.018
#: … and when the next box in its group has under this share of its mark,
#: which is what tells a tick from the noise.
_CLEAR: Final = 0.5

#: Quarter turns tried, counter-clockwise, as :func:`numpy.rot90` counts them:
#: upright, then a page scanned turned left, turned right, upside down.
_QUARTER_TURNS: Final = (0, 3, 1, 2)

#: The fewest columns the form has: owner, asset, three types, two dates and
#: ten amounts.
_MIN_COLUMNS: Final = 17

#: The amount boxes, left to right: A to J, then the spreadsheets' K.
_AMOUNT_COLUMNS: Final = (10, 11)

_DIGITS: Final = "0123456789/-."

#: The confidence a pair of dates that parse and agree is given, whatever
#: Tesseract said: enough to pass :data:`MIN_CONFIDENCE`.
_AGREEING_DATES: Final = 0.85

#: What a stray mark at the start of a cell reads as.
_JUNK: Final = " _-|.,:;'\"`~\u2018\u2019\u201c\u201d"

_TYPES_3: Final = (TransactionType.PURCHASE, TransactionType.SALE, TransactionType.EXCHANGE)
_TYPES_4: Final = (
    TransactionType.PURCHASE,
    TransactionType.SALE,
    TransactionType.PARTIAL_SALE,
    TransactionType.EXCHANGE,
)

Gray = npt.NDArray[np.uint8]
Mask = npt.NDArray[np.bool_]


# --- Tesseract -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    """0 to 100, as Tesseract reports it."""


class Ocr(Protocol):
    """What reading a scan needs of an OCR engine."""

    def words(
        self, image: Image.Image, *, psm: int, whitelist: str | None = None
    ) -> list[OcrWord]: ...


class OcrError(Exception):
    """Tesseract failed, or is not there."""


class Tesseract:
    """The ``tesseract`` command, one subprocess a call, TSV out."""

    def __init__(self, command: str = "tesseract", *, timeout: float = 120.0) -> None:
        self.command = command
        self.timeout = timeout

    @classmethod
    def find(cls, command: str = "tesseract") -> Tesseract | None:
        """The engine, if ``command`` is on the path; ``None`` if OCR is not installed."""
        found = shutil.which(command)
        return cls(found) if found else None

    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        arguments = [self.command, "stdin", "stdout", "--psm", str(psm), "--dpi", str(RENDER_DPI)]
        if whitelist:
            arguments += ["-c", f"tessedit_char_whitelist={whitelist}"]
        arguments.append("tsv")
        try:
            done = subprocess.run(
                arguments,
                input=buffer.getvalue(),
                capture_output=True,
                timeout=self.timeout,
                check=False,
                # One thread each: the run reads several scans at once already.
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as failure:
            raise OcrError(f"tesseract did not run: {failure}") from failure
        if done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip()[:300]
            raise OcrError(f"tesseract exited {done.returncode}: {message}")
        return _tsv_words(done.stdout.decode("utf-8", errors="replace"))


def _tsv_words(tsv: str) -> list[OcrWord]:
    words: list[OcrWord] = []
    for record in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (record.get("text") or "").strip()
        if record.get("level") != "5" or not text:
            continue
        words.append(
            OcrWord(
                text=text,
                left=int(record["left"]),
                top=int(record["top"]),
                width=int(record["width"]),
                height=int(record["height"]),
                confidence=max(0.0, float(record["conf"])),
            )
        )
    return words


# --- the document ----------------------------------------------------------------------


def read_scan(pdf: pypdfium2.PdfDocument, ocr: Ocr, *, latest: date | None = None) -> PtrDocument:
    """Every row OCR can find in a scanned PTR's grids.

    :raises OcrError: Tesseract failed on a page.
    """
    pages = len(pdf)
    rows: list[PtrRow] = []
    notes: list[str] = []
    grids = 0
    for number in range(min(pages, MAX_PAGES)):
        found = _find_form(_render(pdf[number]))
        if found is None:
            notes.append(f"page {number + 1}: no form grid")
            continue
        grids += 1
        gray, grid, layout = found
        for read in _read_grid(gray, grid, layout, ocr, latest=latest):
            rows.append(_numbered(read, line=len(rows) + 1, page=number + 1))
    if pages > MAX_PAGES:
        notes.append(f"pages after {MAX_PAGES} not read")
    if not grids:
        unreadable = "OCR found no form grid on any page"
    elif not rows:
        unreadable = "OCR found the form's grid but no legible row in it"
    else:
        unreadable = None
    return PtrDocument(
        Extraction.OCR, pages=pages, rows=tuple(rows), unreadable=unreadable, notes=tuple(notes)
    )


def _find_form(gray: Gray) -> tuple[Gray, _Grid, _Layout] | None:
    """The page turned upright, its grid and the form's columns in it.

    A landscape form is often scanned onto a portrait page, either way round:
    each quarter turn is tried. The grid alone cannot tell up from down, but
    the form's columns can — the amount boxes are on the right — so the turn
    kept is the first whose grid is the form's.
    """
    for quarter in _QUARTER_TURNS:
        turned = np.ascontiguousarray(np.rot90(gray, quarter)) if quarter else gray
        upright = _deskew(turned)
        grid = _grid(upright)
        if grid is None:
            continue
        layout = _layout(grid.columns)
        if layout is not None:
            return upright, grid, layout
    return None


def _numbered(row: PtrRow, *, line: int, page: int) -> PtrRow:
    return replace(row, line=line, page=page)


def _render(page: pypdfium2.PdfPage) -> Gray:
    bitmap = page.render(scale=RENDER_DPI / 72, grayscale=True)
    image = bitmap.to_pil().convert("L")
    return np.asarray(image, dtype=np.uint8)


# --- rules and the grid ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Grid:
    columns: list[int]
    """Vertical rules' x, left to right: ``len - 1`` columns."""
    bands: list[tuple[int, int]]
    """The body's rows, ``(top, bottom)`` rule to rule: the header left out."""


def _runs(ink: Mask, length: int, axis: int) -> Mask:
    """Pixels that start a run of ``length`` inked pixels along ``axis``."""
    counts = np.cumsum(ink, axis=axis, dtype=np.int32)
    if axis == 1:
        padded = np.pad(counts, ((0, 0), (1, 0)))
        window = padded[:, length:] - padded[:, :-length]
    else:
        padded = np.pad(counts, ((1, 0), (0, 0)))
        window = padded[length:, :] - padded[:-length, :]
    return window >= length - 1  # a pixel's gap in a scanned rule is forgiven


def _rules(coverage: npt.NDArray[np.int64], threshold: float) -> list[tuple[int, float]]:
    """Each run of positions whose coverage passes ``threshold``: its centre,
    and its strongest coverage."""
    hits = np.flatnonzero(coverage >= threshold)
    rules: list[tuple[int, float]] = []
    group: list[int] = []

    def close() -> None:
        rules.append((round(sum(group) / len(group)), float(coverage[group].max())))

    for position in hits.tolist():
        if group and position - group[-1] > _MERGE:
            close()
            group = []
        group.append(position)
    if group:
        close()
    return rules


def _grid(gray: Gray) -> _Grid | None:
    """The transaction grid: the longest run of rows ruled into the form's
    columns, its taller header rows left off the top."""
    width = gray.shape[1]
    ink = gray < _INK
    across = _runs(ink, max(20, width // 20), axis=1).sum(axis=1)
    horizontal = [y for y, _ in _rules(across, _H_RULE * width)]
    bands = list(pairwise(horizontal))
    ruled = [_band_rules(ink, top, bottom) for top, bottom in bands]
    full = [at for at, rules in enumerate(ruled) if len(rules) >= _MIN_COLUMNS + 1]
    if not full:
        return None
    runs: list[list[int]] = [[full[0]]]
    for at in full[1:]:
        if at == runs[-1][-1] + 1:
            runs[-1].append(at)
        else:
            runs.append([at])
    run = max(runs, key=len)
    usual = float(np.median([bands[at][1] - bands[at][0] for at in run]))
    while run and bands[run[0]][1] - bands[run[0]][0] > usual * 1.5:
        run = run[1:]
    if not run:
        return None
    columns = _consensus([ruled[at] for at in run])
    if len(columns) < _MIN_COLUMNS + 1:
        return None
    return _Grid(columns=columns, bands=[bands[at] for at in run])


def _band_rules(ink: Mask, top: int, bottom: int) -> list[int]:
    """Vertical rules crossing one row of the grid, top to bottom."""
    region = ink[top + 3 : bottom - 2, :]
    if region.shape[0] < 8:
        return []
    return [x for x, _ in _rules(region.sum(axis=0), 0.9 * region.shape[0])]


def _consensus(per_band: Sequence[Sequence[int]]) -> list[int]:
    """The rules most rows have, each at its median x, with any that would make
    a sliver of a column — a stamp's edge, a doubled rule — dropped."""
    clusters: list[list[int]] = []
    for x in sorted(x for rules in per_band for x in rules):
        if clusters and x - clusters[-1][-1] <= _MERGE * 2:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    kept = [
        (round(float(np.median(cluster))), len(cluster))
        for cluster in clusters
        if len(cluster) >= len(per_band) / 2
    ]
    while len(kept) > 2:
        widths = [b[0] - a[0] for a, b in pairwise(kept)]
        usual = float(np.median(widths))
        narrowest = min(range(len(widths)), key=widths.__getitem__)
        if widths[narrowest] >= usual * 0.45:
            break
        left, right = kept[narrowest], kept[narrowest + 1]
        kept.remove(left if left[1] < right[1] else right)
    return [x for x, _ in kept]


def _deskew(gray: Gray) -> Gray:
    """The page turned so its rules are level: a scan a degree off defeats
    the projections. The angle is the one, within two degrees, under which
    the rows of ink are sharpest."""
    small = Image.fromarray(gray).reduce(4)
    ink = (np.asarray(small) < _INK).astype(np.float64)
    best, best_score = 0.0, -1.0
    image = Image.fromarray((ink * 255).astype(np.uint8))
    for tenth in range(-20, 21, 2):
        angle = tenth / 10
        turned = np.asarray(image.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0))
        profile = turned.sum(axis=1, dtype=np.float64)
        score = float((profile**2).sum())
        if score > best_score:
            best, best_score = angle, score
    if best == 0.0:
        return gray
    level = Image.fromarray(gray).rotate(best, resample=Image.Resampling.BICUBIC, fillcolor=255)
    return np.asarray(level, dtype=np.uint8)


@dataclass(frozen=True, slots=True)
class _Layout:
    owner: tuple[int, int] | None
    asset: tuple[int, int]
    kinds: tuple[TransactionType, ...]
    """What each type box means, left to right."""
    types: tuple[tuple[int, int], ...]
    traded: tuple[int, int]
    notified: tuple[int, int]
    amounts: tuple[tuple[int, int], ...]


def _layout(columns: Sequence[int]) -> _Layout | None:
    """The form's columns, by width, from the right. ``None`` if it is not the form.

    From the right: 10 or 11 amount boxes alike; two date columns, wider than
    a type box; three type boxes (P, S, E) or four (P, S, partial sale, E, the
    form before 2019); and directly left of them the asset, the widest column,
    with the owner left of it.
    """
    cells = list(pairwise(columns))
    widths = [b - a for a, b in cells]
    for count, kinds in product(sorted(_AMOUNT_COLUMNS, reverse=True), (_TYPES_4, _TYPES_3)):
        boxes = len(kinds)
        if len(cells) < count + boxes + 3:
            continue
        narrow = widths[-count:]
        if max(narrow) > 1.6 * min(narrow):
            continue
        rest, rest_widths = cells[:-count], widths[:-count]
        type_widths = rest_widths[-boxes - 2 : -2]
        if min(rest_widths[-2:]) < 1.3 * max(type_widths):
            continue
        if max(type_widths) > 1.8 * min(type_widths):
            continue
        before = rest[: -boxes - 2]
        asset_at = len(before) - 1
        widest = max(range(len(before)), key=lambda at: before[at][1] - before[at][0])
        if widest != asset_at:
            continue
        return _Layout(
            owner=before[asset_at - 1] if asset_at >= 1 else None,
            asset=before[asset_at],
            kinds=kinds,
            types=tuple(rest[-boxes - 2 : -2]),
            traded=rest[-2],
            notified=rest[-1],
            amounts=tuple(cells[-count:]),
        )
    return None


# --- reading the cells -----------------------------------------------------------------


@dataclass(slots=True)
class _Read:
    """One column's OCR, by body band."""

    text: dict[int, str] = field(default_factory=dict)
    confidence: dict[int, float] = field(default_factory=dict)
    inked: set[int] = field(default_factory=set)
    """Bands whose cell has ink in it, read or not."""


def _read_grid(
    gray: Gray, grid: _Grid, layout: _Layout, ocr: Ocr, *, latest: date | None
) -> list[PtrRow]:
    bands = grid.bands
    ink = gray < _INK
    # psm 4, a column of lines of varying size, for names; 6, a uniform block,
    # for the dates and codes.
    asset = _read_column(gray, ink, bands, layout.asset, ocr, psm=4)
    traded = _read_column(gray, ink, bands, layout.traded, ocr, psm=6, whitelist=_DIGITS)
    notified = _read_column(gray, ink, bands, layout.notified, ocr, psm=6, whitelist=_DIGITS)
    owner = (
        _read_column(gray, ink, bands, layout.owner, ocr, psm=6, whitelist="JTSPDC")
        if layout.owner is not None
        else _Read()
    )
    # Ink over what an empty box has: the older form prints a square in each.
    types = _excess([[_coverage(ink, *band, box) for box in layout.types] for band in bands])
    amounts = _excess([[_coverage(ink, *band, box) for box in layout.amounts] for band in bands])
    rows: list[PtrRow] = []
    for at in range(len(bands)):
        name = asset.text.get(at, "")
        if name.lower().startswith("example") or "mega corp" in name.lower():
            continue
        dates = (traded.text.get(at, ""), notified.text.get(at, ""))
        inked = at in asset.inked or at in traded.inked or at in notified.inked
        # A line with no writing is empty unless it has both ticks a trade has:
        # one box's leftover print is not a trade.
        if not inked and (_ticked(types[at])[0] is None or _ticked(amounts[at])[0] is None):
            continue
        rows.append(
            _row(
                name=name,
                name_confidence=asset.confidence.get(at, 0.0),
                owner=owner.text.get(at, ""),
                owner_inked=at in owner.inked,
                traded=dates[0],
                traded_confidence=traded.confidence.get(at, 0.0),
                notified=dates[1],
                notified_inked=at in notified.inked,
                notified_confidence=notified.confidence.get(at, 0.0),
                kinds=layout.kinds,
                types=types[at],
                amounts=amounts[at],
                latest=latest,
            )
        )
    return rows


def _excess(coverage: Sequence[Sequence[float]]) -> list[list[float]]:
    """Each box's ink over the median box's in its group, on this page: most
    boxes are empty, so the median is an empty one."""
    flat = [value for row in coverage for value in row]
    if not flat:
        return [list(row) for row in coverage]
    empty = float(np.median(flat))
    return [[max(0.0, value - empty) for value in row] for row in coverage]


def _coverage(ink: Mask, top: int, bottom: int, box: tuple[int, int]) -> float:
    """The share of a box that is a mark: its ink once every long straight
    run is taken out. A printed square, or a rule, is straight; an X or a tick
    is diagonal, and its runs across or down are only as long as it is thick."""
    cell = ink[top + 3 : bottom - 2, box[0] + 3 : box[1] - 2]
    if cell.size == 0:
        return 0.0
    length = max(8, round(min(cell.shape) * _STRAIGHT))
    straight = _in_runs(cell, length, axis=1) | _in_runs(cell, length, axis=0)
    return float((cell & ~straight).mean())


def _in_runs(ink: Mask, length: int, axis: int) -> Mask:
    """Pixels that are part of a run of ``length`` or more along ``axis``."""
    if ink.shape[axis] < length:
        return np.zeros_like(ink)
    starts = _runs(ink, length, axis).astype(np.int32)
    # A pixel is covered if a run starts within ``length`` before it.
    counts = np.cumsum(starts, axis=axis)
    if axis == 1:
        counts = np.pad(counts, ((0, 0), (0, ink.shape[1] - counts.shape[1])), mode="edge")
        before = np.pad(counts, ((0, 0), (length, 0)))[:, : ink.shape[1]]
    else:
        counts = np.pad(counts, ((0, ink.shape[0] - counts.shape[0]), (0, 0)), mode="edge")
        before = np.pad(counts, ((length, 0), (0, 0)))[: ink.shape[0], :]
    return (counts - before) > 0


def _read_column(
    gray: Gray,
    ink: Mask,
    bands: Sequence[tuple[int, int]],
    column: tuple[int, int],
    ocr: Ocr,
    *,
    psm: int,
    whitelist: str | None = None,
) -> _Read:
    """OCR every body cell of one column in one call: the cells stacked, with
    white between them, and each word given back to the cell it came from."""
    left, right = column[0] + _INSET, column[1] - _INSET
    gap = 24
    pieces: list[tuple[int, Gray]] = []
    for at, (top, bottom) in enumerate(bands):
        cell = gray[top + _INSET : bottom - _INSET, left:right]
        if cell.size == 0:
            continue
        cleaned = _clean_cell(cell)
        if (cleaned < _INK).mean() < 0.002:
            continue  # specks
        pieces.append((at, cleaned))
    read = _Read(inked={at for at, _ in pieces})
    if not pieces:
        return read
    width = right - left
    height = sum(piece.shape[0] + gap for _, piece in pieces) + gap
    sheet = np.full((height, width + 2 * gap), 255, dtype=np.uint8)
    offsets: list[tuple[int, int, int]] = []
    y = gap
    for at, piece in pieces:
        sheet[y : y + piece.shape[0], gap : gap + piece.shape[1]] = piece
        offsets.append((at, y, y + piece.shape[0]))
        y += piece.shape[0] + gap
    words = ocr.words(Image.fromarray(sheet), psm=psm, whitelist=whitelist)
    by_cell: dict[int, list[OcrWord]] = {}
    for word in words:
        if not any(char.isalnum() for char in word.text):
            continue  # a speck read as punctuation, at confidence 0
        middle = word.top + word.height / 2
        for at, start, end in offsets:
            if start - gap / 2 <= middle < end + gap / 2:
                by_cell.setdefault(at, []).append(word)
                break
    for at, found in by_cell.items():
        # Tesseract's own reading order, which a wrapped name needs.
        read.text[at] = " ".join(word.text for word in found)
        # The weakest word: one misread letter is a wrong name.
        read.confidence[at] = min(word.confidence for word in found) / 100
    return read


def _clean_cell(cell: Gray) -> Gray:
    """A cell with what is left of its rules whited out: lines of ink along its
    edges only, since a letter's stroke is as tall as a short cell."""
    cleaned = cell.copy()
    ink = cleaned < _INK
    edge = _INSET + 4
    height, width = ink.shape
    for at in [*range(min(edge, height)), *range(max(0, height - edge), height)]:
        if ink[at, :].mean() > 0.5:
            cleaned[at, :] = 255
    for at in [*range(min(edge, width)), *range(max(0, width - edge), width)]:
        if ink[:, at].mean() > 0.5:
            cleaned[:, at] = 255
    return cleaned


# --- a row -----------------------------------------------------------------------------


def _ticked(coverage: Sequence[float]) -> tuple[int | None, float]:
    """The ticked box, and how clearly: ``None`` when none or several are."""
    order = sorted(range(len(coverage)), key=lambda at: coverage[at], reverse=True)
    best = coverage[order[0]]
    if best < _TICKED:
        return None, 0.0
    runner_up = coverage[order[1]] if len(order) > 1 else 0.0
    if runner_up >= _TICKED and runner_up / best >= _CLEAR:
        return None, 0.0
    return order[0], 1.0 - runner_up / best


def _dates_agree(traded: date | None, notified: date | None, latest: date | None) -> bool:
    """A trade on or before its notification, both on or before the filing."""
    if traded is None:
        return False
    last = notified or traded
    return traded <= last and (latest is None or last <= latest)


def _row(
    *,
    name: str,
    name_confidence: float,
    owner: str,
    owner_inked: bool,
    traded: str,
    traded_confidence: float,
    notified: str,
    notified_inked: bool,
    notified_confidence: float,
    kinds: Sequence[TransactionType],
    types: Sequence[float],
    amounts: Sequence[float],
    latest: date | None,
) -> PtrRow:
    problems: list[str] = []
    scores: list[float] = []

    asset_name = " ".join(name.split()).lstrip(_JUNK).rstrip(" |_") or None
    if asset_name is None:
        problems.append("no asset name read")
    scores.append(name_confidence)

    owner_code = parse_owner(owner)
    if (owner.strip() or owner_inked) and owner_code is None:
        problems.append(f"owner read as {owner!r}")

    traded_on = parse_date(traded.replace(" ", ""), latest=latest)
    if traded_on is None:
        problems.append(f"transaction date read as {traded!r}")
    notified_on = parse_date(notified.replace(" ", ""), latest=latest) if notified else None
    if (notified or notified_inked) and notified_on is None:
        problems.append(f"notification date read as {notified!r}")
    # Tesseract scores a whitelisted read 0 as often as not, right or wrong;
    # dates that parse and agree with each other and the filing are the check.
    agree = _dates_agree(traded_on, notified_on, latest)
    scores.append(max(traded_confidence, _AGREEING_DATES) if agree else traded_confidence)
    if notified:
        scores.append(max(notified_confidence, _AGREEING_DATES) if agree else notified_confidence)

    kind_at, kind_clarity = _ticked(types)
    if kind_at is None:
        problems.append("no single type box ticked")
    scores.append(kind_clarity)
    band_at, band_clarity = _ticked(amounts)
    if band_at is None:
        problems.append("no single amount box ticked")
    scores.append(band_clarity)

    confidence = round(min(scores), 3)
    if not problems and confidence < MIN_CONFIDENCE:
        problems.append(f"OCR confidence {confidence:.2f} under {MIN_CONFIDENCE:.2f}")
    band = AMOUNT_BANDS[band_at] if band_at is not None else None
    marks = (
        "type " + "/".join(f"{value:.3f}" for value in types),
        "amount " + "/".join(f"{value:.3f}" for value in amounts),
    )
    return PtrRow(
        line=0,
        page=0,
        status=RowStatus.NEEDS_REVIEW if problems else RowStatus.PARSED,
        owner=owner_code,
        asset_name=asset_name,
        transaction_type=kinds[kind_at] if kind_at is not None else None,
        transaction_date=traded_on,
        notification_date=notified_on,
        amount_low=band[0] if band else None,
        amount_high=band[1] if band else None,
        confidence=confidence,
        problems=tuple(problems),
        raw_text=" | ".join((owner, name, traded, notified, *marks)),
    )
