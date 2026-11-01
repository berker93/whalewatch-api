"""CSV exports: one way to write a file, for every table that can be downloaded.

Every export surface (:mod:`app.api.routers.exports`) runs the listing's own
query with no page (:func:`app.api.pagination.page_statement`), opens it as a
server-side cursor (:func:`stream_rows`), and hands the cursor and its columns
to :func:`rows_to_csv` through :func:`csv_response`. So a file has exactly the
rows a walk of every page would, in the same order, and none of them is held:
asyncpg fetches :data:`BATCH_ROWS` at a time, and each batch is written out
before the next is read. A 3,000-position filing is never a Python list.

The file
--------
- UTF-8 with a byte-order mark, so Excel reads ``SOCIÉTÉ GÉNÉRALE`` as that
  and not as mojibake. Every other reader skips it.
- A block of ``#`` lines first: where the data is from, when the file was
  made, the disclaimer, and a link back to the page it was taken from.
  ``pandas.read_csv(path, comment="#")`` reads past it; Excel shows it above
  the table.
- One row of column names, then the rows, ``\\r\\n`` between them (RFC 4180).
- Numbers as plain digits: a ``Decimal`` as ``format(d, "f")``, so never
  ``1E+3``, never ``1,000``, never a float's ``0.30000000000000004``. A float
  reaching a cell is a bug, and raises.
- Nothing as an empty cell; ``true``/``false`` for a flag; dates and times in
  ISO 8601, times in UTC with a ``Z``.
- A text cell starting with ``=``, ``+``, ``-``, ``@``, a tab or a carriage
  return is prefixed with ``'``, so a spreadsheet shows it rather than
  evaluating it. Issuer names, insider names and footnotes are as filed, and
  anyone can file. Numbers are not text and are never prefixed.

Provenance
----------
Every export carries ``as_of_period``, ``filed_at``, ``confidence`` and
``observation_count`` as its last columns, and ``is_split_adjusted`` where a
row's change can be across a split: the same columns in every file, so a
spreadsheet built on one export's provenance works on the next. What each
means for its surface is in the route's description.
"""

import csv
import io
import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy import Row, Select
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from app.api.errors import Responses

#: How many rows asyncpg fetches from the cursor at once, and how many are
#: written into one chunk of the response.
BATCH_ROWS: Final = 500

#: What makes Excel read the file as UTF-8.
BOM: Final = "﻿"

MEDIA_TYPE: Final = "text/csv; charset=utf-8"

#: What a spreadsheet reads as the start of a formula.
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")

#: What a filename keeps: everything else is a hyphen.
_UNSAFE_IN_FILENAME: Final = re.compile(r"[^a-z0-9.-]+")

#: Every export's 200, in the OpenAPI document: a file, not JSON.
CSV_FILE: Final[Responses] = {
    status.HTTP_200_OK: {
        "description": (
            "The file: UTF-8 with a BOM, a block of `#` lines, a header row, then "
            "every row. `Content-Disposition: attachment` names it."
        ),
        "content": {"text/csv": {"schema": {"type": "string"}}},
    }
}


class Kind(Enum):
    """What a column's numbers are, for a spreadsheet to show them as.

    A CSV cell is the same digits whatever the kind: only the ``.xlsx``
    (:mod:`app.api.xlsx_export`) formats by it. A kind says nothing about a
    text, date or flag cell, which is written as what it is.
    """

    TEXT = "text"
    """Words, or a number with no unit."""
    CODE = "code"
    """An identifier: a CUSIP, a ticker, a CIK, an accession number. Text,
    whatever it looks like, so ``037833100`` keeps its leading zero."""
    USD = "usd"
    """Dollars, shown to the dollar."""
    USD_CENTS = "usd_cents"
    """Dollars, shown to the cent: a trade's value."""
    PRICE = "price"
    """Dollars a share, to between two and four places."""
    QUANTITY = "quantity"
    """Shares, or a count of something."""
    PCT = "pct"
    """A percentage, as the API writes it: 12.5 is 12.5%."""
    POINTS = "points"
    """A difference between two percentages, in percentage points."""
    RATIO = "ratio"
    """A plain multiple: a split's 10 for 10-for-1."""


