"""``crawl-form4``: EDGAR's daily index, bounded to a universe, into ``pending_filing``.

Against a real Postgres because what is worth asserting is what a run leaves
behind: the queue rows, the watermark, and both after a failure partway. EDGAR
is faked with respx. The quarter listing is the real 2026Q4 one
(``tests/fixtures/daily_index``), which has no 12 October, Columbus Day, and
"today" is pinned to Wednesday 14 October 2026, before its index exists.

The week the tests walk::

    Thu 8   indexed   Elastic's Form 4 (under Elastic and its insider),
                      Steel Connect's, 3M's
    Fri 9   indexed   Elastic's 4/A
    Sat 10, Sun 11    weekend
    Mon 12            no index: Columbus Day
    Tue 13  indexed   Steel Connect's
    Wed 14            today, not published yet
"""

import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket
from app.db.models import (
    CrawlWatermark,
    Filer,
    Filing,
    IngestionRun,
    PendingFiling,
    PositionSnapshot,
    SecCompanyTicker,
    Security,
)
from app.db.session import create_session_factory
from app.ingestion.backfill import plan_backfill
from app.ingestion.edgar.company_tickers import COMPANY_TICKERS_URL
from app.ingestion.edgar.daily_index import daily_index_url, quarter_listing_url
from tests.conftest import make_settings

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "daily_index"
LISTING_2026Q4: Final = json.loads((FIXTURES / "2026-QTR4-index.json").read_text())
#: The real index's header: description, blank lines, the wrapped column
#: header and the rule of dashes.
HEADER: Final = b"".join((FIXTURES / "form.20241002.idx").read_bytes().splitlines(True)[:11])

TODAY: Final = date(2026, 10, 14)

ELASTIC: Final = "0001707753"
ELASTIC_INSIDER: Final = "0001275992"
STEEL_CONNECT: Final = "0000914712"
THREE_M: Final = "0000066740"
BERKSHIRE: Final = "0001067983"

ELASTIC_4: Final = "0001062993-26-000001"
STEEL_4: Final = "0000921895-26-000002"
THREE_M_4: Final = "0001127602-26-000003"
ELASTIC_4A: Final = "0001062993-26-000004"
STEEL_4_LATER: Final = "0000921895-26-000005"

#: Each indexed day's lines: (form, name, CIK, accession).
_DAYS: Final = {
    date(2026, 10, 8): (
        ("4", "Elastic N.V.", ELASTIC, ELASTIC_4),
        ("4", "ABBASI SOHAIB", ELASTIC_INSIDER, ELASTIC_4),
        ("4", "Steel Connect, Inc.", STEEL_CONNECT, STEEL_4),
        ("4", "3M CO", THREE_M, THREE_M_4),
        # Starts with a 4, and is not one.
        ("424B2", "Steel Connect, Inc.", STEEL_CONNECT, "0000921895-26-000099"),
    ),
    date(2026, 10, 9): (("4/A", "Elastic N.V.", ELASTIC, ELASTIC_4A),),
    date(2026, 10, 13): (("4", "Steel Connect, Inc.", STEEL_CONNECT, STEEL_4_LATER),),
}


def _index(day: date, *lines: tuple[str, str, str, str]) -> bytes:
    body = HEADER
    for form, name, cik, accession in lines:
        path = f"edgar/data/{int(cik)}/{accession}.txt"
        body += f"{form:<12}{name:<62}{int(cik):<12}{day:%Y%m%d}    {path}\n".encode()
    return body


