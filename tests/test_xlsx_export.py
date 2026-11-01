"""The workbook every Excel export is written as: what each cell is, what each
sheet says, and that nothing is left on disk once the file is open."""

import io
import os
import tempfile
import zipfile
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import openpyxl
import pytest
from fastapi.responses import StreamingResponse
from openpyxl.worksheet.worksheet import Worksheet

from app.api import xlsx_export
from app.api.csv_export import BATCH_ROWS, Column, Kind
from app.api.errors import ApiError
from app.api.methodology import FORM4, thirteen_f
from app.api.xlsx_export import (
    CRIMSON,
    MAX_WIDTH,
    MEDIA_TYPE,
    TEAL,
    WORDMARK,
    Section,
    xlsx_response,
)
from app.db.models.position_change import ChangeAction


class _Cursor:
    """What :func:`xlsx_response` reads: rows, then a close it must make."""

    def __init__(self, rows: Sequence[object]) -> None:
        self.rows = rows
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Any]:
        async def each() -> AsyncIterator[Any]:
            for row in self.rows:
                yield row

        return each()

    async def close(self) -> None:
        self.closed = True


COLUMNS = (
    Column("cusip", kind=Kind.CODE),
    Column("issuer_name"),
    Column("shares", kind=Kind.QUANTITY),
    Column("value_usd", kind=Kind.USD),
    Column("weight_pct", kind=Kind.PCT),
    Column("shares_delta", kind=Kind.QUANTITY, delta=True),
    Column("weight_delta", kind=Kind.POINTS, delta=True),
    Column("implied_price", kind=Kind.PRICE),
    Column("action"),
    Column("period"),
    Column("filed_at"),
    Column("is_split_adjusted"),
    Column("issuer_cik", kind=Kind.CODE),
    Column("footnotes"),
)

ROW = SimpleNamespace(
    cusip="037833100",
    issuer_name='=HYPERLINK("http://evil.example","x")',
    shares=Decimal("1234567.0000"),
    value_usd=Decimal("65950296923.00"),
    weight_pct=Decimal("12.5"),
    shares_delta=Decimal("-250.0000"),
    weight_delta=Decimal("1.25"),
    implied_price=Decimal("0.00900000"),
    action=ChangeAction.TRIM,
    period=date(2026, 9, 30),
    filed_at=datetime(2026, 11, 14, 16, 5, 4, tzinfo=timezone(timedelta(hours=-5))),
    is_split_adjusted=False,
    issuer_cik=320193,
    footnotes={"F1": "range 10.00 to 10.50"},
)

ABOUT: list[tuple[str, str | datetime]] = [
    ("Export", "WhaleWatch export: a test"),
    ("Generated at", datetime(2026, 11, 1, 9, 30, tzinfo=UTC)),
    ("Coverage", "62 of 100 investors have filed for 2026Q3."),
    ("EDGAR", "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000011/"),
]


async def _respond(
    cursor: _Cursor, columns: Sequence[Column] = COLUMNS, methodology: Sequence[Section] = ()
) -> StreamingResponse:
    return await xlsx_response(
        "berkshire_2026Q3_holdings.xlsx",
        cursor,  # type: ignore[arg-type]
        columns,
        title="WhaleWatch export: a test",
        about=ABOUT,
        methodology=methodology,
    )


async def _book(
    rows: Sequence[object],
    columns: Sequence[Column] = COLUMNS,
    methodology: Sequence[Section] = (),
) -> tuple[openpyxl.Workbook, StreamingResponse, _Cursor]:
    cursor = _Cursor(rows)
    response = await _respond(cursor, columns, methodology)
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    assert int(response.headers["content-length"]) == len(body)
    return openpyxl.load_workbook(io.BytesIO(body)), response, cursor


def _rows(sheet: Worksheet) -> list[list[Any]]:
    return [list(row) for row in sheet.iter_rows(values_only=True)]


