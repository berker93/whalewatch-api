"""Excel exports: the CSV's rows as a workbook, with the caveats on board.

Every ``.xlsx`` route (:mod:`app.api.routers.exports`) runs the query its
``.csv`` sibling runs and hands the same cursor and columns to
:func:`xlsx_response`. So the rows are the CSV's, in its order. What the
workbook adds is what a CSV cannot carry: real numbers in real formats, and
two sheets that say what the numbers are. An export is how the numbers reach
someone who never saw the page, and the workbook is the one chance to send
the caveats with them.

The sheets
----------
``Data``
    The header row, frozen and filtered, then every row. Columns are as wide
    as their longest cell, up to :data:`MAX_WIDTH`. Each column's
    :class:`~app.api.csv_export.Kind` picks its number format, and every cell
    is written as what it is: a number as a number, a date as a date, a flag
    as ``TRUE``/``FALSE``, and an identifier as text, so a CUSIP keeps its
    leading zero (the CSV's one problem Excel cannot be talked out of). A
    percentage is the fraction Excel expects, 0.125 shown as 12.50%, so
    ``SUM`` and charts work on it; a change in weight is percentage points,
    shown with ``pp``. A column that is a change is coloured by its sign in
    Ledger's teal and crimson, the web's buys and sells, not red and green.
``Methodology``
    How the numbers are made, in full, as :class:`Section`\\ s the route
    passes in (:mod:`app.api.methodology`).
``About``
    What the file is: when it was made, of which period, how much of the
    universe had filed, where the data is from, and the disclaimer.

Written to disk, then sent
--------------------------
An ``.xlsx`` is a zip, whose directory comes last, so no byte of it can be
sent before the last row is in. xlsxwriter's ``constant_memory`` mode writes
each row to a temporary file as it is given and keeps none, so the workbook
holds a row at a time as the CSV does. Each workbook gets a directory of its
own for those files and for itself, because xlsxwriter leaves a sheet's file
behind when the sheet has no rows, and every sheet's when the build fails.
Once finished, the workbook is opened and the directory removed, and the
file is streamed from the open handle: nothing stays on disk however the
build or the download ends. Being built before the response starts is also what makes
a failure a clean one: a query error is a 500, and a build that outruns the
request timeout is its 504, never a file cut off.

xlsxwriter is synchronous, so each batch of
:data:`~app.api.csv_export.BATCH_ROWS` rows is written in a worker thread,
and a 15,000-row workbook does not hold the event loop.

Text is never a formula
-----------------------
Every string is written with ``write_string``, which stores it as text. A
name starting with ``=`` is shown as that and never evaluated, so the ``'``
the CSV prefixes such a cell with is not needed here, and not added.
"""

import asyncio
import json
import math
import os
import shutil
import tempfile
from collections.abc import AsyncIterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import IO, Any, Final

import xlsxwriter
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncResult
from xlsxwriter.worksheet import Worksheet

from app.api.csv_export import BATCH_ROWS, Column, Kind
from app.api.errors import ApiError, Responses
from app.api.schemas.error import ErrorCode

MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Every Excel export's 200, in the OpenAPI document.
XLSX_FILE: Final[Responses] = {
    status.HTTP_200_OK: {
        "description": (
            "The workbook: a `Data` sheet with the rows, `Methodology` and `About`. "
            "`Content-Disposition: attachment` names it."
        ),
        "content": {MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    }
}

#: Rows a worksheet holds below its header row.
MAX_ROWS: Final = 1_048_575

#: The widest a data column is fitted to, in characters: a long issuer name
#: or footnote is cut off at the next cell, not given the screen.
MAX_WIDTH: Final = 60

#: What the filter's drop-down button takes from a header cell, in characters.
_FILTER_BUTTON: Final = 3

#: How much of the file is read into one chunk of the response.
CHUNK_BYTES: Final = 64 * 1024

# Ledger's colours (whalewatch-web, src/styles/index.css). Ledger is dark only,
# and its accumulate #3FBBA2 and distribute #E8657F are 2.4:1 and 3.2:1 on a
# white sheet. So a change is the hue at 16% on white behind it, and the hue
# darkened until it is 4.5:1 on that as the figure: the same teal and crimson,
# readable printed.
TEAL: Final = {"font_color": "#287868", "bg_color": "#E0F4F0"}
CRIMSON: Final = {"font_color": "#A94A5D", "bg_color": "#FBE6EB"}
#: The header row: Ledger's ink on its surface.
HEADER: Final = {"bold": True, "font_color": "#E9E9ED", "bg_color": "#232532"}

#: Each kind's number format: as it is, and with its sign, for a change.
NUMBER_FORMATS: Final[dict[Kind, tuple[str, str]]] = {
    Kind.TEXT: ("General", "General"),
    Kind.CODE: ("@", "@"),
    Kind.USD: ("$#,##0", "+$#,##0;-$#,##0;$0"),
    Kind.USD_CENTS: ("$#,##0.00", "+$#,##0.00;-$#,##0.00;$0.00"),
    Kind.PRICE: ("$#,##0.00##", "+$#,##0.00##;-$#,##0.00##;$0.00"),
    Kind.QUANTITY: ("#,##0", "+#,##0;-#,##0;0"),
    Kind.PCT: ("0.00%", "+0.00%;-0.00%;0.00%"),
    Kind.POINTS: ('0.00" pp"', '+0.00" pp";-0.00" pp";0.00" pp"'),
    Kind.RATIO: ("General", "General"),
}
DATE_FORMAT: Final = "yyyy-mm-dd"
#: Times are UTC, which the column can't say and the Methodology sheet does.
DATETIME_FORMAT: Final = "yyyy-mm-dd hh:mm:ss"

#: Characters a line of the Methodology and About sheets' wide column holds,
#: for the row heights wrapped text needs: Excel does not fit a row to it.
_LINE_CHARS: Final = 95
_LINE_POINTS: Final = 15

Entry = str | tuple[str, str]
"""A Methodology paragraph, or a term and what it means."""


@dataclass(frozen=True, slots=True)
class Section:
    """One heading of the Methodology sheet and what is under it."""

    heading: str
    entries: tuple[Entry, ...]


About = Sequence[tuple[str, str | datetime]]
"""The About sheet's rows: a label, and a text, a link or a time."""


class _Workbook:
    """The workbook while it is written. Synchronous: each call is one worker thread's."""

    def __init__(
        self,
        path: str,
        directory: str,
        columns: Sequence[Column],
        *,
        title: str,
        about: About,
        methodology: Sequence[Section],
    ) -> None:
        self.columns = columns
        self.book = xlsxwriter.Workbook(
            path,
            {
                "constant_memory": True,
                "tmpdir": directory,
                # Belt and braces: every cell is written with its own method.
                "strings_to_formulas": False,
                "strings_to_numbers": False,
                "strings_to_urls": False,
            },
        )
        self.book.set_properties({"title": title, "author": "WhaleWatch", "company": "WhaleWatch"})
        self.data = self.book.add_worksheet("Data")
        methodology_sheet = self.book.add_worksheet("Methodology")
        about_sheet = self.book.add_worksheet("About")

        add = self.book.add_format
        self.text = add({"num_format": "@"})
        self.date = add({"num_format": DATE_FORMAT})
        self.datetime = add({"num_format": DATETIME_FORMAT})
        self.numbers = [
            add({"num_format": NUMBER_FORMATS[c.kind][1 if c.delta else 0]}) for c in columns
        ]
        self.widths = [len(c.name) + _FILTER_BUTTON for c in columns]
        self.row = 0

        header = add(HEADER)
        for index, column in enumerate(columns):
            self.data.write_string(0, index, column.name, header)

        _write_methodology(self.book, methodology_sheet, methodology)
        _write_about(self.book, about_sheet, about)

    def write(self, rows: Sequence[Sequence[object]]) -> None:
        if self.row + len(rows) > MAX_ROWS:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ErrorCode.VALIDATION_ERROR,
                f"More rows than a worksheet holds ({MAX_ROWS:,}). Narrow the request, "
                "or take the CSV.",
            )
        for values in rows:
            self.row += 1
            for index, value in enumerate(values):
                self._cell(index, value)

    def _cell(self, index: int, value: object) -> None:
        """One cell of the current row, written as what it is, and its width noted."""
        sheet, row, column = self.data, self.row, self.columns[index]
        match value:
            case None:
                return
            case bool():
                sheet.write_boolean(row, index, value)
                shown = 5
            case Decimal() | int() if column.kind is Kind.CODE:
                shown = self._text(index, str(value))
            case Decimal() | int():
                number = value / 100 if column.kind is Kind.PCT else value
                sheet.write_number(row, index, float(number), self.numbers[index])
                shown = _shown(value, column.kind)
            case float():
                raise TypeError(f"a float reached a workbook cell: {value!r}")
            case datetime():
                sheet.write_datetime(
                    row, index, value.astimezone(UTC).replace(tzinfo=None), self.datetime
                )
                shown = len(DATETIME_FORMAT)
            case date():
                sheet.write_datetime(row, index, value, self.date)
                shown = len(DATE_FORMAT)
            case Enum():
                return self._cell(index, value.value)
            case str():
                shown = self._text(index, value)
            case dict() | list():
                shown = self._text(index, json.dumps(value, sort_keys=True, ensure_ascii=False))
            case _:
                raise TypeError(f"no workbook form for {type(value).__name__}: {value!r}")
        self.widths[index] = max(self.widths[index], shown)

    def _text(self, index: int, value: str) -> int:
        code = self.columns[index].kind is Kind.CODE
        self.data.write_string(self.row, index, value, self.text if code else None)
        return len(value)

    def close(self) -> None:
        """Freeze, filter, fit and colour the data sheet, then write the zip."""
        sheet = self.data
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, self.row, len(self.columns) - 1)
        for index, column in enumerate(self.columns):
            width = min(self.widths[index], MAX_WIDTH) + 1
            # A column of identifiers is text throughout, so a value typed or
            # pasted into it later keeps its leading zeros too.
            sheet.set_column(index, index, width, self.text if column.kind is Kind.CODE else None)
            if column.delta and self.row:
                for criteria, colour in ((">", TEAL), ("<", CRIMSON)):
                    sheet.conditional_format(
                        1,
                        index,
                        self.row,
                        index,
                        {
                            "type": "cell",
                            "criteria": criteria,
                            "value": 0,
                            "format": self.book.add_format(colour),
                        },
                    )
        sheet.activate()
        self.book.close()


