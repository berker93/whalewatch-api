"""Senate eFD's search pages and reports, read; and a layout change, refused.

The reports in tests/fixtures/senate_efd/ are real eFD pages, as eFD served
them to a public scraper (github.com/Strokeofluck/senate-ptr-scraper) between
2021 and October 2026, named by their report id. The parser was checked
against all 583 that scraper archived — every row read, none to review — and
these are the shapes among them: a plain report, an amendment, an exchange,
an option, a bond, crypto.

This machine cannot reach eFD (it answers non-US addresses with a 403), so the
search pages here are built: the row shape — five strings, the link an ``<a>``
whose text is the title — is the one public scrapers read.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from app.ingestion.congress.ptr import Extraction, RowStatus, TransactionType
from app.ingestion.congress.senate_ptr import (
    ELECTRONIC,
    PAPER,
    SenateLayoutError,
    parse_report,
    parse_search_page,
)

FIXTURES: Final = Path(__file__).parent / "fixtures" / "senate_efd"

BOOZMAN: Final = "7dfdae57-9401-4bf1-9059-85d1e5ee4a84"
"""John Boozman, 21 October 2026: seven partial sales, jointly held."""
HAGERTY_AMENDMENT: Final = "5506edae-7d2f-4c33-a22e-7971200c6f0d"
MORAN_EXCHANGE: Final = "121df9b4-2fea-46d1-8090-10cf701ddb38"
ARMSTRONG: Final = "b999bc0e-3eb0-4ca9-ab07-8e8f2e04b41f"
FETTERMAN_BOND: Final = "4b69867f-0376-4526-93f2-cd556b1155c9"
CRUZ_BITCOIN: Final = "fcb0d9dc-aba4-4fa0-a15b-afed9107a34a"


def _page(report_id: str) -> bytes:
    return (FIXTURES / f"{report_id}.html").read_bytes()


def _read(report_id: str, filed: date, html: bytes | None = None) -> Any:
    return parse_report(html if html is not None else _page(report_id), filing_date=filed)


# --- reports ---------------------------------------------------------------------


def test_a_report_is_read_row_by_row_by_its_own_numbers() -> None:
    report = _read(BOOZMAN, date(2026, 10, 21))

    document = report.document
    assert document.extraction is Extraction.HTML
    assert [row.line for row in document.rows] == [1, 2, 3, 4, 5, 6, 7]
    assert {row.status for row in document.rows} == {RowStatus.PARSED}
    first, last = document.rows[0], document.rows[-1]
    assert (first.ticker, first.asset_name) == ("MU", "Micron Technology, Inc. - Common Stock")
    assert first.owner == "JT"
    assert first.asset_type == "ST"
    assert first.transaction_type is TransactionType.PARTIAL_SALE
    assert first.transaction_date == date(2026, 9, 9)
    assert (first.amount_low, first.amount_high) == (1_001, 15_000)
    assert first.comment is None
    assert first.page is None
    # The page lists them newest first, #7 at the top.
    assert (last.ticker, last.transaction_date) == ("APTV", date(2026, 9, 25))
    assert last.raw_text.startswith("7 | 09/25/2026 | Joint | APTV | Aptiv PLC")
    assert (report.report_date, report.amendment) == (date(2026, 10, 21), None)


def test_an_amendment_says_which_report_it_amends_by_the_originals_date() -> None:
    report = _read(HAGERTY_AMENDMENT, date(2023, 8, 14))

    assert (report.report_date, report.amendment) == (date(2022, 4, 13), 1)
    [row] = report.document.rows
    assert row.owner is None  # Self
    assert row.transaction_type is TransactionType.SALE  # Sale (Full)
    assert row.comment == "Asset held by Roundstone Ventures LLC"


def test_an_exchange_is_the_asset_given_with_the_one_received_described() -> None:
    [row] = _read(MORAN_EXCHANGE, date(2021, 5, 19)).document.rows

    assert row.status is RowStatus.PARSED
    assert (row.ticker, row.asset_name) == ("VZ", "Verizon Communications Inc.")
    assert row.transaction_type is TransactionType.EXCHANGE
    assert row.description == "Received: T-Mobile US, Inc. (TMUS)"
    assert row.comment == "Exchange occurred as a result of the Sprint/T-Mobile merger."


def test_an_options_strike_and_expiry_and_an_exchange_without_tickers() -> None:
    rows = _read(ARMSTRONG, date(2026, 9, 17)).document.rows

    option, exchange = rows[0], rows[1]
    assert option.asset_type == "OP"
    assert option.ticker == "WMB"
    assert option.asset_name == "Williams Companies, Inc. (The) Common Stock"
    assert option.description == "Option Type: Call Strike price: $75.00 Expires: 2026-08-21"
    assert exchange.status is RowStatus.PARSED
    assert exchange.ticker is None
    assert exchange.asset_name == "AvalonBay Communities, Inc. Common Stock (AVB)"
    assert exchange.description is not None
    assert exchange.description.startswith("Received: VMRK - Vivmark Residential")


def test_a_bond_and_a_coin_map_to_the_houses_codes() -> None:
    [bond] = _read(FETTERMAN_BOND, date(2026, 6, 12)).document.rows
    [coin] = _read(CRUZ_BITCOIN, date(2022, 2, 4)).document.rows

    assert (bond.asset_type, bond.owner, bond.ticker) == ("CS", "DC", None)
    assert bond.description == "Rate/Coupon: 5.5% Matures: 2036-04-15"
    assert (coin.asset_type, coin.ticker, coin.asset_name) == ("CT", "BTC", "Bitcoin")
    assert coin.description == "Exchange/Platform: River"


def test_every_fixture_reads_in_full() -> None:
    filed = {
        BOOZMAN: date(2026, 10, 21),
        HAGERTY_AMENDMENT: date(2023, 8, 14),
        MORAN_EXCHANGE: date(2021, 5, 19),
        ARMSTRONG: date(2026, 9, 17),
        FETTERMAN_BOND: date(2026, 6, 12),
        CRUZ_BITCOIN: date(2022, 2, 4),
    }
    assert sorted(path.stem for path in FIXTURES.glob("*.html")) == sorted(filed)
    for report_id, day in filed.items():
        rows = _read(report_id, day).document.rows
        assert rows, report_id
        assert all(row.status is RowStatus.PARSED for row in rows), report_id


# --- a value eFD had not used: one row to review ---------------------------------------


def _replace(report_id: str, old: str, new: str, count: int = 1) -> bytes:
    page = _page(report_id).decode()
    assert old in page, old
    return page.replace(old, new, count).encode()


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("<td>Joint</td>", "<td>Trust</td>", "owner 'Trust'"),
        ("<td>Sale (Partial)</td>", "<td>Gift</td>", "type 'Gift'"),
        ("<td>$1,001 - $15,000</td>", "<td>$1,000 - $15,000</td>", "amount '$1,000 - $15,000'"),
        ("<td>Stock</td>", "<td>Annuity</td>", "asset type 'Annuity'"),
        ("09/25/2026", "13/45/2026", "transaction date '13/45/2026'"),
        ("09/25/2026", "11/25/2026", "after the filing date"),
        ("Aptiv PLC Ordinary Shares", "", "no asset name"),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 30 else None,
)
def test_a_value_not_known_sends_its_row_to_review_and_reads_the_rest(
    old: str, new: str, problem: str
) -> None:
    rows = _read(BOOZMAN, date(2026, 10, 21), _replace(BOOZMAN, old, new)).document.rows

    review = [row for row in rows if row.status is RowStatus.NEEDS_REVIEW]
    assert [row.line for row in review] == [7]
    assert any(problem in reason for reason in review[0].problems), review[0].problems
    assert sum(row.status is RowStatus.PARSED for row in rows) == 6


def test_the_open_top_band_is_one_of_the_houses() -> None:
    html = _replace(BOOZMAN, "<td>$1,001 - $15,000</td>", "<td>Over $50,000,000</td>")
    row = _read(BOOZMAN, date(2026, 10, 21), html).document.rows[-1]

    assert (row.status, row.amount_low, row.amount_high) == (RowStatus.PARSED, 50_000_001, None)


def test_a_figure_where_a_band_should_be_is_kept_as_one() -> None:
    html = _replace(BOOZMAN, "<td>$1,001 - $15,000</td>", "<td>$710.52</td>")
    row = _read(BOOZMAN, date(2026, 10, 21), html).document.rows[-1]

    assert (row.amount_low, row.amount_exact) == (None, Decimal("710.52"))


# --- a layout that moved: an error, never zero rows --------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "count", "says"),
    [
        ('<th scope="col">Comment</th>', '<th scope="col">Notes</th>', 1, "no transaction table"),
        ('<table class="table table-striped">', '<div class="table">', 1, "no transaction table"),
        ("(7 transactions total)", "(seven in total)", 1, "transactions total"),
        ("(7 transactions total)", "(8 transactions total)", 1, "7 transaction rows where"),
        ("<td>--</td>\n", "\n", 1, "of 8 cells"),
        ("<td>7</td>", "<td>8</td>", 1, "not 1 to 7"),
        ("Periodic Transaction Report\n", "Periodic Report\n", 1, "heading"),
        ("<h1", "<h1>Two</h1><h1", 1, "2 h1 headings"),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 25 else None,
)
def test_a_page_not_in_efds_layout_is_refused(old: str, new: str, count: int, says: str) -> None:
    html = _replace(BOOZMAN, old, new, count)

    with pytest.raises(SenateLayoutError, match=says):
        parse_report(html, filing_date=date(2026, 10, 21), key="raw/senate-efd/report/ptr/x.html")


def test_the_refusal_names_where_the_page_is_archived() -> None:
    key = f"raw/senate-efd/report/ptr/{BOOZMAN}.html"

    with pytest.raises(SenateLayoutError) as refused:
        parse_report(
            b"<html><body>Service unavailable</body></html>", filing_date=date.today(), key=key
        )

    assert refused.value.key == key
    assert key in str(refused.value)


def test_a_report_that_counts_no_transactions_and_has_no_table_is_a_placeholder() -> None:
    page = _page(BOOZMAN).decode()
    start, end = page.index("<table"), page.index("</table>") + len("</table>")
    html = (page[:start] + page[end:]).replace("(7 transactions total)", "(0 transactions total)")

    document = _read(BOOZMAN, date(2026, 10, 21), html.encode()).document

    assert document.rows == ()
    assert document.unreadable == "the report lists no transactions"


# --- search pages ------------------------------------------------------------------------


def _link(kind: str, report_id: str, title: str) -> str:
    return f'<a href="/search/view/{kind}/{report_id}/" target="_blank">{title}</a>'


def _search(rows: list[list[Any]], *, total: int | None = None, **extra: Any) -> bytes:
    body = {
        "draw": 1,
        "recordsTotal": 9_999,
        "recordsFiltered": len(rows) if total is None else total,
        "data": rows,
        "result": "ok",
    }
    return json.dumps(body | extra).encode()


def test_a_search_page_lists_reports_online_and_on_paper() -> None:
    page = parse_search_page(
        _search(
            [
                [
                    "John",
                    "Boozman",
                    "Boozman, John (Senator)",
                    _link("ptr", BOOZMAN.upper(), "Periodic Transaction Report for 10/21/2026"),
                    "10/21/2026",
                ],
                [
                    "William F",
                    "Hagerty, IV",
                    "Hagerty, William F (Senator)",
                    _link(
                        "ptr",
                        HAGERTY_AMENDMENT,
                        "Periodic Transaction Report for 04/13/2022 (Amendment 1)",
                    ),
                    "08/14/2023",
                ],
                [
                    "RICHARD",
                    "BLUMENTHAL",
                    "Senator",
                    _link(
                        "paper",
                        "929216d5-5dbd-429c-858c-1e9332924627",
                        "Periodic Transaction Report for 08/31/2026",
                    ),
                    "08/31/2026",
                ],
                [
                    "James M",
                    "Inhofe",
                    "Inhofe, James M (Former Senator)",
                    _link(
                        "ptr",
                        "11111111-2222-3333-4444-555555555555",
                        "Periodic Transaction Report for 01/02/2023 (Amendment)",
                    ),
                    "01/09/2023",
                ],
            ],
            total=240,
        )
    )

    assert page.total == 240
    boozman, hagerty, paper, former = page.rows
    assert boozman.doc_id == BOOZMAN
    assert boozman.link == f"/search/view/ptr/{BOOZMAN}/"
    assert (boozman.kind, boozman.office, boozman.first_name) == (ELECTRONIC, "Senator", "John")
    assert (boozman.filing_date, boozman.report_date, boozman.amendment) == (
        date(2026, 10, 21),
        date(2026, 10, 21),
        None,
    )
    assert (hagerty.last_name, hagerty.amendment) == ("Hagerty, IV", 1)
    assert hagerty.report_date == date(2022, 4, 13)
    assert (paper.kind, paper.office, paper.last_name) == (PAPER, "Senator", "BLUMENTHAL")
    assert (former.office, former.amendment) == ("Former Senator", 1)


def test_an_empty_search_is_an_empty_page() -> None:
    page = parse_search_page(_search([]))

    assert (page.total, page.rows) == (0, ())


@pytest.mark.parametrize(
    ("body", "says"),
    [
        (b"<html>Access Denied</html>", "not JSON"),
        (b"[]", "not a JSON object"),
        (json.dumps({"recordsFiltered": 1}).encode(), "no 'data' list"),
        (json.dumps({"data": []}).encode(), "no 'recordsFiltered'"),
        (_search([["John", "Boozman", "Senator", "10/21/2026"]]), "not five strings"),
        (
            _search([["J", "B", "Senator", "<b>no link</b>", "10/21/2026"]]),
            "not one link",
        ),
        (
            _search([["J", "B", "Senator", _link("annual", BOOZMAN, "Annual Report"), "x"]]),
            "of a kind not known",
        ),
        (
            _search(
                [["J", "B", "Senator", _link("ptr", BOOZMAN, "Annual Report for 2025"), "1/1/26"]]
            ),
            "not a PTR's",
        ),
        (
            _search(
                [
                    [
                        "J",
                        "B",
                        "Senator",
                        _link("ptr", BOOZMAN, "Periodic Transaction Report"),
                        "2026-10-21",
                    ]
                ]
            ),
            "not MM/DD/YYYY",
        ),
        (
            _search(
                [
                    [
                        "J",
                        " ",
                        "Senator",
                        _link("ptr", BOOZMAN, "Periodic Transaction Report"),
                        "10/21/2026",
                    ]
                ]
            ),
            "no last name",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_search_page_not_in_efds_shape_is_refused(body: bytes, says: str) -> None:
    with pytest.raises(SenateLayoutError, match=says):
        parse_search_page(body, key="raw/senate-efd/search/x.json")