async def test_the_three_sheets_in_order_and_the_download() -> None:
    book, response, cursor = await _book([ROW])
    assert book.sheetnames == ["Data", "Methodology", "About"]
    assert book.active.title == "Data"
    assert cursor.closed
    assert response.media_type == MEDIA_TYPE
    assert response.headers["content-disposition"] == (
        'attachment; filename="berkshire_2026Q3_holdings.xlsx"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert book.properties.title == "WhaleWatch export: a test"


async def test_every_cell_is_written_as_what_it_is() -> None:
    book, _, _ = await _book([ROW])
    sheet = book["Data"]
    header, _ = _rows(sheet)
    assert header == [c.name for c in COLUMNS]
    cells = dict(zip(header, sheet[2], strict=True))

    # Identifiers are text, leading zero and all, in a text column.
    assert (cells["cusip"].value, cells["cusip"].data_type) == ("037833100", "s")
    assert cells["cusip"].number_format == "@"
    assert (cells["issuer_cik"].value, cells["issuer_cik"].data_type) == ("320193", "s")
    # A name that looks like a formula is the text it is, not a formula, and unquoted.
    assert cells["issuer_name"].data_type == "s"
    assert cells["issuer_name"].value == '=HYPERLINK("http://evil.example","x")'

    # Real numbers, in their formats.
    assert cells["shares"].value == 1234567
    assert cells["shares"].number_format == "#,##0"
    assert cells["value_usd"].value == 65950296923
    assert cells["value_usd"].number_format == "$#,##0"
    # A percentage is the fraction Excel expects.
    assert cells["weight_pct"].value == pytest.approx(0.125)
    assert cells["weight_pct"].number_format == "0.00%"
    # A change is signed; a change in weight stays in points.
    assert cells["shares_delta"].value == -250
    assert cells["shares_delta"].number_format == "+#,##0;-#,##0;0"
    assert cells["weight_delta"].value == pytest.approx(1.25)
    assert cells["weight_delta"].number_format.startswith('+0.00" pp"')
    assert cells["implied_price"].value == pytest.approx(0.009)
    assert cells["implied_price"].number_format == "$#,##0.00##"

    assert cells["action"].value == "trim"
    assert cells["period"].value == datetime(2026, 9, 30)
    assert cells["period"].number_format == "yyyy-mm-dd"
    # In UTC, which Excel cannot hold a zone for.
    assert cells["filed_at"].value == datetime(2026, 11, 14, 21, 5, 4)
    assert cells["is_split_adjusted"].value is False
    assert cells["footnotes"].value == '{"F1": "range 10.00 to 10.50"}'


async def test_the_header_is_frozen_filtered_and_the_columns_fitted() -> None:
    long_name = SimpleNamespace(**{**vars(ROW), "issuer_name": "X" * 200})
    book, _, _ = await _book([ROW, long_name])
    sheet = book["Data"]
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:N3"
    widths = {
        c.name: sheet.column_dimensions[chr(ord("A") + i)].width for i, c in enumerate(COLUMNS)
    }
    # Wide enough for the header and its filter button, and capped for a long name.
    assert widths["cusip"] >= len("037833100") + 1
    assert widths["value_usd"] >= len("$65,950,296,923")
    assert MAX_WIDTH < widths["issuer_name"] < MAX_WIDTH + 2
    assert sheet.column_dimensions["A"].number_format == "@"


async def test_changes_are_coloured_by_sign_in_ledger_teal_and_crimson() -> None:
    book, _, _ = await _book([ROW, ROW, ROW])
    rules = {
        str(ranges.sqref): [(rule.operator, rule.formula, rule.dxf) for rule in rules]
        for ranges, rules in book["Data"].conditional_formatting._cf_rules.items()
    }
    # shares_delta is F, weight_delta G: those, and nothing else.
    assert set(rules) == {"F2:F4", "G2:G4"}
    (gt, positive, teal), (lt, negative, crimson) = rules["F2:F4"]
    assert (gt, positive, lt, negative) == ("greaterThan", ["0"], "lessThan", ["0"])
    assert teal.font.color.rgb.endswith(TEAL["font_color"][1:])
    assert teal.fill.bgColor.rgb.endswith(TEAL["bg_color"][1:])
    assert crimson.font.color.rgb.endswith(CRIMSON["font_color"][1:])


async def test_an_empty_listing_is_a_header_and_no_colours() -> None:
    book, _, _ = await _book([])
    sheet = book["Data"]
    assert _rows(sheet) == [[c.name for c in COLUMNS]]
    assert sheet.auto_filter.ref == "A1:N1"
    assert not list(sheet.conditional_formatting)


async def test_rows_go_to_the_writer_a_batch_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    batches: list[int] = []
    write = xlsx_export._Workbook.write

    def counted(self: Any, rows: Sequence[Sequence[object]]) -> None:
        batches.append(len(rows))
        write(self, rows)

    monkeypatch.setattr(xlsx_export._Workbook, "write", counted)
    book, _, _ = await _book([ROW] * (BATCH_ROWS * 2 + 1))
    assert batches == [BATCH_ROWS, BATCH_ROWS, 1]
    assert book["Data"].max_row == BATCH_ROWS * 2 + 2


async def test_a_float_is_refused_and_nothing_is_left_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    cursor = _Cursor([SimpleNamespace(**{**vars(ROW), "shares": 1.5})])
    with pytest.raises(TypeError, match="float"):
        await _respond(cursor)
    assert cursor.closed
    assert os.listdir(tmp_path) == []


async def test_nothing_is_left_on_disk_once_the_file_is_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # About and Methodology empty: xlsxwriter's own leak, a sheet with no rows.
    response = await xlsx_response(
        "x.xlsx",
        _Cursor([ROW]),  # type: ignore[arg-type]
        COLUMNS,
        title="t",
        about=[],
        methodology=(),
    )
    assert os.listdir(tmp_path) == []
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    assert body.startswith(b"PK")


async def test_more_rows_than_a_worksheet_holds_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xlsx_export, "MAX_ROWS", 2)
    with pytest.raises(ApiError) as refused:
        await _book([ROW] * 3)
    assert refused.value.status_code == 422
    assert "take the CSV" in str(refused.value.detail)