def _shown(value: Decimal | int, kind: Kind) -> int:
    """About how many characters ``value`` takes in ``kind``'s format."""
    magnitude = abs(value)
    match kind:
        case Kind.USD:
            return len(f"{magnitude:,.0f}") + 2
        case Kind.USD_CENTS | Kind.PRICE:
            return len(f"{magnitude:,.2f}") + 2
        case Kind.QUANTITY:
            return len(f"{magnitude:,.0f}") + 1
        case Kind.PCT:
            return len(f"{magnitude:.2f}") + 2
        case Kind.POINTS:
            return len(f"{magnitude:.2f}") + 4
        case _:
            return len(str(value))


def _height(*texts: str) -> int:
    """The points a row of ``texts`` wrapped in the wide column needs."""
    lines = max(
        sum(max(1, math.ceil(len(line) / _LINE_CHARS)) for line in text.split("\n"))
        for text in texts
    )
    return lines * _LINE_POINTS


def _write_methodology(
    book: xlsxwriter.Workbook, sheet: Worksheet, sections: Sequence[Section]
) -> None:
    """Headings and terms in a narrow first column, paragraphs wrapped in a wide second."""
    title = book.add_format({"bold": True, "font_size": 14})
    heading = book.add_format({"bold": True, "font_size": 12})
    term = book.add_format({"bold": True, "valign": "top"})
    prose = book.add_format({"text_wrap": True, "valign": "top"})
    sheet.set_column(0, 0, 18)
    sheet.set_column(1, 1, _LINE_CHARS + 5)

    sheet.write_string(0, 0, "Methodology", title)
    row = 2
    for section in sections:
        sheet.write_string(row, 0, section.heading, heading)
        row += 1
        for entry in section.entries:
            name, text = entry if isinstance(entry, tuple) else ("", entry)
            sheet.set_row(row, _height(text))
            if name:
                sheet.write_string(row, 0, name, term)
            sheet.write_string(row, 1, text, prose)
            row += 1
        row += 1


