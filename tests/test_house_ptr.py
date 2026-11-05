"""House PTR PDFs, read: the text layer, OCR, and the fields both come to.

The fixtures in tests/fixtures/house_ptr/ are real PTRs, as the Clerk serves
them under ``/public_disc/ptr-pdfs/{year}/{doc_id}.pdf``:

=========  ====  =================================================================
doc id     year  why
=========  ====  =================================================================
20034179   2026  a clean e-filed PTR, one page, with asterisks in its accounts' names
20033738   2026  joint (``JT``) trades, in bonds with no ticker
20026590   2025  options (``[OP]``), a partial sale, and a description that carries
                 over a page break
20029134   2025  four pages, thirty rows, details under each page's header
20034556   2026  an amended line, with the ID of the transaction it amends
20012561   2019  the 2015-2019 form: capitals printed lower-case, Marlett boxes
20034999   2026  an exact figure, $2,722.50, where the form wants a band
9116212    2026  a scan of the paper form, upright: two typed lines
9113472    2018  a scan of the older paper form, turned on the page; one line has no
                 type ticked
=========  ====  =================================================================

No PTR in a sample of about three hundred marks a joint account with an
asterisk: joint is the ``JT`` owner code. The asterisks there are, are inside
names (``E*TRADE``) and on the asset-code footnote, which ends the table; the
first fixture has both.

The OCR tests need Tesseract (``brew install tesseract``; the image has it) and
are skipped without it. Their expectations are what a person reads off the
scan, except confidence, which is Tesseract's and is only bounded.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from PIL import Image

from app.ingestion.congress.ptr import (
    AMOUNT_BANDS,
    Extraction,
    PtrPdfError,
    PtrRow,
    RowStatus,
    TransactionType,
    parse_amount,
    parse_date,
    parse_exact_amount,
    parse_transaction_type,
    split_asset,
)
from app.ingestion.congress.ptr_ocr import MIN_CONFIDENCE, OcrWord, Tesseract
from app.ingestion.congress.ptr_pdf import read_ptr

FIXTURES: Final = Path(__file__).parent / "fixtures" / "house_ptr"

TESSERACT: Final = Tesseract.find()
needs_tesseract = pytest.mark.skipif(TESSERACT is None, reason="tesseract is not installed")


def _read(doc_id: str, *, ocr: bool = False, filed: date | None = None) -> tuple[PtrRow, ...]:
    document = read_ptr(
        (FIXTURES / f"{doc_id}.pdf").read_bytes(), ocr=TESSERACT if ocr else None, latest=filed
    )
    assert document.unreadable is None, document.unreadable
    return document.rows


# --- fields --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "band"),
    [
        ("$1,001 - $15,000", (1_001, 15_000)),
        ("$5,000,001 -  $25,000,000", (5_000_001, 25_000_000)),
        ("$15,001 \u2013 $50,000", (15_001, 50_000)),  # an en dash
        ("Over $50,000,000", (50_000_001, None)),
        ("Spouse/DC Over $1,000,000", (1_000_001, None)),
        ("$1,000 - $15,000", None),
        ("$15,001 -", None),
        ("", None),
    ],
)
def test_an_amount_is_one_of_the_forms_bands_or_nothing(
    text: str, band: tuple[int, int | None] | None
) -> None:
    assert parse_amount(text) == band
    if band is not None:
        assert band in AMOUNT_BANDS


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("P", TransactionType.PURCHASE),
        ("s", TransactionType.SALE),
        ("S (partial)", TransactionType.PARTIAL_SALE),
        ("E", TransactionType.EXCHANGE),
        ("S my spouse's", None),
        ("", None),
    ],
)
def test_a_type_is_p_s_s_partial_or_e(text: str, kind: TransactionType | None) -> None:
    assert parse_transaction_type(text) is kind


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("01/14/2025", date(2025, 1, 14)),
        ("07/6/2017", date(2017, 7, 6)),
        ("06/09/26", date(2026, 6, 9)),
        ("01-28-2025", date(2025, 1, 28)),
        ("1/30/2026-", None),
        ("13/01/2025", None),
        ("02/30/2025", None),
        ("01/14/1925", None),
        ("01/14/2205", None),
    ],
)
def test_a_date_is_one_the_form_could_hold(text: str, day: date | None) -> None:
    assert parse_date(text, latest=date(2026, 11, 5)) == day


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        (
            "Alphabet Inc. - Class A Common Stock (GOOGL) [OP]",
            ("Alphabet Inc. - Class A Common Stock", "GOOGL", "OP"),
        ),
        ("Apple Inc. (AAPl) [sT]", ("Apple Inc.", "AAPL", "ST")),
        ("Hill International, Inc. (HIl)", ("Hill International, Inc.", "HIL", None)),
        (
            "Registered Index-Linked Annuity (RILA) [VA]",
            ("Registered Index-Linked Annuity (RILA)", None, "VA"),
        ),
        ("US Treasury Note 01/31/27 [GS]", ("US Treasury Note 01/31/27", None, "GS")),
    ],
)
def test_an_asset_splits_into_name_ticker_and_code(
    text: str, parts: tuple[str | None, str | None, str | None]
) -> None:
    assert split_asset(text) == parts


# --- the text layer ------------------------------------------------------------------


def test_a_clean_e_filed_ptr_reads_in_full_asterisks_and_all() -> None:
    rows = _read("20034179")

    assert [(row.line, row.page, row.status) for row in rows] == [
        (1, 1, RowStatus.PARSED),
        (2, 1, RowStatus.PARSED),
    ]
    first = rows[0]
    assert (first.owner, first.asset_name, first.ticker, first.asset_type) == (
        None,
        "Microsoft Corporation - Common Stock",
        "MSFT",
        "ST",
    )
    assert (first.transaction_type, first.transaction_date, first.notification_date) == (
        TransactionType.PURCHASE,
        date(2026, 3, 12),
        date(2026, 3, 12),
    )
    assert (first.amount_low, first.amount_high, first.cap_gains_over_200) == (1_001, 15_000, False)
    assert first.filing_status == "new"
    # An asterisk inside a name is the name's, not the footnote's.
    assert [row.subholding_of for row in rows] == [
        "Morgan Stanley - E*TRADE - Fields Law Firm 2, LLC",
        "Morgan Stanley - E*TRADE #2",
    ]
    assert first.confidence is None
    assert first.problems == ()


def test_the_document_says_which_filing_it_is() -> None:
    document = read_ptr((FIXTURES / "20034179.pdf").read_bytes(), ocr=None)

    assert (document.extraction, document.pages, document.filing_id) == (
        Extraction.TEXT,
        1,
        "20034179",
    )


def test_a_joint_trade_is_owned_jt() -> None:
    rows = _read("20033738")

    assert [(row.owner, row.asset_name, row.ticker, row.asset_type) for row in rows] == [
        ("JT", "California State Public Works Board Lease Revenue Bonds, Ser. D", None, "GS"),
        ("JT", "Lynwood Unified School District GO Bonds", None, "GS"),
    ]
    assert [(row.amount_low, row.amount_high) for row in rows] == [
        (1_001, 15_000),
        (100_001, 250_000),
    ]
    assert {row.subholding_of for row in rows} == {"Collins/Lofgren Family Trust"}


def test_options_read_with_their_terms_across_a_page_break() -> None:
    rows = _read("20026590")

    assert len(rows) == 9
    assert all(row.status is RowStatus.PARSED for row in rows)
    googl = rows[0]
    assert (googl.owner, googl.ticker, googl.asset_type, googl.transaction_type) == (
        "SP",
        "GOOGL",
        "OP",
        TransactionType.PURCHASE,
    )
    assert googl.description == (
        "Purchased 50 call options with a strike price of $150 and an expiration date of 1/16/26."
    )
    apple = rows[2]
    assert (apple.transaction_type, apple.amount_low, apple.amount_high) == (
        TransactionType.PARTIAL_SALE,
        5_000_001,
        25_000_000,
    )
    assert apple.cap_gains_over_200 is True
    # NVIDIA's options start on page 1; their description is under page 2's header.
    nvidia = rows[5]
    assert (nvidia.page, nvidia.ticker, nvidia.asset_type) == (1, "NVDA", "OP")
    assert nvidia.description == (
        "Purchased 50 call options with a strike price of $80 and an expiration date of 1/16/26."
    )
    assert rows[6].page == 2
    assert rows[6].ticker == "PANW"


def test_a_long_ptr_reads_every_row_on_every_page() -> None:
    rows = _read("20029134")

    assert len(rows) == 30
    assert {row.page for row in rows} == {1, 2, 3, 4}
    assert [row.line for row in rows] == list(range(1, 31))
    assert all(row.status is RowStatus.PARSED for row in rows)
    etf = rows[7]
    assert (etf.asset_name, etf.ticker, etf.asset_type) == (
        "Direxion Daily Small Cap Bull 3X ETF",
        "TNA",
        "OT",
    )
    assert (etf.location, etf.description) == ("US", "ETF")
    assert rows[6].subholding_of == "TradeWinds"


def test_an_amended_line_carries_the_id_it_amends() -> None:
    rows = _read("20034556")

    amended = [row for row in rows if row.filing_status == "amended"]
    assert [(row.ticker, row.amended_id) for row in amended] == [("CVS", "2000152177")]
    assert {row.amended_id for row in rows if row.filing_status == "new"} == {None}


def test_the_2019_forms_mangled_capitals_and_marlett_boxes() -> None:
    rows = _read("20012561")

    assert len(rows) == 11
    assert [row.ticker for row in rows[:4]] == ["AMZN", "AAPL", "CMCSA", "DHR"]
    # The name is kept as printed: only a ticker can be repaired by case.
    assert rows[3].asset_name == "danaher Corporation"
    assert {row.cap_gains_over_200 for row in rows} == {False}
    assert {row.subholding_of for row in rows} == {"Wells Fargo IRA # 1"}


def test_an_exact_figure_instead_of_a_band_is_kept_as_one() -> None:
    [row] = _read("20034999")

    assert row.status is RowStatus.PARSED
    assert (row.amount_low, row.amount_high, row.amount_exact) == (None, None, Decimal("2722.50"))
    assert row.ticker == "ICHR"


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("$710.52", Decimal("710.52")),
        ("$2,722.50", Decimal("2722.50")),
        ("$15", Decimal(15)),
        ("$0.00", None),
        ("$1,001 - $15,000", None),
        ("710.52", None),
    ],
)
def test_an_exact_amount_is_one_dollar_figure(text: str, amount: Decimal | None) -> None:
    assert parse_exact_amount(text) == amount


# --- scans ---------------------------------------------------------------------------


def test_a_scan_without_ocr_is_unreadable_not_an_error() -> None:
    document = read_ptr((FIXTURES / "9116212.pdf").read_bytes(), ocr=None)

    assert (document.extraction, document.pages, document.rows) == (Extraction.NONE, 1, ())
    assert document.unreadable == "a scan with no text layer, and OCR is not installed"


def test_bytes_that_are_not_a_pdf_are_refused() -> None:
    with pytest.raises(PtrPdfError, match="not a PDF"):
        read_ptr(b"<html>Service unavailable</html>", ocr=None)
    with pytest.raises(PtrPdfError, match="cannot be opened"):
        read_ptr(b"%PDF-1.4 and then nothing", ocr=None)


@needs_tesseract
def test_a_scan_of_the_form_reads_its_ticked_boxes() -> None:
    rows = _read("9116212", ocr=True, filed=date(2026, 7, 8))

    assert [
        (
            row.owner,
            row.asset_name,
            row.transaction_type,
            row.transaction_date,
            row.notification_date,
            row.amount_low,
            row.amount_high,
        )
        for row in rows
    ] == [
        (
            "JT",
            "Tractor Supply Co TSCO",
            TransactionType.SALE,
            date(2026, 6, 9),
            date(2026, 7, 1),
            1_001,
            15_000,
        ),
        (
            "JT",
            "Zoetis Inc ZTS",
            TransactionType.PURCHASE,
            date(2026, 6, 9),
            date(2026, 7, 1),
            15_001,
            50_000,
        ),
    ]
    for row in rows:
        assert row.confidence is not None and 0 <= row.confidence <= 1
        assert (row.status is RowStatus.PARSED) == (row.confidence >= MIN_CONFIDENCE)
        assert row.ticker is None and row.asset_type is None


@needs_tesseract
def test_a_scan_turned_on_its_page_is_read_upright_and_a_line_with_no_type_is_kept() -> None:
    rows = _read("9113472", ocr=True, filed=date(2018, 6, 28))

    assert [(row.asset_name, row.transaction_type, row.amount_low) for row in rows] == [
        ("Ford Motor Company", None, 1_001),
        ("Regions Financial Group", TransactionType.SALE, 1_001),
    ]
    ford = rows[0]
    assert ford.status is RowStatus.NEEDS_REVIEW
    assert "no single type box ticked" in ford.problems
    assert (ford.transaction_date, ford.notification_date) == (date(2018, 6, 19), date(2018, 6, 21))
    # The empty lines under them, boxes and all, are not rows.
    assert len(rows) == 2


class _Illegible:
    """An OCR engine that reads every cell as a smudge it is not sure of."""

    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        return [OcrWord("smudge", 0, 0, image.width, image.height, 12.0)]


def test_an_illegible_scan_keeps_every_line_for_review() -> None:
    """Flagged, never dropped: the grid's lines are there, whatever OCR makes of them.

    The form's printed example is one of them here: it is skipped by its words,
    "Example: Mega Corp", and an engine that reads nothing cannot tell it from
    a trade. A person rejects it in the queue.
    """
    document = read_ptr((FIXTURES / "9116212.pdf").read_bytes(), ocr=_Illegible())

    assert document.extraction is Extraction.OCR
    assert len(document.rows) == 3
    assert {row.status for row in document.rows} == {RowStatus.NEEDS_REVIEW}
    for row in document.rows:
        assert row.problems
        assert row.confidence is not None and row.confidence < MIN_CONFIDENCE
    # What the boxes say is still read: the example's S and B, then the two trades'.
    assert [(row.transaction_type, row.amount_low) for row in document.rows] == [
        (TransactionType.SALE, 15_001),
        (TransactionType.SALE, 1_001),
        (TransactionType.PURCHASE, 15_001),
    ]