async def test_the_about_sheet_labels_each_fact() -> None:
    book, _, _ = await _book([ROW])
    sheet = book["About"]
    about = {row[0]: row[1] for row in _rows(sheet)}
    assert about["Export"] == "WhaleWatch export: a test"
    assert about["Generated at"] == datetime(2026, 11, 1, 9, 30)
    assert about["Coverage"] == "62 of 100 investors have filed for 2026Q3."
    # Under the wordmark's row and a blank one.
    assert sheet["A3"].value == "Export"
    edgar = sheet["B6"]
    assert edgar.hyperlink.target == ABOUT[3][1]


async def test_the_about_sheet_opens_with_the_wordmark() -> None:
    response = await _respond(_Cursor([ROW]))
    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]
    with zipfile.ZipFile(io.BytesIO(body)) as package:
        names = package.namelist()
        # The one picture in the workbook, as drawn, and on the About sheet.
        assert [n for n in names if n.startswith("xl/media/")] == ["xl/media/image1.png"]
        assert package.read("xl/media/image1.png") == WORDMARK.read_bytes()
        about_rels = package.read("xl/worksheets/_rels/sheet3.xml.rels").decode()
        assert "../drawings/drawing1.xml" in about_rels
        drawing = package.read("xl/drawings/drawing1.xml").decode()
    # Anchored in A1, moved with the cells but not sized by them, and named.
    assert "<xdr:from><xdr:col>0</xdr:col>" in drawing
    assert "<xdr:row>0</xdr:row>" in drawing
    assert 'editAs="oneCell"' in drawing
    assert 'descr="WhaleWatch"' in drawing


async def test_the_methodology_sheet_says_it_in_full() -> None:
    book, _, _ = await _book([ROW], methodology=thirteen_f())
    text = "\n".join(str(v) for row in _rows(book["Methodology"]) for v in row if v)
    for heading in ("Implied price", "Confidence scale", "Split adjustment", "Actions"):
        assert heading in text
    # The thresholds the pipeline runs on, not a paraphrase of them.
    assert "within 2% of it" in text
    assert "3-for-2, 2-for-1, 3-for-1, 4-for-1" in text
    for level in ("high", "medium", "low"):
        assert level in text
    # A long paragraph gets a row tall enough to show it.
    sheet = book["Methodology"]
    tall = [d.height for d in sheet.row_dimensions.values() if d.height]
    assert max(tall) > 15


def test_the_screener_and_the_insider_feed_have_their_own_sections() -> None:
    assert "Screener figures" in [s.heading for s in thirteen_f(flows=True)]
    assert "Screener figures" not in [s.heading for s in thirteen_f()]
    form4 = {s.heading: s for s in FORM4}
    assert "Implied price" not in form4
    codes = dict(e for e in form4["Transaction codes"].entries if isinstance(e, tuple))
    assert codes["P"] == "Open-market or private purchase"
