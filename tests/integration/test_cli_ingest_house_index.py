"""``ingest-house-index``: the House Clerk's yearly ZIPs into ``congress_filing``.

Against a real Postgres, because what is worth asserting is what a run leaves
behind — the rows, their timestamps, the watermark — and what a second run does
not change. The fetcher is a fake that serves ZIPs by URL (the client and the
real fetcher over a socket are tests/test_house_index.py's), and "today" is
pinned to Thursday 5 November 2026.
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
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import CongressFiling, CrawlWatermark, IngestionRun
from app.ingestion.congress.house_clerk import index_url
from app.ingestion.remote.fetcher import FetchRequest, FetchResponse
from app.ingestion.remote.handler import CookieRecord
from tests.conftest import make_settings
from tests.house_index_fixtures import house_zip, member

TODAY: Final = date(2026, 11, 5)
JOB: Final = "ingest-house-index"

PELOSI: Final = member("20033725", "Pelosi", "Nancy", office="CA11", filed="1/23/2026")
ALFORD: Final = member("20034201", "Alford", "Mark", office="MO04", filed="11/3/2026")
# Filed in 2026, indexed under 2025.
LATE: Final = member(
    "20032999", "Aderholt", "Robert B.", office="AL04", year=2025, filed="1/16/2026"
)
OLDER: Final = member("20031000", "Allen", "Rick", office="GA12", year=2025, filed="9/10/2025")
ANNUAL: Final = member("10072640", "Abel", filing_type="O", filed="5/15/2026")


class FakeFetcher:
    """Answers each URL with what :attr:`answers` has for it, a 404 otherwise."""

    backend = "local"

    def __init__(self) -> None:
        self.answers: dict[str, tuple[int, bytes]] = {}
        self.asked: list[str] = []

    def serve(self, year: int, *members: dict[str, str], status: int = 200) -> None:
        body = house_zip(year, *members) if status == 200 else b"<html>error</html>"
        self.answers[index_url(year)] = (status, body)

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        self.asked.append(request.url)
        status, body = self.answers.get(request.url, (404, b"<html>404</html>"))
        return FetchResponse(status=status, url=request.url, headers=(), body=body)


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    migrated_engine: AsyncEngine,
    fetcher: FakeFetcher,
) -> Iterator[Settings]:
    @asynccontextmanager
    async def open_fake(_: Settings) -> AsyncIterator[FakeFetcher]:
        yield fetcher

    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.new_york_today", lambda: TODAY)
    monkeypatch.setattr("app.jobs.congress.open_fetcher", open_fake)
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text("TRUNCATE ingestion_run, crawl_watermark, congress_filing RESTART IDENTITY CASCADE"),
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


def _filings(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            CongressFiling.chamber,
            CongressFiling.doc_id,
            CongressFiling.report_type,
            CongressFiling.last_name,
            CongressFiling.first_name,
            CongressFiling.office,
            CongressFiling.filing_date,
            CongressFiling.year,
        ).order_by(CongressFiling.doc_id),
    )


def _stamps(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            CongressFiling.doc_id,
            CongressFiling.source_key,
            CongressFiling.first_seen_at,
            CongressFiling.updated_at,
        ).order_by(CongressFiling.doc_id),
    )


def _watermark(engine: AsyncEngine) -> date | None:
    rows = _fetch(engine, select(CrawlWatermark.last_date).where(CrawlWatermark.job == JOB))
    return rows[0][0] if rows else None


def _set_watermark(engine: AsyncEngine, day: date) -> None:
    _execute(engine, insert(CrawlWatermark).values(job=JOB, last_date=day))


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
    root = Path(settings.raw_store_local_root) / "raw" / "house-clerk" / "index"
    return sorted(str(path.relative_to(root)) for path in root.rglob("*.zip"))


# --- a sweep ---------------------------------------------------------------------


def test_a_sweep_stores_each_years_ptrs_archives_each_zip_and_sets_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher, cli_settings: Settings
) -> None:
    fetcher.serve(2025, OLDER, LATE)
    fetcher.serve(2026, PELOSI, ANNUAL, ALFORD)

    result = runner.invoke(app, ["ingest-house-index", "--from-year", "2025"])

    assert result.exit_code == 0, result.output
    assert fetcher.asked == [index_url(2025), index_url(2026)]
    assert _filings(migrated_engine) == [
        ("house", "20031000", "ptr", "Allen", "Rick", "GA12", date(2025, 9, 10), 2025),
        ("house", "20032999", "ptr", "Aderholt", "Robert B.", "AL04", date(2026, 1, 16), 2025),
        ("house", "20033725", "ptr", "Pelosi", "Nancy", "CA11", date(2026, 1, 23), 2026),
        ("house", "20034201", "ptr", "Alford", "Mark", "MO04", date(2026, 11, 3), 2026),
    ]
    assert [year[:4] for year in _archived(cli_settings)] == ["2025", "2026"]
    assert _watermark(migrated_engine) == date(2026, 11, 3)
    [(status, seen, written, metrics, error)] = _runs(migrated_engine)
    assert (status, seen, written, error) == ("success", 4, 4, None)
    assert metrics["inserted"] == 4
    assert metrics["archived"] == 2
    assert "watermark none -> 2026-11-03" in result.output


def test_reading_the_same_year_again_changes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher, cli_settings: Settings
) -> None:
    fetcher.serve(2026, PELOSI, ALFORD)
    assert runner.invoke(app, ["ingest-house-index", "--from-year", "2026"]).exit_code == 0
    before = (_stamps(migrated_engine), _archived(cli_settings), _watermark(migrated_engine))
    # The nightly rebuild: new ZIP bytes, the same index.
    fetcher.serve(2026, PELOSI, ALFORD)

    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])

    assert result.exit_code == 0, result.output
    assert (_stamps(migrated_engine), _archived(cli_settings), _watermark(migrated_engine)) == (
        before
    )
    assert _runs(migrated_engine)[-1][2] == 0  # items_written
    assert "0 new     0 changed  index unchanged" in result.output


def test_a_corrected_row_is_updated_and_keeps_where_it_was_first_seen(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(2026, PELOSI, ALFORD)
    assert runner.invoke(app, ["ingest-house-index", "--year", "2026"]).exit_code == 0
    [pelosi_before, alford_before] = _stamps(migrated_engine)
    fetcher.serve(2026, PELOSI, {**ALFORD, "First": "Mark A."})

    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])

    assert result.exit_code == 0, result.output
    [pelosi_after, alford_after] = _stamps(migrated_engine)
    assert pelosi_after == pelosi_before
    assert alford_after[1:3] == alford_before[1:3]  # source_key, first_seen_at
    assert alford_after[3] > alford_before[3]
    assert _filings(migrated_engine)[1][4] == "Mark A."
    assert "0 new     1 changed  index new" in result.output


# --- the watermark ---------------------------------------------------------------


def test_one_year_is_read_without_moving_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _set_watermark(migrated_engine, date(2026, 6, 1))
    fetcher.serve(2026, PELOSI, ALFORD)

    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])

    assert result.exit_code == 0, result.output
    assert len(_filings(migrated_engine)) == 2
    assert _watermark(migrated_engine) == date(2026, 6, 1)


def test_a_catch_up_reads_the_watermarks_year_the_two_before_and_on_to_today(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _set_watermark(migrated_engine, date(2025, 12, 30))
    fetcher.serve(2023)
    fetcher.serve(2024)
    fetcher.serve(2025, OLDER, LATE)
    fetcher.serve(2026, PELOSI)

    result = runner.invoke(app, ["ingest-house-index"])

    assert result.exit_code == 0, result.output
    assert fetcher.asked == [index_url(year) for year in (2023, 2024, 2025, 2026)]
    assert len(_filings(migrated_engine)) == 3
    assert _watermark(migrated_engine) == date(2026, 1, 23)


def test_the_watermark_never_moves_back(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _set_watermark(migrated_engine, date(2026, 11, 4))
    fetcher.serve(2024)
    fetcher.serve(2025, OLDER)
    fetcher.serve(2026, PELOSI)

    result = runner.invoke(app, ["ingest-house-index", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert _watermark(migrated_engine) == date(2026, 11, 4)


def test_a_filing_dated_after_today_is_stored_and_does_not_move_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(2026, PELOSI, member("20039999", "Typo", filed="3/2/2062"))

    result = runner.invoke(app, ["ingest-house-index", "--from-year", "2026"])

    assert result.exit_code == 0, result.output
    assert len(_filings(migrated_engine)) == 2
    assert _watermark(migrated_engine) == date(2026, 1, 23)


def test_a_catch_up_with_no_watermark_says_where_to_start(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    result = runner.invoke(app, ["ingest-house-index", "--catch-up"])

    assert result.exit_code == 1
    assert "--from-year" in result.output
    assert fetcher.asked == []
    assert _runs(migrated_engine)[0][0] == "failed"


# --- what can go wrong -------------------------------------------------------------


def test_the_current_year_not_published_yet_is_not_a_failure(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(2025, OLDER)  # and no 2026 yet: the first days of January

    result = runner.invoke(app, ["ingest-house-index", "--from-year", "2025"])

    assert result.exit_code == 0, result.output
    assert "2026  not published yet" in result.output
    assert _watermark(migrated_engine) == date(2025, 9, 10)


def test_a_failed_year_keeps_the_others_and_holds_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _set_watermark(migrated_engine, date(2026, 1, 1))
    fetcher.serve(2024, status=503)
    fetcher.serve(2025, OLDER)
    fetcher.serve(2026, PELOSI)

    result = runner.invoke(app, ["ingest-house-index", "--catch-up"])

    assert result.exit_code == 1
    assert "2024  FAILED  the House Clerk answered 503" in result.output
    assert [row[1] for row in _filings(migrated_engine)] == ["20031000", "20033725"]
    assert _watermark(migrated_engine) == date(2026, 1, 1)
    [(status, _, written, _, error)] = _runs(migrated_engine)
    assert (status, written) == ("partial", 2)
    assert "2024: the House Clerk answered 503" in error


def test_a_past_year_that_is_missing_is_a_failure(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(2026, PELOSI)

    result = runner.invoke(app, ["ingest-house-index", "--from-year", "2025"])

    assert result.exit_code == 1
    assert "2025  FAILED  the House Clerk has no 2025 index" in result.output
    assert _watermark(migrated_engine) is None


def test_a_malformed_ptr_row_is_left_out_and_makes_the_run_partial(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(2026, PELOSI, member("20039998", "Undated", filed=""))

    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])

    assert result.exit_code == 0, result.output
    assert [row[1] for row in _filings(migrated_engine)] == ["20033725"]
    assert "row 2 (20039998) left out" in result.output
    [(status, _, _, _, error)] = _runs(migrated_engine)
    assert status == "partial"
    assert "2026 row 2 (20039998)" in error


def test_no_fetcher_configured_fails_the_run_before_any_year(
    runner: CliRunner, migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ingestion.remote import fetcher as real

    monkeypatch.setattr("app.jobs.congress.open_fetcher", real.open_fetcher)

    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])

    assert result.exit_code == 1
    assert "no US fetcher is configured" in result.output
    assert _runs(migrated_engine)[0][0] == "failed"
    assert _filings(migrated_engine) == []


@pytest.mark.parametrize(
    "args",
    [
        ["--year", "2007"],
        ["--year", "2027"],
        ["--from-year", "2026", "--catch-up"],
        ["--year", "2026", "--from-year", "2025"],
    ],
)
def test_years_the_clerk_cannot_have_and_mixed_choices_are_usage_errors(
    runner: CliRunner, args: list[str], fetcher: FakeFetcher
) -> None:
    result = runner.invoke(app, ["ingest-house-index", *args])

    assert result.exit_code == 2
    assert fetcher.asked == []