@dataclass(frozen=True, slots=True)
class Column:
    """One column of an export: its name in the header row, and its cell in each row.

    ``value`` reads the cell's value off a row; left out, it is the row's
    attribute of the column's name. ``kind`` and ``delta`` are for a
    spreadsheet: what the numbers are, and whether the column is a change, which
    is shown in the colour of its sign.
    """

    name: str
    value: Callable[[Row[Any]], object] | None = None
    kind: Kind = Kind.TEXT
    delta: bool = False

    def of(self, row: Row[Any]) -> object:
        return getattr(row, self.name) if self.value is None else self.value(row)


def cell(value: object) -> str:
    """``value`` as one CSV cell, by the rules in the module docstring.

    :raises TypeError: for a float, which is never exact enough to export, and
        for anything with no written form here.
    """
    match value:
        case None:
            return ""
        case bool():
            return "true" if value else "false"
        case Decimal():
            return format(value, "f")
        case int():
            return str(value)
        case float():
            raise TypeError(f"a float reached a CSV cell: {value!r}")
        case datetime():
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        case date():
            return value.isoformat()
        case Enum():
            return cell(value.value)
        case str():
            return f"'{value}" if value.startswith(_FORMULA_START) else value
        case dict() | list():
            return cell(json.dumps(value, sort_keys=True, ensure_ascii=False))
        case _:
            raise TypeError(f"no CSV form for {type(value).__name__}: {value!r}")


async def rows_to_csv(
    cursor: AsyncIterable[Row[Any]], columns: Sequence[Column]
) -> AsyncIterator[str]:
    """The header row and then every row of ``cursor``, as CSV text, a batch at a time.

    Never more than :data:`BATCH_ROWS` rows in hand: each batch is written,
    yielded and dropped before the next is read.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def flush() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    writer.writerow([column.name for column in columns])
    pending = 0
    async for row in cursor:
        writer.writerow([cell(column.of(row)) for column in columns])
        pending += 1
        if pending == BATCH_ROWS:
            yield flush()
            pending = 0
    yield flush()


async def stream_rows(session: AsyncSession, statement: Select[Any]) -> AsyncResult[Any]:
    """``statement`` opened as a server-side cursor, fetched :data:`BATCH_ROWS` at a time.

    Executed here, before the response starts: a query that fails is a 500,
    not a file cut off after its header.
    """
    return await session.stream(statement.execution_options(yield_per=BATCH_ROWS))


def comment_block(lines: Iterable[str]) -> str:
    """``lines`` as the file's ``#`` lines. A line break inside one would start
    a row of data, so it is a space."""
    return "".join(f"# {' '.join(line.splitlines())}\r\n" for line in lines)


def filename(*parts: str, extension: str = "csv") -> str:
    """``berkshire-hathaway_2026Q3_holdings.csv``: the parts, lowercased but for
    the quarter labels, anything but letters, digits, dots and hyphens a
    hyphen, joined by underscores."""
    cleaned = []
    for part in parts:
        lowered = re.sub(r"(\d{4})q([1-4])", r"\1Q\2", _UNSAFE_IN_FILENAME.sub("-", part.lower()))
        cleaned.append(lowered.strip("-") or "export")
    return f"{'_'.join(cleaned)}.{extension}"


def csv_response(
    name: str, header: Iterable[str], cursor: AsyncResult[Any], columns: Sequence[Column]
) -> StreamingResponse:
    """The download: the BOM and ``header`` as ``#`` lines, then :func:`rows_to_csv`.

    The cursor is closed when the last row is out, and when the client goes
    away before it. ``no-store``: an export is one person's, on their plan,
    and taken once.
    """
    preamble = BOM + comment_block(header)

    async def body() -> AsyncIterator[str]:
        try:
            yield preamble
            async for chunk in rows_to_csv(cursor, columns):
                yield chunk
        finally:
            await cursor.close()

    return StreamingResponse(
        body(),
        media_type=MEDIA_TYPE,
        headers={
            "content-disposition": f'attachment; filename="{name}"',
            "cache-control": "no-store",
        },
    )
