"""A House PTR's PDF, read the way it can be: its text layer, or OCR, or not at all.

See :mod:`app.ingestion.congress.ptr` for what comes out. This picks the path:

1. **Text** when the pages have text on them (:mod:`~app.ingestion.congress.ptr_text`).
   Every e-filed PTR does, and no paper one does, so the doc id would say the
   same — but the PDF is what is read, so it decides.
2. **OCR** when they have none and there is an engine to read them with
   (:mod:`~app.ingestion.congress.ptr_ocr`).
3. **None** otherwise: the document is :attr:`~app.ingestion.congress.ptr.PtrDocument.unreadable`
   with ``extraction`` ``none``, and a run with OCR installed reads it again.

Pure, given the bytes: no network, no database. Slow on a scan — Tesseract
runs a few times a page — so the caller runs it in a thread.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Final

import pdfplumber
import pypdfium2
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException

from app.ingestion.congress.ptr import Extraction, PtrDocument, PtrPdfError
from app.ingestion.congress.ptr_ocr import Ocr, read_scan
from app.ingestion.congress.ptr_text import read_text_layer

#: A page with this many characters on it has a text layer. E-filed PTRs carry
#: 900 and up on their first page; scans carry none.
_TEXT_LAYER_CHARS: Final = 40


def read_ptr(data: bytes, *, ocr: Ocr | None, latest: date | None = None) -> PtrDocument:
    """``data``'s rows, by text layer or OCR.

    :param ocr: The engine for a scan, or ``None`` when OCR is not installed.
    :param latest: The filing's date: a transaction date after it is a misread.
    :raises PtrPdfError: Not a PDF, or one that cannot be opened.
    :raises OcrError: The OCR engine failed on a page.
    """
    if not data.startswith(b"%PDF"):
        raise PtrPdfError("not a PDF: it does not start with %PDF")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            if pages and any(len(page.chars) >= _TEXT_LAYER_CHARS for page in pdf.pages):
                return read_text_layer(pdf, latest=latest)
    except (PDFSyntaxError, PdfminerException, ValueError, KeyError, TypeError) as failure:
        raise PtrPdfError(f"the PDF cannot be opened: {failure}") from failure
    if not pages:
        return PtrDocument(Extraction.NONE, pages=0, unreadable="the PDF has no pages")
    if ocr is None:
        return PtrDocument(
            Extraction.NONE,
            pages=pages,
            unreadable="a scan with no text layer, and OCR is not installed",
        )
    try:
        scan = pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as failure:
        raise PtrPdfError(f"the PDF cannot be rendered: {failure}") from failure
    try:
        return read_scan(scan, ocr, latest=latest)
    finally:
        scan.close()