def _write_about(book: xlsxwriter.Workbook, sheet: Worksheet, rows: About) -> None:
    label = book.add_format({"bold": True, "valign": "top"})
    prose = book.add_format({"text_wrap": True, "valign": "top"})
    link = book.add_format({"font_color": "#5D5294", "underline": 1, "valign": "top"})
    when = book.add_format({"num_format": f'{DATETIME_FORMAT} "UTC"', "align": "left"})
    sheet.set_column(0, 0, 18)
    sheet.set_column(1, 1, _LINE_CHARS + 5)

    for row, (name, value) in enumerate(rows):
        sheet.write_string(row, 0, name, label)
        if isinstance(value, datetime):
            sheet.write_datetime(row, 1, value.astimezone(UTC).replace(tzinfo=None), when)
        elif value.startswith(("https://", "http://")):
            sheet.write_url(row, 1, value, link, string=value)
        else:
            sheet.set_row(row, _height(value))
            sheet.write_string(row, 1, value, prose)


async def _write(
    directory: str,
    cursor: AsyncIterable[Row[Any]],
    columns: Sequence[Column],
    *,
    title: str,
    about: About,
    methodology: Sequence[Section],
) -> str:
    """The workbook, in ``directory``: every row of ``cursor``, a batch per
    worker thread. Returns its path."""
    path = os.path.join(directory, "export.xlsx")
    book = await asyncio.to_thread(
        _Workbook, path, directory, columns, title=title, about=about, methodology=methodology
    )
    batch: list[list[object]] = []
    async for row in cursor:
        batch.append([column.of(row) for column in columns])
        if len(batch) == BATCH_ROWS:
            await asyncio.to_thread(book.write, batch)
            batch = []
    await asyncio.to_thread(book.write, batch)
    await asyncio.to_thread(book.close)
    return path


def _chunks(handle: IO[bytes]) -> Iterator[bytes]:
    """The file, :data:`CHUNK_BYTES` at a time. Synchronous: Starlette reads it
    in a worker thread."""
    with handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


async def xlsx_response(
    name: str,
    cursor: AsyncResult[Any],
    columns: Sequence[Column],
    *,
    title: str,
    about: About,
    methodology: Sequence[Section],
) -> StreamingResponse:
    """The download: the workbook, written to a directory of its own and streamed from it.

    The cursor is closed once the rows are in, and the directory removed once
    the workbook is open, whatever happens in between. ``no-store``, as for
    the CSV.
    """
    directory = await asyncio.to_thread(tempfile.mkdtemp, prefix="whalewatch-xlsx-")
    try:
        try:
            path = await _write(
                directory, cursor, columns, title=title, about=about, methodology=methodology
            )
        finally:
            await cursor.close()
        handle = await asyncio.to_thread(open, path, "rb")
        size = os.fstat(handle.fileno()).st_size
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

    return StreamingResponse(
        _chunks(handle),
        media_type=MEDIA_TYPE,
        headers={
            "content-disposition": f'attachment; filename="{name}"',
            "content-length": str(size),
            "cache-control": "no-store",
        },
    )
