"""``verify-investors``: every CIK in the list, checked against EDGAR.

The failures that matter here are the silent ones this command exists to make
loud — a typo'd CIK that names nobody, a CIK that names a company rather than
its filing manager, a filer that stopped filing — and the one it must not make
loud: a predecessor CIK that stopped filing because it was succeeded, which is
what a predecessor is.

``httpx.MockTransport`` answers every request, so nothing here reaches
data.sec.gov.
"""

import csv
import io
import json
import logging
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.submissions import (
    FilerNotFoundError,
    SubmissionMalformedError,
    list_filings,
)
from app.ingestion.investors import parse_investors
from app.ingestion.verify_investors import (
    Flag,
    name_similarity,
    stale_cutoff,
    verify_investors,
)
from tests.conftest import make_settings

TODAY: Final = date(2026, 9, 30)
_UNTHROTTLED: Final = 10_000.0

_LIST: Final = """
- slug: berkshire-hathaway
  display_name: Berkshire Hathaway
  manager_name: Warren Buffett
  category: value
  country: US
  ciks: [1067983]
- slug: pershing-square
  display_name: Pershing Square Capital Management
  manager_name: Bill Ackman
  category: activist
  country: US
  ciks: [1336528, 2026053]
"""


def _columns(*rows: tuple[str, str, str]) -> dict[str, list[str]]:
    """``(accession, form, reportDate)`` rows as EDGAR publishes them: one
    parallel array per field."""
    return {
        "accessionNumber": [row[0] for row in rows],
        "form": [row[1] for row in rows],
        "reportDate": [row[2] for row in rows],
        "filingDate": ["2026-01-01" for _ in rows],
    }


def _quarterly(cik: int, *periods: str, form: str = "13F-HR") -> list[tuple[str, str, str]]:
    return [(f"{cik:010d}-{i:02d}-000001", form, period) for i, period in enumerate(periods)]


def _submissions(
    cik: int,
    name: str,
    rows: list[tuple[str, str, str]],
    *,
    older: list[str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "cik": str(cik),
            "name": name,
            "filings": {
                "recent": _columns(*rows),
                "files": [{"name": page} for page in older or []],
            },
        }
    ).encode()


def _path(cik: int) -> str:
    return f"/submissions/CIK{cik:010d}.json"


def _client(settings: Settings, documents: dict[str, bytes]) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = documents.get(request.url.path)
        return httpx.Response(404) if body is None else httpx.Response(200, content=body)

    return EdgarClient(
        settings,
        transport=httpx.MockTransport(handler),
        limiter=AsyncTokenBucket(_UNTHROTTLED),
    )


#: Berkshire current; Pershing's predecessor stopped in 2024 (expected) and its
#: successor is current but EDGAR calls it something unlike our name.
_HEALTHY: Final = {
    _path(1067983): _submissions(
        1067983,
        "BERKSHIRE HATHAWAY INC",
        [("0001067983-26-000002", "4", ""), *_quarterly(1067983, "2026-06-30", "2013-03-31")],
    ),
    _path(1336528): _submissions(
        1336528, "Pershing Square Capital Management, L.P.", _quarterly(1336528, "2024-12-31")
    ),
    _path(2026053): _submissions(
        2026053, "PSH Holdco 2025 LLC", _quarterly(2026053, "2025-03-31", "2026-06-30")
    ),
}


# --- the rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "cutoff"),
    [
        # Q2 2026 is due (Aug 14 has passed), so Q1 2026 is the oldest that passes.
        (date(2026, 9, 30), date(2026, 3, 31)),
        # Q2 not yet due on Aug 1: Q1 is the one expected, Q4 2025 still passes.
        (date(2026, 8, 1), date(2025, 12, 31)),
        # The 45th day after the quarter closes the window.
        (date(2026, 8, 14), date(2026, 3, 31)),
        (date(2026, 8, 13), date(2025, 12, 31)),
        # Across a year boundary: Q4 2025 is due on Feb 14.
        (date(2026, 2, 20), date(2025, 9, 30)),
        (date(2026, 2, 10), date(2025, 6, 30)),
    ],
)
def test_the_stale_cutoff_allows_one_missed_quarter_after_the_filing_window(
    today: date, cutoff: date
) -> None:
    assert stale_cutoff(today) == cutoff


@pytest.mark.parametrize(
    ("edgar", "ours"),
    [
        ("BERKSHIRE HATHAWAY INC", "Berkshire Hathaway"),
        ("Pershing Square Capital Management, L.P.", "Pershing Square Capital Management"),
        ("DUQUESNE FAMILY OFFICE LLC", "Duquesne Family Office"),
    ],
)
def test_legal_names_match_their_brands(edgar: str, ours: str) -> None:
    assert name_similarity(edgar, ours) == pytest.approx(1.0)