def _edgar(**overrides: httpx.Response) -> dict[date, respx.Route]:
    """The 2026Q4 listing, and each indexed day; ``overrides`` by ``d20261009``."""
    respx.get(quarter_listing_url(2026, 4)).mock(
        return_value=httpx.Response(200, json=LISTING_2026Q4)
    )
    routes = {}
    for day, lines in _DAYS.items():
        response = overrides.get(f"d{day:%Y%m%d}", httpx.Response(200, content=_index(day, *lines)))
        routes[day] = respx.get(daily_index_url(day)).mock(return_value=response)
    return routes


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def universe_csv(tmp_path: Path) -> Path:
    """An S&P 500 file with Elastic and Steel Connect in it, and not 3M."""
    path = tmp_path / "sp500.csv"
    path.write_text(
        "Symbol,Security,CIK\nESTC,Elastic,1707753\nSTCN,Steel Connect,914712\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    migrated_engine: AsyncEngine,
    universe_csv: Path,
) -> Iterator[Settings]:
    """The container's DSN, an unthrottled client, the S&P 500 universe from
    :func:`universe_csv`, and today pinned."""
    configured = settings.model_copy(
        update={"form4_universe": "sp500", "form4_sp500_path": universe_csv}
    )
    monkeypatch.setattr("app.cli.get_settings", lambda: configured)
    monkeypatch.setattr("app.cli.new_york_today", lambda: TODAY)
    monkeypatch.setattr(
        "app.ingestion.edgar.client.get_edgar_limiter",
        lambda rate_per_second: AsyncTokenBucket(10_000.0),
    )
    yield configured
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
        text(
            "TRUNCATE ingestion_run, crawl_watermark, sec_company_ticker, pending_filing, "
            "position_snapshot, holding, filing, security, filer_cik, filer "
            "RESTART IDENTITY CASCADE"
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


def _queue(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            PendingFiling.accession_no,
            PendingFiling.cik,
            PendingFiling.form_type,
            PendingFiling.filing_date,
            PendingFiling.report_date,
            PendingFiling.status,
        ).order_by(PendingFiling.accession_no),
    )


def _watermark(engine: AsyncEngine) -> date | None:
    rows = _fetch(engine, select(CrawlWatermark.last_date))
    return rows[0][0] if rows else None


def _set_watermark(engine: AsyncEngine, day: date) -> None:
    _execute(engine, insert(CrawlWatermark).values(job="crawl-form4", last_date=day))


def _run(engine: AsyncEngine) -> tuple[Any, ...]:
    [row] = _fetch(
        engine,
        select(
            IngestionRun.status,
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.metrics,
            IngestionRun.error,
        ).where(IngestionRun.job_name == "crawl-form4"),
    )
    return row


# --- a range -------------------------------------------------------------------


@respx.mock
def test_a_range_queues_the_universes_form_4s_and_sets_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()

    result = runner.invoke(app, ["crawl-form4", "--from", "2026-10-08", "--to", "2026-10-14"])

    assert result.exit_code == 0, result.output
    # Elastic's once, though listed under Elastic and its insider; 3M's not at all.
    assert _queue(migrated_engine) == [
        (STEEL_4, STEEL_CONNECT, "4", date(2026, 10, 8), None, "pending"),
        (STEEL_4_LATER, STEEL_CONNECT, "4", date(2026, 10, 13), None, "pending"),
        (ELASTIC_4, ELASTIC, "4", date(2026, 10, 8), None, "pending"),
        (ELASTIC_4A, ELASTIC, "4/A", date(2026, 10, 9), None, "pending"),
    ]
    # Through the weekend and the holiday, and not into today.
    assert _watermark(migrated_engine) == date(2026, 10, 13)
    assert "2026-10-12  no index (holiday)" in result.output
    assert "2026-10-14  not published yet: stopped here" in result.output
    assert "watermark none -> 2026-10-13" in result.output

    status, seen, written, metrics, error = _run(migrated_engine)
    assert (status, seen, written, error) == ("success", 4, 4, None)
    assert metrics["universe_issuers"] == 2
    assert metrics["days_read"] == 6
    assert metrics["stopped"] == "not published yet"


@respx.mock
def test_no_index_is_requested_for_a_day_the_listing_does_not_have(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """A weekend, a holiday and today: requested, each would be S3's 403."""
    _edgar()
    holiday = respx.get(daily_index_url(date(2026, 10, 12))).mock(return_value=httpx.Response(403))

    result = runner.invoke(app, ["crawl-form4", "--from", "2026-10-10", "--to", "2026-10-12"])

    assert result.exit_code == 0, result.output
    assert holiday.call_count == 0
    assert _watermark(migrated_engine) == date(2026, 10, 12)


@respx.mock
def test_running_a_day_again_queues_nothing_twice(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()

    for _ in range(3):
        result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])
        assert result.exit_code == 0, result.output

    assert [row[0] for row in _queue(migrated_engine)] == [STEEL_4, ELASTIC_4]


