"""``ingest-house-ptrs``: House PTR PDFs into ``congress_transaction``.

Against a real Postgres, because what is worth asserting is what a run leaves
behind — each filing's rows, its placeholder, what the filing says of how it
was read, the run's coverage — and what a second run does not touch. The
fetcher is a fake that serves the real PDFs in tests/fixtures/house_ptr/ by URL
(the readers are tests/test_house_ptr.py's). OCR is off unless a test turns it
on, and those need Tesseract.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Executable, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import AdminUser, CongressFiling, CongressTransaction, IngestionRun
from app.ingestion.congress.house_clerk import ptr_url
from app.ingestion.congress.ptr import PARSER_VERSION
from app.ingestion.remote.fetcher import FetcherUnavailable, FetchRequest, FetchResponse
from app.ingestion.remote.handler import CookieRecord
from tests.conftest import make_settings
from tests.test_house_ptr import FIXTURES, TESSERACT

JOB: Final = "ingest-house-ptrs"

#: An admin of this module's own, committed, so deleted again after each test:
#: the admin tests make theirs under another address.
REVIEWER: Final = "ptr-reviewer@whalewatch.io"

#: doc id → (index year, filing date). Each serves the fixture of its doc id.
FILINGS: Final[dict[str, tuple[int, date]]] = {
    "20034179": (2026, date(2026, 3, 17)),
    "20026590": (2025, date(2025, 1, 17)),
    "9116212": (2026, date(2026, 7, 8)),
    # Missing at first (a 404), then served.
    "20033738": (2026, date(2026, 1, 6)),
    # A 500 at first, then served.
    "20034556": (2026, date(2026, 5, 15)),
}


class FakeClerk:
    """Serves the fixture PDF for each URL in :attr:`answers`, a 404 otherwise."""

    backend = "local"

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, bytes]] = {}
        self.asked: list[str] = []

    def serve(self, doc_id: str, fixture: str | None = None, *, status: int = 200) -> None:
        year = FILINGS[doc_id][0] if doc_id in FILINGS else 2026
        body = (FIXTURES / f"{fixture or doc_id}.pdf").read_bytes() if status == 200 else b"oops"
        self.answers[ptr_url(year, doc_id)] = (status, body)

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        self.asked.append(request.url.rsplit("/", 1)[1].removesuffix(".pdf"))
        status, body = self.answers.get(request.url, (404, b"<html>404</html>"))
        return FetchResponse(status=status, url=request.url, headers=(), body=body)


@pytest.fixture
def clerk() -> FakeClerk:
    clerk = FakeClerk()
    clerk.serve("20034179")
    clerk.serve("20026590")
    clerk.serve("9116212")
    clerk.serve("20034556", status=500)
    return clerk


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def ocr_off(settings: Settings) -> Settings:
    return settings.model_copy(update={"ptr_ocr": "none"})


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    ocr_off: Settings,
    migrated_engine: AsyncEngine,
    clerk: FakeClerk,
) -> Iterator[Settings]:
    @asynccontextmanager
    async def open_fake(_: Settings) -> AsyncIterator[FakeClerk]:
        yield clerk

    monkeypatch.setattr("app.cli.get_settings", lambda: ocr_off)
    monkeypatch.setattr("app.jobs.congress.open_fetcher", open_fake)
    yield ocr_off
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    _seed(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE ingestion_run, congress_transaction, congress_filing, congress_filed_name "
            "RESTART IDENTITY CASCADE"
        ),
        delete(AdminUser).where(AdminUser.email == REVIEWER),
    )


def _seed(engine: AsyncEngine) -> None:
    _execute(
        engine,
        insert(CongressFiling).values(
            [
                {
                    "chamber": "house",
                    "doc_id": doc_id,
                    "report_type": "ptr",
                    "last_name": f"Member {doc_id}",
                    "office": "CA11",
                    "filing_date": filed,
                    "year": year,
                    "source_key": f"raw/house-clerk/index/{year}/{'0' * 64}.zip",
                }
                for doc_id, (year, filed) in FILINGS.items()
            ]
        ),
    )


def _execute(engine: AsyncEngine, *statements: Executable) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(statement)).all()]

    return asyncio.run(run())


def _rows(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    found: dict[str, list[tuple[Any, ...]]] = {}
    for doc_id, *row in _fetch(
        engine,
        select(
            CongressFiling.doc_id,
            CongressTransaction.line,
            CongressTransaction.parse_status,
            CongressTransaction.extraction,
            CongressTransaction.ticker,
        )
        .join(CongressFiling, CongressFiling.id == CongressTransaction.filing_id)
        .order_by(CongressFiling.doc_id, CongressTransaction.line),
    ):
        found.setdefault(doc_id, []).append(tuple(row))
    return found


def _filing(engine: AsyncEngine, doc_id: str) -> dict[str, Any]:
    columns = (
        "document_url",
        "document_key",
        "pages",
        "extraction",
        "parser_version",
        "parse_error",
    )
    [row] = _fetch(
        engine,
        select(*(getattr(CongressFiling, name) for name in columns)).where(
            CongressFiling.doc_id == doc_id
        ),
    )
    return dict(zip(columns, row, strict=True))


def _runs(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            IngestionRun.status,
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.metrics,
            IngestionRun.error,
        )
        .where(IngestionRun.job_name == JOB)
        .order_by(IngestionRun.started_at),
    )


def _archived(settings: Settings) -> list[str]:
    root = Path(settings.raw_store_local_root) / "raw" / "house-clerk" / "ptr"
    return sorted(str(path.relative_to(root)) for path in root.rglob("*.pdf"))


# --- a run -------------------------------------------------------------------------------


def test_a_run_reads_each_pdf_into_rows_and_reports_its_coverage(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk, cli_settings: Settings
) -> None:
    result = runner.invoke(app, [JOB])

    # The Clerk's 500 fails one filing; the others are read.
    assert result.exit_code == 1, result.output
    assert sorted(clerk.asked) == sorted(FILINGS)
    rows = _rows(migrated_engine)
    assert rows["20034179"] == [
        (1, "parsed", "text", "MSFT"),
        (2, "parsed", "text", "MSFT"),
    ]
    assert len(rows["20026590"]) == 9
    assert {status for _, status, _, _ in rows["20026590"]} == {"parsed"}
    # A paper PTR read with OCR off: one placeholder, nothing dropped, nothing invented.
    assert rows["9116212"] == [(1, "placeholder", "none", None)]
    assert "20033738" not in rows and "20034556" not in rows

    assert _filing(migrated_engine, "20034179") == {
        "document_url": ptr_url(2026, "20034179"),
        "document_key": "raw/house-clerk/ptr/2026/20034179.pdf",
        "pages": 1,
        "extraction": "text",
        "parser_version": PARSER_VERSION,
        "parse_error": None,
    }
    assert _filing(migrated_engine, "9116212") | {"document_key": None} == {
        "document_url": ptr_url(2026, "9116212"),
        "document_key": None,
        "pages": 1,
        "extraction": "none",
        "parser_version": PARSER_VERSION,
        "parse_error": "a scan with no text layer, and OCR is not installed",
    }
    # Not up yet: the link and the reason, and read again next time.
    missing = _filing(migrated_engine, "20033738")
    assert (missing["parser_version"], missing["document_url"]) == (
        None,
        ptr_url(2026, "20033738"),
    )
    assert missing["parse_error"] == "the House Clerk has no PDF for 2026/20033738"
    assert _filing(migrated_engine, "20034556")["parser_version"] is None

    assert _archived(cli_settings) == ["2025/20026590.pdf", "2026/20034179.pdf", "2026/9116212.pdf"]

    [(status, seen, written, metrics, error)] = _runs(migrated_engine)
    assert (status, seen, written) == ("partial", 5, 3)
    assert error is not None and "20034556" in error
    assert metrics == {
        "ocr": False,
        "filings_seen": 5,
        "filings_read": 2,
        "review_only": 0,
        "by_text": 2,
        "by_ocr": 0,
        "placeholders": 1,
        "missing": 1,
        "failed": 1,
        "rows_parsed": 11,
        "rows_needs_review": 0,
        "coverage_pct": 40.0,
        "fetched": 3,
        "table_coverage_pct": 50.0,
        # No security to resolve them to: every line is a stock's, unresolved.
        "assets_resolved": 0,
        "assets_unresolved": 11,
        "asset_coverage_pct": 0.0,
        "asset_filing_coverage_pct": 0.0,
    }
    assert (
        "this run   2/5 filings read (40.0%), 0 only in review, 1 placeholders, 1 missing, "
        "1 failed; rows 11 parsed, 0 needs review"
    ) in result.output
    assert "all House  2/4 filings read (50.0%)" in result.output
    assert "9116212    placeholder  a scan with no text layer" in result.output


def test_a_second_run_reads_only_what_is_left(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk
) -> None:
    runner.invoke(app, [JOB])
    before = _fetch(migrated_engine, select(CongressTransaction.id, CongressTransaction.created_at))
    clerk.asked.clear()
    clerk.serve("20033738")
    clerk.serve("20034556")

    result = runner.invoke(app, [JOB])

    assert result.exit_code == 0, result.output
    assert sorted(clerk.asked) == ["20033738", "20034556"]
    after = _fetch(migrated_engine, select(CongressTransaction.id, CongressTransaction.created_at))
    assert set(before) <= set(after)
    rows = _rows(migrated_engine)
    assert len(rows["20033738"]) == 2 and len(rows["20034556"]) == 5
    assert _filing(migrated_engine, "20033738")["parse_error"] is None
    assert "all House  4/5 filings read (80.0%)" in result.output


def test_reparse_reads_the_archive_again_and_fetches_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk
) -> None:
    runner.invoke(app, [JOB])
    clerk.asked.clear()

    result = runner.invoke(app, [JOB, "--reparse", "--year", "2025", "--year", "2026"])

    # The three archived PDFs come from the archive; the two not archived are asked again.
    assert sorted(clerk.asked) == ["20033738", "20034556"]
    assert len(_rows(migrated_engine)["20026590"]) == 9
    assert result.exit_code == 1, result.output


def test_one_filing_by_doc_id(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk
) -> None:
    result = runner.invoke(app, [JOB, "--doc-id", "20026590"])

    assert result.exit_code == 0, result.output
    assert clerk.asked == ["20026590"]
    assert list(_rows(migrated_engine)) == ["20026590"]


def test_a_reviewed_filing_is_never_read_again(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk
) -> None:
    runner.invoke(app, [JOB, "--doc-id", "20034179"])
    _execute(
        migrated_engine,
        insert(AdminUser).values(email=REVIEWER, name="Grace Hopper", password_hash="x"),
        update(CongressTransaction)
        .where(CongressTransaction.line == 1)
        .values(
            reviewed_at=func.now(),
            reviewed_by=select(AdminUser.id).where(AdminUser.email == REVIEWER).scalar_subquery(),
            review_note="checked against the original",
        ),
    )
    clerk.asked.clear()

    runner.invoke(app, [JOB, "--reparse", "--doc-id", "20034179"])

    assert clerk.asked == []
    [(note,)] = _fetch(
        migrated_engine,
        select(CongressTransaction.review_note).where(CongressTransaction.line == 1),
    )
    assert note == "checked against the original"


def test_a_pdf_of_another_filing_gets_a_placeholder(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk
) -> None:
    clerk.serve("20033738", "20034179")

    runner.invoke(app, [JOB, "--doc-id", "20033738"])

    assert _rows(migrated_engine)["20033738"] == [(1, "placeholder", "text", None)]
    assert _filing(migrated_engine, "20033738")["parse_error"] == (
        "the PDF is Filing ID #20034179, not 20033738"
    )


def test_an_error_page_served_as_a_pdf_is_a_failure_not_an_archive(
    runner: CliRunner, migrated_engine: AsyncEngine, clerk: FakeClerk, cli_settings: Settings
) -> None:
    clerk.answers[ptr_url(2026, "20033738")] = (200, b"<html>maintenance</html>")

    result = runner.invoke(app, [JOB, "--doc-id", "20033738"])

    assert result.exit_code == 1
    assert "20033738   failed       2026/20033738.pdf is not a PDF" in result.output
    assert _archived(cli_settings) == []


def test_no_fetcher_stops_the_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, migrated_engine: AsyncEngine
) -> None:
    @asynccontextmanager
    async def unavailable(_: Settings) -> AsyncIterator[None]:
        raise FetcherUnavailable("US_FETCHER_BACKEND is none")
        yield

    monkeypatch.setattr("app.jobs.congress.open_fetcher", unavailable)

    result = runner.invoke(app, [JOB])

    assert result.exit_code == 1
    assert "US_FETCHER_BACKEND is none" in result.output
    assert _rows(migrated_engine) == {}


@pytest.mark.skipif(TESSERACT is None, reason="tesseract is not installed")
def test_a_scan_read_without_ocr_is_read_again_once_ocr_is_there(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: AsyncEngine,
    clerk: FakeClerk,
    settings: Settings,
) -> None:
    runner.invoke(app, [JOB, "--doc-id", "9116212"])
    assert _rows(migrated_engine)["9116212"] == [(1, "placeholder", "none", None)]
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)  # PTR_OCR=tesseract
    clerk.asked.clear()

    result = runner.invoke(app, [JOB])

    # Everything but the scan, which is archived: read from there, by OCR now.
    assert sorted(clerk.asked) == sorted(set(FILINGS) - {"9116212"})
    rows = _rows(migrated_engine)["9116212"]
    assert [(line, extraction) for line, _, extraction, _ in rows] == [(1, "ocr"), (2, "ocr")]
    assert {status for _, status, _, _ in rows} <= {"parsed", "needs_review"}
    assert _filing(migrated_engine, "9116212")["extraction"] == "ocr"
    assert "OCR on" in result.output