def test_an_unrelated_name_does_not_match() -> None:
    assert name_similarity("APPLE INC", "Berkshire Hathaway", "Warren Buffett") < 0.6


def test_the_manager_name_counts_as_one_of_ours() -> None:
    """A family office often files under a name closer to its founder's."""
    assert name_similarity(
        "STANLEY DRUCKENMILLER", "Duquesne Family Office", "Stanley Druckenmiller"
    ) == pytest.approx(1.0)


# --- reading the index -------------------------------------------------------


async def test_list_filings_zips_the_parallel_arrays_and_walks_every_page(
    settings: Settings,
) -> None:
    """``recent`` holds a thousand submissions; the earliest 13F of an old
    filer is on a page after it, and a listing that stopped at ``recent``
    would report the wrong first period."""
    page = "CIK0001067983-submissions-001.json"
    documents = {
        _path(1067983): _submissions(
            1067983,
            "BERKSHIRE HATHAWAY INC",
            [("A-1", "13F-HR", "2026-06-30"), ("A-2", "4", "")],
            older=[page],
        ),
        f"/submissions/{page}": json.dumps(_columns(("A-0", "13F-HR", "1998-12-31"))).encode(),
    }
    async with _client(settings, documents) as edgar:
        history = await list_filings(edgar, cik="1067983")

    assert history.cik == "0001067983"
    assert history.entity_name == "BERKSHIRE HATHAWAY INC"
    # One filing date throughout, so this is the accession-number tie-break.
    assert [(f.accession_no, f.form_type, f.report_date) for f in history.filings] == [
        ("A-0", "13F-HR", date(1998, 12, 31)),
        ("A-1", "13F-HR", date(2026, 6, 30)),
        ("A-2", "4", None),
    ]


async def test_list_filings_names_a_cik_edgar_does_not_have(settings: Settings) -> None:
    async with _client(settings, {}) as edgar:
        with pytest.raises(FilerNotFoundError, match="0001067983"):
            await list_filings(edgar, cik="1067983")


async def test_ragged_column_arrays_are_malformed_not_misaligned(settings: Settings) -> None:
    """Zipping arrays of different lengths pairs one filing's form with
    another's period. That is refused rather than read."""
    columns = _columns(("A-1", "13F-HR", "2026-06-30"), ("A-2", "13F-HR", "2026-03-31"))
    columns["reportDate"].pop()
    body = json.dumps({"cik": "1067983", "name": "X", "filings": {"recent": columns}}).encode()
    async with _client(settings, {_path(1067983): body}) as edgar:
        with pytest.raises(SubmissionMalformedError, match="different lengths"):
            await list_filings(edgar, cik="1067983")


# --- the checks --------------------------------------------------------------


async def _verify(settings: Settings, documents: dict[str, bytes], text: str = _LIST) -> Any:
    async with _client(settings, documents) as edgar:
        return await verify_investors(edgar, parse_investors(text), today=TODAY)


async def test_a_healthy_list_reports_each_cik_in_list_order(settings: Settings) -> None:
    checks = await _verify(settings, _HEALTHY)

    assert [(c.slug, c.cik, c.current) for c in checks] == [
        ("berkshire-hathaway", "0001067983", True),
        ("pershing-square", "0001336528", False),
        ("pershing-square", "0002026053", True),
    ]
    berkshire = checks[0]
    assert berkshire.edgar_name == "BERKSHIRE HATHAWAY INC"
    assert berkshire.thirteen_f_count == 2, "the Form 4 is not a 13F"
    assert berkshire.earliest_period == date(2013, 3, 31)
    assert berkshire.latest_period == date(2026, 6, 30)
    assert berkshire.status == "ok"
    assert not any(check.failures for check in checks)


async def test_a_predecessor_that_stopped_filing_is_only_a_warning(settings: Settings) -> None:
    predecessor = (await _verify(settings, _HEALTHY))[1]

    assert predecessor.failures == ()
    assert predecessor.warnings == (Flag.STALE,)


async def test_a_name_unlike_ours_is_a_warning_not_a_failure(settings: Settings) -> None:
    successor = (await _verify(settings, _HEALTHY))[2]

    assert successor.failures == ()
    assert successor.warnings == (Flag.NAME_MISMATCH,)
    assert successor.name_similarity is not None and successor.name_similarity < 0.6


