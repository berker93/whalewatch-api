"""The CSV writer every export streams through: what a cell may look like, and
that rows go out a batch at a time."""

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.csv_export import (
    BATCH_ROWS,
    BOM,
    Column,
    cell,
    comment_block,
    csv_response,
    filename,
    rows_to_csv,
)
from app.db.models.position_change import ChangeAction


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (Decimal("65950296923.00"), "65950296923.00"),
        # What str() would write as an exponent, and a thousands separator never.
        (Decimal("1E+3"), "1000"),
        (Decimal("0E-6"), "0.000000"),
        (Decimal("-0.000001"), "-0.000001"),
        (1234567, "1234567"),
        (date(2026, 9, 30), "2026-09-30"),
        (
            datetime(2026, 8, 14, 16, 5, 4, tzinfo=timezone(timedelta(hours=-4))),
            "2026-08-14T20:05:04Z",
        ),
        (ChangeAction.ADD, "add"),
        ("SOCIÉTÉ GÉNÉRALE", "SOCIÉTÉ GÉNÉRALE"),
        (
            {"F2": "range 10.00 to 10.50", "F1": "plan"},
            '{"F1": "plan", "F2": "range 10.00 to 10.50"}',
        ),
    ],
)
def test_a_cell_is_written_plainly(value: object, written: str) -> None:
    assert cell(value) == written


@pytest.mark.parametrize("formula", ['=HYPERLINK("x")', "+1", "-1+2", "@SUM(A1)", "\tx", "\rx"])
def test_text_a_spreadsheet_would_evaluate_is_quoted(formula: str) -> None:
    assert cell(formula) == f"'{formula}"


def test_a_negative_number_is_a_number() -> None:
    assert cell(Decimal("-30230150.0000")) == "-30230150.0000"


def test_a_float_never_reaches_a_file() -> None:
    with pytest.raises(TypeError, match="float"):
        cell(0.1)


def test_the_comment_block_keeps_each_line_one_line() -> None:
    assert comment_block(["Source: EDGAR", "two\nlines"]) == "# Source: EDGAR\r\n# two lines\r\n"


def test_a_filename_is_safe_and_keeps_its_quarters() -> None:
    assert filename("berkshire-hathaway", "2026Q3", "holdings") == (
        "berkshire-hathaway_2026Q3_holdings.csv"
    )
    assert filename("BRK.B", "2025Q4-2026Q3", "holders") == "brk.b_2025Q4-2026Q3_holders.csv"
    assert filename('a"b/c', "", "x") == "a-b-c_export_x.csv"


async def _rows(n: int) -> AsyncIterator[Any]:
    for i in range(n):
        yield SimpleNamespace(id=i, name=f"row {i}")


async def test_rows_go_out_a_batch_at_a_time() -> None:
    chunks = [
        chunk
        async for chunk in rows_to_csv(_rows(2 * BATCH_ROWS + 1), [Column("id"), Column("name")])
    ]
    # Two full batches, then the last row; the header goes out with the first.
    assert len(chunks) == 3
    rows = list(csv.reader(io.StringIO("".join(chunks))))
    assert rows[0] == ["id", "name"]
    assert len(rows) == 2 * BATCH_ROWS + 2
    assert rows[-1] == [str(2 * BATCH_ROWS), f"row {2 * BATCH_ROWS}"]


async def test_a_column_can_compute_its_cell() -> None:
    chunks = [
        chunk async for chunk in rows_to_csv(_rows(1), [Column("label", lambda r: f"#{r.id}")])
    ]
    assert "".join(chunks) == "label\r\n#0\r\n"


class _Cursor:
    def __init__(self, n: int) -> None:
        self.rows = _rows(n)
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Any]:
        return self.rows

    async def close(self) -> None:
        self.closed = True


async def test_the_response_is_bom_comments_header_rows_and_closes_its_cursor() -> None:
    cursor = _Cursor(2)
    response = csv_response("x_2026Q3_y.csv", ["Source: here"], cursor, [Column("id")])  # type: ignore[arg-type]
    assert response.headers["content-disposition"] == 'attachment; filename="x_2026Q3_y.csv"'
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"

    body = "".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    assert body == f"{BOM}# Source: here\r\nid\r\n0\r\n1\r\n"
    assert cursor.closed


def test_utc_is_written_with_a_z() -> None:
    assert cell(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
