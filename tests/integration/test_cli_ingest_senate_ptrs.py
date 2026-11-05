"""``ingest-senate-ptrs``: Senate eFD's PTRs into ``congress_filing`` and ``congress_transaction``.

Against a real Postgres, because what is worth asserting is what a run leaves
behind: the filings and their rows, the paper placeholder, the watermark, the
run's row and what a second run does not touch. eFD is a fake behind the
fetcher interface that keeps eFD's rules — the terms before anything, a
``csrftoken`` cookie, a session cookie — and serves the real report pages in
tests/fixtures/senate_efd/ (the parser's own tests are tests/test_senate_ptr.py).
"""

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import urlsplit

import pytest
from sqlalchemy import Executable, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import CongressFiling, CongressTransaction, CrawlWatermark, IngestionRun
from app.ingestion.congress.senate_ptr import SENATE_PARSER_VERSION
from app.ingestion.congress.senate_ptrs import PAPER_REASON
from app.ingestion.remote.fetcher import FetcherUnavailable, FetchRequest, FetchResponse
from app.ingestion.remote.handler import CookieRecord
from tests.conftest import make_settings
from tests.test_senate_ptr import BOOZMAN, FIXTURES, HAGERTY_AMENDMENT

JOB: Final = "ingest-senate-ptrs"
EFD: Final = "https://efdsearch.senate.gov"
TODAY: Final = date(2026, 11, 5)

PAPER_ID: Final = "929216d5-5dbd-429c-858c-1e9332924627"
#: A report outside the first test's window.
EARLY_ID: Final = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"

_AGREEMENT: Final = (
    '<form method="POST"><input type="hidden" name="csrfmiddlewaretoken" value="tok">'
    '<input type="checkbox" name="prohibition_agreement" value="1"></form>'
)


def _cookie(name: str, value: str) -> CookieRecord:
    return CookieRecord(
        name=name,
        value=value,
        domain="efdsearch.senate.gov",
        path="/",
        secure=True,
        expires=None,
        domain_specified=False,
        path_specified=True,
        discard=True,
        http_only=False,
    )


def _row(kind: str, report_id: str, last: str, filed: date, title: str) -> list[str]:
    link = f'<a href="/search/view/{kind}/{report_id}/" target="_blank">{title}</a>'
    office = "Senator" if kind == "paper" else f"{last}, Jane (Senator)"
    return ["Jane", last, office, link, filed.strftime("%m/%d/%Y")]


@dataclass
class FakeEfd:
    """eFD's rules, and its answers, in process."""

    backend: str = "local"
    rows: list[list[str]] = field(default_factory=list)
    pages: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    """The report ids fetched, in order."""
    searched: list[dict[str, str]] = field(default_factory=list)

    def report(self, report_id: str, body: bytes | None = None, *, status: int = 200) -> None:
        page = body if body is not None else (FIXTURES / f"{report_id}.html").read_bytes()
        self.pages[f"/search/view/ptr/{report_id}/"] = (status, page)

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        jar = {cookie["name"]: cookie["value"] for cookie in cookies}
        path = urlsplit(request.url).path
        if path == "/search/home/" and request.method == "GET":
            return self._answer(path, 200, _AGREEMENT.encode(), cookies, csrftoken="tok")
        if path == "/search/home/":
            return self._answer("/search/", 200, b"<h1>Search</h1>", cookies, sessionid="s1")
        if "sessionid" not in jar:
            return self._answer("/search/home/", 200, _AGREEMENT.encode(), cookies)
        if path == "/search/report/data/":
            return self._search(dict(request.form or ()), cookies)
        if path in self.pages:
            self.asked.append(path.rstrip("/").rsplit("/", 1)[1])
            status, body = self.pages[path]
            return self._answer(path, status, body, cookies)
        return self._answer(path, 404, b"<h1>Not Found</h1>", cookies)

    def _search(self, form: dict[str, str], cookies: Sequence[CookieRecord]) -> FetchResponse:
        self.searched.append(form)

        def day(value: str) -> date:
            return datetime.strptime(value.split()[0], "%m/%d/%Y").date()

        first = day(form["submitted_start_date"])
        last = day(form["submitted_end_date"]) if form["submitted_end_date"] else date.max
        matching = [row for row in self.rows if first <= day(row[4]) <= last]
        start, length = int(form["start"]), int(form["length"])
        body = {
            "draw": 1,
            "recordsTotal": len(self.rows),
            "recordsFiltered": len(matching),
            "data": matching[start : start + length],
            "result": "ok",
        }
        return self._answer(
            "/search/report/data/", 200, json.dumps(body).encode(), cookies, json_=True
        )

    @staticmethod
    def _answer(
        path: str,
        status: int,
        body: bytes,
        cookies: Sequence[CookieRecord],
        *,
        json_: bool = False,
        **setting: str,
    ) -> FetchResponse:
        jar = {cookie["name"]: cookie for cookie in cookies}
        jar |= {name: _cookie(name, value) for name, value in setting.items()}
        kind = "application/json" if json_ else "text/html; charset=utf-8"
        return FetchResponse(
            status=status,
            url=EFD + path,
            headers=(("Content-Type", kind),),
            body=body,
            cookies=tuple(jar.values()),
        )


