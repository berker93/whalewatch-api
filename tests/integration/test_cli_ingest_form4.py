"""``ingest-form4``: the queue ``crawl-form4`` fills, drained into the insider tables.

Against a real Postgres, with EDGAR faked by respx. Three queued filings: the
Kinetik joint filing, the NVIDIA 4/A, and (filed a month earlier, and queued
after it) the Form 4 that 4/A amends, so the run loads an amendment before its
original. What is asserted is what a run leaves behind: queue rows, filing
rows, lines, the archive, and the ``ingestion_run``.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import date
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
from app.db.models import Filing, IngestionRun, InsiderTransaction, PendingFiling
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.form4_documents import header_url
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "form4"

KINETIK: Final = "0001692787"
NVIDIA: Final = "0001045810"

KINETIK_4: Final = "0000905148-26-004450"
NVIDIA_4A: Final = "0002152188-26-000007"
NVIDIA_4: Final = "0002152188-26-000003"

_ORIGINAL_XML: Final = (
    (FIXTURES / "amendment-nvidia.xml")
    .read_bytes()
    .replace(b"<documentType>4/A</documentType>", b"<documentType>4</documentType>")
    .replace(b"<dateOfOriginalSubmission>2026-09-11</dateOfOriginalSubmission>", b"")
)

#: accession -> (queue CIK, filing date, form, document name, document, acceptance)
_FILINGS: Final = {
    KINETIK_4: (
        KINETIK,
        "2026-10-13",
        "4",
        "wk-form4_1.xml",
        (FIXTURES / "joint-filing-kinetik.xml").read_bytes(),
        "20261013163000",
    ),
    NVIDIA_4A: (
        NVIDIA,
        "2026-10-13",
        "4/A",
        "wk-form4a_1791925767.xml",
        (FIXTURES / "amendment-nvidia.xml").read_bytes(),
        "20261013170929",
    ),
    NVIDIA_4: (NVIDIA, "2026-09-11", "4", "form4.xml", _ORIGINAL_XML, "20260911180200"),
}


def _listing(accession: str, *names: str) -> dict[str, Any]:
    return {
        "directory": {
            "item": [
                {"name": f"{accession}-index-headers.html", "type": "text.gif", "size": ""},
                {"name": f"{accession}.txt", "type": "text.gif", "size": ""},
                *({"name": name, "type": "text.gif", "size": "4189"} for name in names),
            ],
            "name": f"/Archives/edgar/data/x/{accession.replace('-', '')}",
        }
    }


def _edgar(*, broken: str | None = None) -> list[respx.Route]:
    """Each filing's listing, document and header. ``broken``'s document is not
    an ownership document's XML."""
    routes = []
    for accession, (cik, _, _, name, document, accepted) in _FILINGS.items():
        index_url = EdgarClient.filing_index_url(cik, accession)
        base = index_url.rsplit("/", 1)[0]
        body = b"<ownershipDocument><documentType>4" if accession == broken else document
        routes += [
            respx.get(index_url).mock(
                return_value=httpx.Response(200, json=_listing(accession, name))
            ),
            respx.get(f"{base}/{name}").mock(return_value=httpx.Response(200, content=body)),
            respx.get(header_url(cik, accession)).mock(
                return_value=httpx.Response(
                    200,
                    content=f"<SEC-HEADER>\n<ACCEPTANCE-DATETIME>{accepted}\n".encode(),
                )
            ),
        ]
    return routes


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """The API's cache, which a run that loads something drops: the insider
    feed reads these tables live."""
    fake = FakeRedis()
    monkeypatch.setattr("app.cli.create_redis", lambda settings: fake)
    return fake


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.ingestion.edgar.client.get_edgar_limiter",
        lambda rate_per_second: AsyncTokenBucket(10_000.0),
    )
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def queued(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    _execute(
        migrated_engine,
        insert(PendingFiling).values(
            [
                {
                    "accession_no": accession,
                    "cik": cik,
                    "form_type": form,
                    "filing_date": date.fromisoformat(filed),
                    "report_date": None,
                }
                for accession, (cik, filed, form, *_) in _FILINGS.items()
            ]
        ),
    )
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE ingestion_run, pending_filing, insider_transaction, reporting_owner, "
            "insider, filing, security, sec_company_ticker RESTART IDENTITY CASCADE"
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
        select(PendingFiling.accession_no, PendingFiling.status, PendingFiling.attempts).order_by(
            PendingFiling.accession_no
        ),
    )


def _lines(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(InsiderTransaction.id, Filing.accession_no, InsiderTransaction.line_number)
        .join(Filing, Filing.id == InsiderTransaction.filing_id)
        .order_by(Filing.accession_no, InsiderTransaction.line_number),
    )


def _run(engine: AsyncEngine) -> tuple[Any, ...]:
    [row] = _fetch(
        engine,
        select(
            IngestionRun.status,
            IngestionRun.items_seen,
            IngestionRun.items_written,
            IngestionRun.metrics,
            IngestionRun.error,
        )
        .where(IngestionRun.job_name == "ingest-form4")
        # The id is a random UUID: the latest run is the one started last.
        .order_by(IngestionRun.started_at.desc())
        .limit(1),
    )
    return row


# --- a run -------------------------------------------------------------------------