@respx.mock
def test_a_loaded_filing_is_not_queued(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    """The same set difference as for 13Fs: a Form 4 the loader has loaded is
    a ``filing`` row whose parse is ok."""
    _execute(
        migrated_engine,
        insert(Filing).values(
            accession_no=ELASTIC_4,
            cik=ELASTIC,
            form_type="4",
            filed_at=datetime(2026, 10, 8, 20, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        ),
    )
    _edgar()

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    assert result.exit_code == 0, result.output
    assert [row[0] for row in _queue(migrated_engine)] == [STEEL_4]
    assert "2 matched      1 new" in result.output


# --- the watermark ---------------------------------------------------------------


@respx.mock
def test_catch_up_walks_from_the_day_after_the_watermark(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    routes = _edgar()
    _set_watermark(migrated_engine, date(2026, 10, 9))

    result = runner.invoke(app, ["crawl-form4", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert routes[date(2026, 10, 8)].call_count == 0
    assert routes[date(2026, 10, 9)].call_count == 0
    assert [row[0] for row in _queue(migrated_engine)] == [STEEL_4_LATER]
    assert _watermark(migrated_engine) == date(2026, 10, 13)
    assert "watermark 2026-10-09 -> 2026-10-13" in result.output


@respx.mock
def test_no_days_given_is_a_catch_up(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _edgar()
    _set_watermark(migrated_engine, date(2026, 10, 12))

    result = runner.invoke(app, ["crawl-form4"])

    assert result.exit_code == 0, result.output
    assert _watermark(migrated_engine) == date(2026, 10, 13)


def test_catch_up_without_a_watermark_fails_without_asking_edgar(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """respx is not active: a request would leave for sec.gov and fail."""
    result = runner.invoke(app, ["crawl-form4", "--catch-up"])

    assert result.exit_code == 1
    assert "no crawl has set a watermark" in result.stderr
    assert _run(migrated_engine)[0] == "failed"


def test_a_watermark_at_today_has_nothing_to_crawl(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _set_watermark(migrated_engine, TODAY)

    result = runner.invoke(app, ["crawl-form4", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert "nothing to crawl" in result.output


@respx.mock
def test_a_day_past_the_watermark_is_crawled_without_moving_it(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """The days between were not read. Moving it would make --catch-up skip them."""
    _edgar()
    _set_watermark(migrated_engine, date(2026, 10, 1))

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-13"])

    assert result.exit_code == 0, result.output
    assert [row[0] for row in _queue(migrated_engine)] == [STEEL_4_LATER]
    assert _watermark(migrated_engine) == date(2026, 10, 1)
    assert "watermark 2026-10-01, unchanged" in result.output


@respx.mock
def test_a_day_before_the_watermark_does_not_move_it_back(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar()
    _set_watermark(migrated_engine, date(2026, 10, 13))

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    assert result.exit_code == 0, result.output
    assert _watermark(migrated_engine) == date(2026, 10, 13)


@respx.mock
def test_a_failed_day_stops_the_walk_and_keeps_the_days_before_it(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Advance only after a day fully succeeds: the 9th is read again next run."""
    routes = _edgar(d20261009=httpx.Response(500))

    result = runner.invoke(app, ["crawl-form4", "--from", "2026-10-08", "--to", "2026-10-13"])

    assert result.exit_code == 1
    assert [row[0] for row in _queue(migrated_engine)] == [STEEL_4, ELASTIC_4]
    assert _watermark(migrated_engine) == date(2026, 10, 8)
    assert routes[date(2026, 10, 13)].call_count == 0
    assert "2026-10-09  FAILED  " in result.output

    status, _, _, metrics, error = _run(migrated_engine)
    assert status == "partial"
    assert error.startswith("2026-10-09: HTTPStatusError")
    assert metrics["stopped"] == "failed"


@respx.mock
def test_a_malformed_index_fails_its_day(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _edgar(d20261008=httpx.Response(200, content=b"<html>Not an index</html>"))

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    assert result.exit_code == 1
    assert "DailyIndexMalformedError" in result.output
    assert _watermark(migrated_engine) is None


# --- the universe ---------------------------------------------------------------


def _held(engine: AsyncEngine) -> None:
    """Berkshire's own class B at 50% of a portfolio, and 3M at 0.5%."""
    _execute(
        engine,
        insert(Filer).values(id=1, name="Some Fund", slug="some-fund"),
        insert(Filing).values(
            id=1,
            accession_no="0000950123-26-000001",
            cik="0000000001",
            filer_id=1,
            form_type="13F-HR",
            period_of_report=date(2026, 6, 30),
            filed_at=datetime(2026, 8, 14, 20, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        ),
        insert(Security).values(
            [
                {"id": 1, "cusip": "084670702", "ticker": "BRK.B", "resolution_source": "manual"},
                {"id": 2, "cusip": "88579Y101", "ticker": "MMM", "resolution_source": "manual"},
            ]
        ),
        insert(PositionSnapshot).values(
            [
                {
                    "filer_id": 1,
                    "period_of_report": date(2026, 6, 30),
                    "security_id": security_id,
                    "shares": Decimal(100),
                    "value_usd": Decimal(1_000),
                    "weight_pct": Decimal(weight),
                    "source_filing_id": 1,
                    "suspect": False,
                }
                for security_id, weight in ((1, "50"), (2, "0.5"))
            ]
        ),
    )


def _company_tickers() -> None:
    respx.get(COMPANY_TICKERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "0": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
                "1": {"cik_str": 66740, "ticker": "MMM", "title": "3M CO"},
            },
        )
    )


@respx.mock
def test_the_held_universe_is_issuers_over_the_weight_floor(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    cli_settings: Settings,
    migrated_engine: AsyncEngine,
) -> None:
    held = cli_settings.model_copy(update={"form4_universe": "held"})
    monkeypatch.setattr("app.cli.get_settings", lambda: held)
    _held(migrated_engine)
    _company_tickers()
    day = date(2026, 10, 8)
    respx.get(quarter_listing_url(2026, 4)).mock(
        return_value=httpx.Response(200, json=LISTING_2026Q4)
    )
    respx.get(daily_index_url(day)).mock(
        return_value=httpx.Response(
            200,
            content=_index(
                day,
                ("4", "BERKSHIRE HATHAWAY INC", BERKSHIRE, "0001067983-26-000001"),
                ("4", "3M CO", THREE_M, THREE_M_4),
            ),
        )
    )

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    assert result.exit_code == 0, result.output
    # BRK.B held as SEC's BRK-B; 3M under the 1% floor.
    assert [row[:2] for row in _queue(migrated_engine)] == [("0001067983-26-000001", BERKSHIRE)]
    assert "1 issuer at least 1% of a portfolio in the four quarters to 2026-06-30" in result.output
    # The map is kept, for the loader to link issuers back to securities.
    assert _fetch(
        migrated_engine,
        select(SecCompanyTicker.ticker, SecCompanyTicker.cik).order_by(SecCompanyTicker.ticker),
    ) == [("BRK-B", BERKSHIRE), ("MMM", THREE_M)]


def test_the_held_universe_without_positions_fails_without_asking_edgar(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    """Crawling an empty universe would move the watermark over days it never
    looked for anything in. respx is not active, so a request would fail."""
    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08", "--universe", "held"])

    assert result.exit_code == 1
    assert "no positions are published" in result.stderr
    assert _watermark(migrated_engine) is None


def test_a_missing_universe_file_fails(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, cli_settings: Settings, tmp_path: Path
) -> None:
    missing = cli_settings.model_copy(update={"form4_sp500_path": tmp_path / "nope.csv"})
    monkeypatch.setattr("app.cli.get_settings", lambda: missing)

    result = runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    assert result.exit_code == 1
    assert "cannot read the S&P 500 list" in result.stderr


def test_the_shipped_sp500_file_is_readable() -> None:
    from app.ingestion.insider_discovery import sp500_universe

    universe = sp500_universe(None)

    assert len(universe.ciks) > 490
    assert "0000320193" in universe.ciks  # Apple


# --- arguments --------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--date", "2026-10-15"],
        ["--from", "2026-10-13", "--to", "2026-10-15"],
        ["--date", "2026-10-08", "--catch-up"],
        ["--to", "2026-10-08"],
        ["--from", "2026-10-09", "--to", "2026-10-08"],
        ["--universe", "everything"],
    ],
)
def test_days_that_cannot_be_crawled_are_refused(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(app, ["crawl-form4", *args])

    assert result.exit_code == 2, result.output


# --- the queue is shared ----------------------------------------------------------


@respx.mock
def test_backfill_leaves_form_4s_in_the_queue_alone(
    runner: CliRunner, migrated_engine: AsyncEngine, settings: Settings
) -> None:
    """The 13F loader would fail every one of them."""
    _edgar()
    runner.invoke(app, ["crawl-form4", "--date", "2026-10-08"])

    async def plan() -> int:
        async with create_session_factory(migrated_engine)() as session:
            planned = await plan_backfill(session, slug=None, since=None, force=False, limit=None)
        return len(planned.work)

    assert asyncio.run(plan()) == 0
    assert len(_queue(migrated_engine)) == 2