@pytest.fixture
def efd() -> FakeEfd:
    fake = FakeEfd()
    fake.rows = [
        _row(
            "ptr",
            BOOZMAN,
            "Boozman",
            date(2026, 10, 21),
            "Periodic Transaction Report for 10/21/2026",
        ),
        _row(
            "ptr",
            HAGERTY_AMENDMENT,
            "Hagerty",
            date(2026, 10, 14),
            "Periodic Transaction Report for 04/13/2022 (Amendment 1)",
        ),
        _row(
            "paper",
            PAPER_ID,
            "BLUMENTHAL",
            date(2026, 10, 9),
            "Periodic Transaction Report for 10/09/2026",
        ),
        _row(
            "ptr", EARLY_ID, "Early", date(2026, 9, 1), "Periodic Transaction Report for 09/01/2026"
        ),
    ]
    fake.report(BOOZMAN)
    fake.report(HAGERTY_AMENDMENT)
    fake.report(EARLY_ID, (FIXTURES / f"{BOOZMAN}.html").read_bytes())
    return fake


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def alert_ops(_: Settings, kind: str, message: str) -> bool:
        sent.append((kind, message))
        return True

    monkeypatch.setattr("app.jobs.congress.alert_ops", alert_ops)
    return sent


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    migrated_engine: AsyncEngine,
    efd: FakeEfd,
    alerts: list[tuple[str, str]],
) -> Iterator[Settings]:
    @asynccontextmanager
    async def open_fake(_: Settings) -> AsyncIterator[FakeEfd]:
        yield efd

    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.new_york_today", lambda: TODAY)
    monkeypatch.setattr("app.jobs.congress.open_fetcher", open_fake)
    monkeypatch.setattr("app.jobs.congress.EFD_INTERVAL_SECONDS", 0.0)
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    truncate = text(
        "TRUNCATE ingestion_run, congress_transaction, congress_filing, congress_filed_name "
        "RESTART IDENTITY CASCADE"
    )
    _execute(migrated_engine, truncate, text(f"DELETE FROM crawl_watermark WHERE job = '{JOB}'"))
    yield
    _execute(migrated_engine, truncate, text(f"DELETE FROM crawl_watermark WHERE job = '{JOB}'"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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


def _filings(engine: AsyncEngine) -> dict[str, dict[str, Any]]:
    columns = (
        "doc_id",
        "chamber",
        "last_name",
        "office",
        "filing_date",
        "year",
        "report_date",
        "amendment",
        "document_url",
        "document_key",
        "extraction",
        "parser_version",
        "parse_error",
        "source_key",
    )
    rows = _fetch(engine, select(*(getattr(CongressFiling, name) for name in columns)))
    return {row[0]: dict(zip(columns, row, strict=True)) for row in rows}


def _lines(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
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


def _watermark(engine: AsyncEngine) -> date | None:
    rows = _fetch(engine, select(CrawlWatermark.last_date).where(CrawlWatermark.job == JOB))
    return rows[0][0] if rows else None


def _runs(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(IngestionRun.status, IngestionRun.error, IngestionRun.metrics)
        .where(IngestionRun.job_name == JOB)
        .order_by(IngestionRun.started_at),
    )


def _invoke(runner: CliRunner, *arguments: str) -> Any:
    return runner.invoke(app, [JOB, *arguments])


# --- a first run ------------------------------------------------------------------------


def test_a_run_lists_the_window_and_reads_each_report(
    runner: CliRunner, migrated_engine: AsyncEngine, efd: FakeEfd, settings: Settings
) -> None:
    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 0, result.output
    filings = _filings(migrated_engine)
    assert sorted(filings) == sorted([BOOZMAN, HAGERTY_AMENDMENT, PAPER_ID])
    boozman = filings[BOOZMAN]
    assert boozman | {"source_key": None} == {
        "doc_id": BOOZMAN,
        "chamber": "senate",
        "last_name": "Boozman",
        "office": "Senator",
        "filing_date": date(2026, 10, 21),
        "year": 2026,
        "report_date": date(2026, 10, 21),
        "amendment": None,
        "document_url": f"{EFD}/search/view/ptr/{BOOZMAN}/",
        "document_key": f"raw/senate-efd/report/ptr/{BOOZMAN}.html",
        "extraction": "html",
        "parser_version": SENATE_PARSER_VERSION,
        "parse_error": None,
        "source_key": None,
    }
    assert boozman["source_key"].startswith("raw/senate-efd/search/")
    assert (filings[HAGERTY_AMENDMENT]["amendment"], filings[HAGERTY_AMENDMENT]["report_date"]) == (
        1,
        date(2022, 4, 13),
    )
    paper = filings[PAPER_ID]
    assert paper["document_url"] == f"{EFD}/search/view/paper/{PAPER_ID}/"
    assert (paper["document_key"], paper["extraction"], paper["parse_error"]) == (
        None,
        "none",
        PAPER_REASON,
    )

    lines = _lines(migrated_engine)
    assert [line[:3] for line in lines[BOOZMAN]] == [(n, "parsed", "html") for n in range(1, 8)]
    assert lines[BOOZMAN][0][3] == "MU"
    assert lines[HAGERTY_AMENDMENT] == [(1, "parsed", "html", "TACO")]
    assert lines[PAPER_ID] == [(1, "placeholder", "none", None)]

    # The paper filing is linked, never fetched; the reports are archived.
    assert sorted(efd.asked) == sorted([BOOZMAN, HAGERTY_AMENDMENT])
    archive = settings.raw_store_local_root
    assert (archive / f"raw/senate-efd/report/ptr/{BOOZMAN}.html").read_bytes() == (
        FIXTURES / f"{BOOZMAN}.html"
    ).read_bytes()
    assert len(list((archive / "raw/senate-efd/search").iterdir())) == 1

    # Senators and former senators, PTRs only, the window as given, to today.
    [form] = efd.searched
    assert (form["report_types"], form["filer_types"]) == ("[11]", "[1, 5]")
    assert form["submitted_start_date"] == "10/01/2026 00:00:00"
    assert form["submitted_end_date"] == "11/05/2026 23:59:59"

    assert _watermark(migrated_engine) == date(2026, 10, 21)
    [(status, error, metrics)] = _runs(migrated_engine)
    assert (status, error) == ("success", None)
    assert metrics["listed"] == 3
    assert metrics["filings_read"] == 2
    assert metrics["placeholders"] == 1
    assert metrics["rows_parsed"] == 8
    assert "2/3 filings read" in result.output
    assert "watermark none -> 2026-10-21" in result.output


def test_a_catch_up_reads_only_what_is_new(
    runner: CliRunner, migrated_engine: AsyncEngine, efd: FakeEfd
) -> None:
    assert _invoke(runner, "--from", "2026-10-01").exit_code == 0
    efd.asked.clear()
    efd.searched.clear()
    later = "c0ffee00-0000-4000-8000-000000000001"
    efd.rows.append(
        _row("ptr", later, "Later", date(2026, 11, 2), "Periodic Transaction Report for 11/02/2026")
    )
    efd.report(later, (FIXTURES / f"{BOOZMAN}.html").read_bytes())

    result = _invoke(runner, "--catch-up")

    assert result.exit_code == 0, result.output
    # Two weeks back from the watermark, 21 October.
    assert efd.searched[0]["submitted_start_date"] == "10/07/2026 00:00:00"
    assert efd.asked == [later]
    assert _watermark(migrated_engine) == date(2026, 11, 2)
    assert len(_lines(migrated_engine)[later]) == 7


def test_a_catch_up_needs_a_watermark(runner: CliRunner) -> None:
    result = _invoke(runner, "--catch-up")

    assert result.exit_code == 1
    assert "--from first" in result.output


def test_a_window_that_ends_before_today_leaves_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    result = _invoke(runner, "--from", "2026-10-01", "--to", "2026-10-15")

    assert result.exit_code == 0, result.output
    assert sorted(_filings(migrated_engine)) == sorted([HAGERTY_AMENDMENT, PAPER_ID])
    assert _watermark(migrated_engine) is None


def test_a_search_of_many_pages_lists_them_all(
    runner: CliRunner, migrated_engine: AsyncEngine, efd: FakeEfd, settings: Settings
) -> None:
    efd.rows = [
        _row(
            "paper",
            str(uuid.UUID(int=n)),
            f"Paper {n}",
            date(2026, 10, 1 + n % 28),
            "Periodic Transaction Report",
        )
        for n in range(1, 231)
    ]

    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 0, result.output
    assert [form["start"] for form in efd.searched] == ["0", "100", "200"]
    assert len(_filings(migrated_engine)) == 230
    assert len(list((settings.raw_store_local_root / "raw/senate-efd/search").iterdir())) == 3


def test_a_reparse_reads_the_archive_and_fetches_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine, efd: FakeEfd
) -> None:
    assert _invoke(runner, "--from", "2026-10-01").exit_code == 0
    efd.asked.clear()
    efd.searched.clear()

    result = _invoke(runner, "--no-search", "--reparse")

    assert result.exit_code == 0, result.output
    assert efd.asked == []
    assert efd.searched == []
    assert len(_lines(migrated_engine)[BOOZMAN]) == 7


# --- what goes wrong ---------------------------------------------------------------------


def test_a_report_that_fails_is_tried_again_and_the_rest_are_read(
    runner: CliRunner, migrated_engine: AsyncEngine, efd: FakeEfd
) -> None:
    efd.report(HAGERTY_AMENDMENT, b"<h1>Server Error</h1>", status=500)

    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 1
    assert f"{HAGERTY_AMENDMENT} failed" in " ".join(result.output.split())
    assert HAGERTY_AMENDMENT not in _lines(migrated_engine)
    assert len(_lines(migrated_engine)[BOOZMAN]) == 7
    [(status, error, _)] = _runs(migrated_engine)
    assert status == "partial"
    assert "answered 500" in error
    # The search was whole, so the watermark moves: the filing waits to be read.
    assert _watermark(migrated_engine) == date(2026, 10, 21)

    efd.report(HAGERTY_AMENDMENT)
    assert _invoke(runner, "--catch-up").exit_code == 0
    assert _lines(migrated_engine)[HAGERTY_AMENDMENT] == [(1, "parsed", "html", "TACO")]


def test_a_layout_change_stops_the_run_and_tells_ops(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    efd: FakeEfd,
    alerts: list[tuple[str, str]],
) -> None:
    moved = (FIXTURES / f"{BOOZMAN}.html").read_bytes().replace(b">Comment</th>", b">Notes</th>")
    efd.report(BOOZMAN, moved)

    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 1
    assert "no transaction table" in result.output
    [(kind, message)] = alerts
    assert kind == "senate-efd-layout"
    assert f"raw/senate-efd/report/ptr/{BOOZMAN}.html" in message
    [(status, error, _)] = _runs(migrated_engine)
    assert status == "failed"
    assert error.startswith("SenateLayoutError: ")
    # Never zero rows quietly: the filing stays unread, and is read once eFD is.
    assert _filings(migrated_engine)[BOOZMAN]["parser_version"] is None
    assert BOOZMAN not in _lines(migrated_engine)

    # What was archived was the moved page: it is fetched again, and archived over.
    efd.report(BOOZMAN)
    efd.asked.clear()
    assert _invoke(runner, "--catch-up").exit_code == 0
    assert BOOZMAN in efd.asked
    assert len(_lines(migrated_engine)[BOOZMAN]) == 7


def test_a_search_answer_not_in_efds_shape_stops_the_run_and_tells_ops(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    efd: FakeEfd,
    alerts: list[tuple[str, str]],
) -> None:
    efd.rows = [["Jane", "Doe", "Senator", "<b>a link no more</b>", "10/21/2026"]]

    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 1
    assert "not one link" in result.output
    assert [kind for kind, _ in alerts] == ["senate-efd-layout"]
    assert _fetch(migrated_engine, select(func.count()).select_from(CongressFiling)) == [(0,)]
    assert _watermark(migrated_engine) is None


def test_weeks_with_no_senate_ptr_is_reported_as_quiet(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    efd: FakeEfd,
    alerts: list[tuple[str, str]],
) -> None:
    efd.rows = [r for r in efd.rows if r[1] == "Early"]  # 1 September only

    result = _invoke(runner, "--from", "2026-08-01")

    assert result.exit_code == 1
    assert "QUIET: no Senate PTR in 65 days" in result.output
    assert [kind for kind, _ in alerts] == ["senate-efd-quiet"]
    [(status, _, metrics)] = _runs(migrated_engine)
    assert (status, metrics["quiet_days"]) == ("partial", 65)


def test_no_fetcher_stops_the_run(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def no_fetcher(_: Settings) -> AsyncIterator[None]:
        raise FetcherUnavailable("no US fetcher is configured")
        yield  # pragma: no cover

    monkeypatch.setattr("app.jobs.congress.open_fetcher", no_fetcher)

    result = _invoke(runner, "--from", "2026-10-01")

    assert result.exit_code == 1
    assert "no US fetcher" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ("--from", "2026-10-01", "--catch-up"),
        ("--to", "2026-10-01"),
        ("--no-search", "--from", "2026-10-01"),
        ("--from", "2011-12-31"),
        ("--from", "2026-10-02", "--to", "2026-10-01"),
    ],
    ids=" ".join,
)
def test_a_window_that_makes_no_sense_is_refused(
    runner: CliRunner, arguments: tuple[str, ...]
) -> None:
    assert _invoke(runner, *arguments).exit_code == 2