@respx.mock
def test_the_queue_is_loaded_and_marked_done(
    runner: CliRunner, migrated_engine: AsyncEngine, settings: Settings, redis: FakeRedis
) -> None:
    _edgar()
    asyncio.run(redis.set("ww:v1:insiders.list_insider_transactions:x", "{}"))

    result = runner.invoke(app, ["ingest-form4", "--concurrency", "1"])

    assert result.exit_code == 0, result.output
    assert _queue(migrated_engine) == [
        (KINETIK_4, "done", 0),
        (NVIDIA_4, "done", 0),
        (NVIDIA_4A, "done", 0),
    ]
    # The original, loaded first because it was filed first, has no lines
    # left: the 4/A replaced them.
    assert [(accession, line) for _, accession, line in _lines(migrated_engine)] == [
        (KINETIK_4, 1),
        (KINETIK_4, 2),
        (KINETIK_4, 3),
        (KINETIK_4, 4),
        (NVIDIA_4A, 1),
    ]
    filings = _fetch(
        migrated_engine,
        select(Filing.accession_no, Filing.cik, Filing.form_type, Filing.raw_key).order_by(
            Filing.accession_no
        ),
    )
    assert filings == [
        (KINETIK_4, KINETIK, "4", f"raw/form4/{KINETIK}/{KINETIK_4}/"),
        (NVIDIA_4, NVIDIA, "4", f"raw/form4/{NVIDIA}/{NVIDIA_4}/"),
        (NVIDIA_4A, NVIDIA, "4/A", f"raw/form4/{NVIDIA}/{NVIDIA_4A}/"),
    ]
    archived = settings.raw_store_local_root / "raw" / "form4" / NVIDIA / NVIDIA_4A
    assert sorted(path.name for path in archived.iterdir()) == [
        f"{NVIDIA_4A}.hdr.sgml",
        "index.json",
        "wk-form4a_1791925767.xml",
    ]

    assert "ingest-form4  3 Form 4s, 3 to ingest · 1 at a time" in result.output
    assert "4/A  NVIDIA CORP · Parker Nicholas P. · 1 line · NVDA" in result.output
    assert "ingest-form4  done: loaded 3 · skipped 0 · failed 0" in result.output
    # The original's line was written, and then replaced by the 4/A's.
    assert "6 written, 1 replaced by 4/As" in result.output

    status, seen, written, metrics, error = _run(migrated_engine)
    assert (status, seen, written, error) == ("success", 3, 3, None)
    assert metrics["lines_written"] == 6
    assert metrics["lines_superseded"] == 1
    assert metrics["security_links"] == {"unlisted": 3}
    assert metrics["amendments"] == {"linked": 1}
    assert metrics["cache_invalidated"] == 1
    assert asyncio.run(redis.get("ww:v1:insiders.list_insider_transactions:x")) is None


@respx.mock
def test_a_second_run_asks_edgar_nothing_and_changes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    routes = _edgar()
    assert runner.invoke(app, ["ingest-form4"]).exit_code == 0
    lines = _lines(migrated_engine)
    calls = sum(route.call_count for route in routes)

    result = runner.invoke(app, ["ingest-form4"])

    assert result.exit_code == 0, result.output
    assert sum(route.call_count for route in routes) == calls
    assert _lines(migrated_engine) == lines
    assert "3 Form 4s, 3 already loaded" in result.output
    # Nothing loaded, so the cache is left alone.
    assert "cache_invalidated" not in _run(migrated_engine)[3]


@respx.mock
def test_force_reloads_from_the_archive_without_edgar(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    routes = _edgar()
    assert runner.invoke(app, ["ingest-form4"]).exit_code == 0
    calls = sum(route.call_count for route in routes)

    result = runner.invoke(app, ["ingest-form4", "--force", "--concurrency", "1"])

    assert result.exit_code == 0, result.output
    assert sum(route.call_count for route in routes) == calls
    assert "3 to reload from the raw store" in result.output
    # Replaced, so new ids; the same lines, and still none for the original.
    assert [(accession, line) for _, accession, line in _lines(migrated_engine)] == [
        (KINETIK_4, 1),
        (KINETIK_4, 2),
        (KINETIK_4, 3),
        (KINETIK_4, 4),
        (NVIDIA_4A, 1),
    ]


@respx.mock
def test_a_filing_that_fails_is_recorded_and_the_run_carries_on(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _edgar(broken=KINETIK_4)

    result = runner.invoke(app, ["ingest-form4"])

    assert result.exit_code == 1, result.output
    assert _queue(migrated_engine) == [
        (KINETIK_4, "failed", 1),
        (NVIDIA_4, "done", 0),
        (NVIDIA_4A, "done", 0),
    ]
    assert f"failed      {KINETIK_4}  FilingParseError" in result.output
    status, seen, written, _, error = _run(migrated_engine)
    assert (status, seen, written) == ("partial", 3, 2)
    assert error is not None and KINETIK_4 in error


@respx.mock
def test_limit_holds_back_the_rest(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _edgar()

    result = runner.invoke(app, ["ingest-form4", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "1 held back by --limit" not in result.output
    assert "2 held back by --limit" in result.output
    # The oldest filing date first: the original, before the 4/A that amends it.
    assert [row[:2] for row in _queue(migrated_engine)] == [
        (KINETIK_4, "pending"),
        (NVIDIA_4, "done"),
        (NVIDIA_4A, "pending"),
    ]