async def test_a_cik_edgar_does_not_have_fails(settings: Settings) -> None:
    documents = {k: v for k, v in _HEALTHY.items() if k != _path(1067983)}

    berkshire = (await _verify(settings, documents))[0]

    assert berkshire.failures == (Flag.NOT_FOUND,)
    assert berkshire.status == "FAIL"


async def test_a_cik_that_never_filed_a_13f_hr_fails(settings: Settings) -> None:
    """Amendments and notices do not count: neither is a quarter of holdings."""
    documents = {
        **_HEALTHY,
        _path(1067983): _submissions(
            1067983,
            "BERKSHIRE HATHAWAY INC",
            [
                ("A-1", "13F-HR/A", "2026-06-30"),
                ("A-2", "13F-NT", "2026-03-31"),
                ("A-3", "10-K", "2025-12-31"),
            ],
        ),
    }

    berkshire = (await _verify(settings, documents))[0]

    assert berkshire.failures == (Flag.NO_13F,)
    assert berkshire.thirteen_f_count == 0


async def test_a_current_cik_that_stopped_filing_fails(settings: Settings) -> None:
    documents = {
        **_HEALTHY,
        _path(1067983): _submissions(
            1067983, "BERKSHIRE HATHAWAY INC", _quarterly(1067983, "2025-12-31")
        ),
    }

    berkshire = (await _verify(settings, documents))[0]

    assert berkshire.failures == (Flag.STALE,)


async def test_under_sum_every_cik_is_current(settings: Settings) -> None:
    """Separate books are all still being filed, so none of them is allowed to
    go quiet the way a predecessor is."""
    summed = _LIST.replace("ciks: [1336528, 2026053]", "ciks: [1336528, 2026053]\n  overlap: sum")

    checks = await _verify(settings, _HEALTHY, summed)

    assert checks[1].current
    assert checks[1].failures == (Flag.STALE,)


async def test_an_unreadable_index_fails_that_cik_and_carries_on(settings: Settings) -> None:
    documents = {**_HEALTHY, _path(1067983): b"[]"}

    checks = await _verify(settings, documents)

    assert checks[0].failures == (Flag.FETCH_FAILED,)
    assert checks[0].error is not None
    assert checks[2].status == "warn"


# --- the command -------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def edgar_documents(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bytes]]:
    """What the command's EDGAR client will be served. Tests edit it in place."""
    documents = dict(_HEALTHY)
    settings = make_settings()
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.EdgarClient", lambda s: _client(s, documents))
    yield documents
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def _invoke(runner: CliRunner, tmp_path: Path, *args: str) -> Any:
    path = tmp_path / "investors.yaml"
    path.write_text(_LIST, encoding="utf-8")
    return runner.invoke(
        app, ["verify-investors", "--file", str(path), "--as-of", TODAY.isoformat(), *args]
    )


def test_warnings_alone_exit_zero_and_print_the_table(
    runner: CliRunner, tmp_path: Path, edgar_documents: dict[str, bytes]
) -> None:
    result = _invoke(runner, tmp_path)

    assert result.exit_code == 0, result.output
    assert "as of 2026-09-30, stale before 2026-03-31" in result.stdout
    assert "BERKSHIRE HATHAWAY INC" in result.stdout
    assert "stale (predecessor)" in result.stdout
    assert "2 filers, 3 CIKs: 0 failed, 2 with warnings, 1 ok" in result.stdout


def test_a_hard_failure_exits_non_zero(
    runner: CliRunner, tmp_path: Path, edgar_documents: dict[str, bytes]
) -> None:
    del edgar_documents[_path(1067983)]

    result = _invoke(runner, tmp_path)

    assert result.exit_code == 1
    assert "NOT_FOUND" in result.stdout
    assert "1 failed" in result.stdout


def test_csv_to_stdout_replaces_the_table(
    runner: CliRunner, tmp_path: Path, edgar_documents: dict[str, bytes]
) -> None:
    result = _invoke(runner, tmp_path, "--csv", "-")

    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert result.exit_code == 0, result.output
    assert [row["cik"] for row in rows] == ["0001067983", "0001336528", "0002026053"]
    assert rows[0]["filings_13f_hr"] == "2"
    assert rows[0]["earliest_period"] == "2013-03-31"
    assert rows[1]["warnings"] == "stale"
    assert rows[1]["current"] == "false"


def test_csv_to_a_file_keeps_the_table(
    runner: CliRunner, tmp_path: Path, edgar_documents: dict[str, bytes]
) -> None:
    out = tmp_path / "report.csv"

    result = _invoke(runner, tmp_path, "--csv", str(out))

    assert result.exit_code == 0, result.output
    assert "verify-investors" in result.stdout
    assert len(list(csv.DictReader(out.open(encoding="utf-8")))) == 3
