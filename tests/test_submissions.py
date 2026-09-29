"""Tests for reading one submission out of EDGAR's per-filer index.

Most of this file is about one field. :attr:`Submission.filed_at` decides
:attr:`~app.db.models.filing.Filing.value_multiplier`, which decides whether a
13F's numbers are thousands of dollars or whole dollars — so a timestamp read
loosely here is a 1000x error on a whole portfolio, and one that looks like data
because every filer in the affected quarter is wrong by the same factor. Hence
the tests that a naive or missing ``acceptanceDateTime`` raises rather than
being guessed at, and the one that pins the cutover behaviour end to end.

The other half is paging. ``filings.recent`` is capped at a thousand
submissions, which for an old filer is about eight years, and a lookup that
stopped there would report "no such filing" for documents sitting in the
archive. A listing that stopped there could miss every 13F a manager ever
filed, if the manager also files enough Form 4s.

``httpx.MockTransport`` or ``respx`` answers every request, so nothing here
reaches data.sec.gov.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.submissions import (
    FilingRef,
    SubmissionMalformedError,
    SubmissionNotFoundError,
    fetch_all_filings,
    filter_forms,
    find_submission,
)
from app.ingestion.normalisation import resolve_value_multiplier
from tests.conftest import make_settings

CIK: Final = "0001067983"
ACCESSION: Final = "0001067983-24-000011"
OTHER: Final = "0001067983-23-000007"

_RECENT_PATH: Final = f"/submissions/CIK{CIK}.json"
_OLDER_PAGE: Final = f"CIK{CIK}-submissions-001.json"
_OLDER_PATH: Final = f"/submissions/{_OLDER_PAGE}"

_UNTHROTTLED: Final = 10_000.0


def _columns(*rows: dict[str, Any]) -> dict[str, list[Any]]:
    """Rows as EDGAR publishes them: one parallel array per field, not objects."""
    names = {name for row in rows for name in row}
    return {name: [row.get(name, "") for row in rows] for name in sorted(names)}


def _row(
    accession_no: str = ACCESSION,
    *,
    form: str = "13F-HR",
    accepted: str = "2024-05-15T20:05:04.000Z",
    report_date: str = "2024-03-31",
    primary_document: str = "primary_doc.xml",
) -> dict[str, Any]:
    return {
        "accessionNumber": accession_no,
        "form": form,
        "acceptanceDateTime": accepted,
        "filingDate": accepted[:10],
        "reportDate": report_date,
        "primaryDocument": primary_document,
    }


def _submissions(
    *rows: dict[str, Any], older: list[str] | None = None, name: str = "BERKSHIRE HATHAWAY INC"
) -> bytes:
    return json.dumps(
        {
            "cik": str(int(CIK)),
            "name": name,
            "filings": {
                "recent": _columns(*rows),
                "files": [{"name": page, "filingCount": 1000} for page in older or []],
            },
        }
    ).encode()


def _client(settings: Settings, documents: dict[str, bytes], fetched: list[str]) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.path)
        body = documents.get(request.url.path)
        return httpx.Response(404) if body is None else httpx.Response(200, content=body)

    return EdgarClient(
        settings,
        transport=httpx.MockTransport(handler),
        limiter=AsyncTokenBucket(_UNTHROTTLED),
    )


async def _find(
    settings: Settings, documents: dict[str, bytes], *, accession_no: str = ACCESSION
) -> Any:
    async with _client(settings, documents, []) as edgar:
        return await find_submission(edgar, cik=CIK, accession_no=accession_no)


# --- the ordinary lookup -----------------------------------------------------


async def test_the_row_is_read_out_of_the_parallel_column_arrays(
    settings: Settings,
) -> None:
    """EDGAR publishes a column per field rather than an object per filing, so a
    row is an index into all of them at once — and the test that matters is that
    the *second* row's fields do not come back mixed with the first's."""
    submission = await _find(
        settings,
        {
            _RECENT_PATH: _submissions(
                _row(OTHER, form="13F-HR/A", report_date="2023-09-30"),
                _row(),
            )
        },
    )

    assert submission.accession_no == ACCESSION
    assert submission.cik == CIK
    assert submission.entity_name == "BERKSHIRE HATHAWAY INC"
    assert submission.form_type == "13F-HR"
    assert submission.period_of_report == date(2024, 3, 31)
    assert submission.primary_document == "primary_doc.xml"


async def test_the_cik_comes_back_padded_however_it_was_asked_for(
    settings: Settings,
) -> None:
    """``filing.cik`` is ``CHAR(10)``, so an unpadded value compares equal to
    nothing. Padding here is what lets ``--cik 1067983`` work."""
    async with _client(settings, {_RECENT_PATH: _submissions(_row())}, []) as edgar:
        submission = await find_submission(edgar, cik="1067983", accession_no=ACCESSION)

    assert submission.cik == CIK


async def test_a_blank_report_date_is_absent_rather_than_a_failure(
    settings: Settings,
) -> None:
    """EDGAR writes ``""`` where a field does not apply. A Form 4 describes a day
    and has no period, and that is not a malformed document."""
    submission = await _find(settings, {_RECENT_PATH: _submissions(_row(report_date=""))})

    assert submission.period_of_report is None


async def test_a_filing_under_a_different_cik_is_not_found(settings: Settings) -> None:
    with pytest.raises(SubmissionNotFoundError):
        await _find(settings, {_RECENT_PATH: _submissions(_row(OTHER))})


# --- paging ------------------------------------------------------------------


async def test_a_filing_older_than_the_recent_thousand_is_found_on_a_later_page(
    settings: Settings,
) -> None:
    """``filings.recent`` holds a thousand submissions. For a filer of Berkshire's
    age that is about eight years, so every 13F before then is on an overflow
    page — and a lookup that stopped at ``recent`` would call a filing sitting in
    the archive missing."""
    submission = await _find(
        settings,
        {
            _RECENT_PATH: _submissions(_row(OTHER), older=[_OLDER_PAGE]),
            _OLDER_PATH: json.dumps(_columns(_row())).encode(),
        },
    )

    assert submission.accession_no == ACCESSION


async def test_the_overflow_pages_are_not_fetched_when_recent_has_the_answer(
    settings: Settings,
) -> None:
    """Each page is a request against a host that counts them, so they are walked
    lazily. The common case is one request."""
    fetched: list[str] = []
    async with _client(
        settings,
        {
            _RECENT_PATH: _submissions(_row(), older=[_OLDER_PAGE]),
            _OLDER_PATH: json.dumps(_columns(_row(OTHER))).encode(),
        },
        fetched,
    ) as edgar:
        await find_submission(edgar, cik=CIK, accession_no=ACCESSION)

    assert fetched == [_RECENT_PATH]


async def test_a_filing_on_no_page_at_all_is_not_found(settings: Settings) -> None:
    with pytest.raises(SubmissionNotFoundError, match=ACCESSION):
        await _find(
            settings,
            {
                _RECENT_PATH: _submissions(_row(OTHER), older=[_OLDER_PAGE]),
                _OLDER_PATH: json.dumps(_columns(_row(OTHER))).encode(),
            },
        )


# --- the timestamp that decides the units ------------------------------------


async def test_the_acceptance_timestamp_is_read_as_an_instant_with_its_zone(
    settings: Settings,
) -> None:
    submission = await _find(settings, {_RECENT_PATH: _submissions(_row())})

    assert submission.filed_at == datetime(2024, 5, 15, 20, 5, 4, tzinfo=UTC)


async def test_a_filing_accepted_the_evening_before_the_cutover_is_still_in_thousands(
    settings: Settings,
) -> None:
    """The three hours this field exists to get right.

    EDGAR accepts submissions until 22:00 Eastern, so 20:00 ET on 2 January 2023
    is 01:00 UTC on the 3rd. ``filingDate`` — a date in New York — would put this
    filing on the near side of the units cutover and a UTC date on the far side,
    and the far side reads thousands of dollars as dollars.
    """
    submission = await _find(
        settings, {_RECENT_PATH: _submissions(_row(accepted="2023-01-03T01:00:00.000Z"))}
    )

    assert resolve_value_multiplier(submission.filed_at) == 1000


@pytest.mark.parametrize(
    "accepted",
    [
        # Absent: EDGAR writes an empty string where a field does not apply.
        "",
        # Present and naive. The two available readings of this — Eastern, or
        # UTC — straddle the cutover, which is exactly the guess not worth making.
        "2023-01-02T20:00:00",
        "not a timestamp",
    ],
)
async def test_an_unusable_acceptance_timestamp_fails_rather_than_being_guessed(
    settings: Settings, accepted: str
) -> None:
    """A hard failure on one filing, against a silent 1000x error on its values.

    The whole filing is lost either way in the short term; only one of the two
    outcomes tells anybody about it.
    """
    with pytest.raises(SubmissionMalformedError):
        await _find(settings, {_RECENT_PATH: _submissions(_row(accepted=accepted))})


async def test_a_malformed_report_date_fails_rather_than_being_dropped(
    settings: Settings,
) -> None:
    """Absent is fine, unreadable is not — the rule the parsers follow. Silently
    nulling a value that is *there* leaves our output and EDGAR's disagreeing
    with nothing recording that they do."""
    with pytest.raises(SubmissionMalformedError, match="reportDate"):
        await _find(settings, {_RECENT_PATH: _submissions(_row(report_date="Q1 2024"))})


# --- documents that are not submissions documents ----------------------------


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"cik": "1067983"}', b'{"filings": []}'],
)
async def test_a_document_that_is_not_a_submissions_index_is_an_error(
    settings: Settings, body: bytes
) -> None:
    """Distinct from "not found", because the fix is different: this one means
    EDGAR changed the feed or served a truncated body, and no amount of checking
    the accession number will help."""
    with pytest.raises(SubmissionMalformedError):
        await _find(settings, {_RECENT_PATH: body})


# --- listing every filing ----------------------------------------------------

_RECENT_URL: Final = f"https://data.sec.gov{_RECENT_PATH}"
_OLDER_URL: Final = f"https://data.sec.gov{_OLDER_PATH}"


def _listing(*rows: dict[str, Any]) -> dict[str, list[Any]]:
    return _columns(*rows)


#: A manager whose recent thousand are all insider and event filings, so every
#: 13F it ever filed is on the overflow page. The case a recent-only listing
#: gets wrong without any error at all.
_BUSY_RECENT: Final = _submissions(
    _row("0001067983-24-000100", form="4", accepted="2024-06-03T21:01:00.000Z", report_date=""),
    {
        **_row("0001067983-24-000090", form="8-K", accepted="2024-02-26T16:30:00.000Z"),
        "reportDate": "2024-02-24",
        "isXBRL": 1,
        "primaryDocument": "brka-20240224.htm",
    },
    older=[_OLDER_PAGE],
)
_BUSY_OLDER: Final = json.dumps(
    _listing(
        _row(
            "0001067983-14-000007",
            form="13F-HR/A",
            accepted="2014-03-05T17:00:00.000Z",
            report_date="2013-12-31",
        ),
        _row(
            "0001067983-14-000003",
            form="13F-HR",
            accepted="2014-02-14T16:10:00.000Z",
            report_date="2013-12-31",
        ),
        _row("0001067983-14-000004", form="4", accepted="2014-02-14T16:10:00.000Z", report_date=""),
    )
).encode()


def _respx_client(settings: Settings) -> EdgarClient:
    """httpx's real transport, which respx intercepts."""
    return EdgarClient(settings, limiter=AsyncTokenBucket(_UNTHROTTLED))


def _mock_busy_filer() -> tuple[respx.Route, respx.Route]:
    recent = respx.get(_RECENT_URL).mock(return_value=httpx.Response(200, content=_BUSY_RECENT))
    older = respx.get(_OLDER_URL).mock(return_value=httpx.Response(200, content=_BUSY_OLDER))
    return recent, older


@respx.mock
async def test_every_page_is_merged_and_sorted_by_filing_date(settings: Settings) -> None:
    recent, older = _mock_busy_filer()

    async with _respx_client(settings) as edgar:
        filings = await fetch_all_filings(edgar, cik="1067983")

    assert recent.call_count == 1
    assert older.call_count == 1
    assert filings == [
        FilingRef(
            accession_no="0001067983-14-000003",
            form_type="13F-HR",
            filing_date=date(2014, 2, 14),
            report_date=date(2013, 12, 31),
            primary_document="primary_doc.xml",
            is_xbrl=False,
        ),
        # Same day as the 13F-HR above: accession number breaks the tie.
        FilingRef(
            accession_no="0001067983-14-000004",
            form_type="4",
            filing_date=date(2014, 2, 14),
            report_date=None,
            primary_document="primary_doc.xml",
            is_xbrl=False,
        ),
        FilingRef(
            accession_no="0001067983-14-000007",
            form_type="13F-HR/A",
            filing_date=date(2014, 3, 5),
            report_date=date(2013, 12, 31),
            primary_document="primary_doc.xml",
            is_xbrl=False,
        ),
        FilingRef(
            accession_no="0001067983-24-000090",
            form_type="8-K",
            filing_date=date(2024, 2, 26),
            report_date=date(2024, 2, 24),
            primary_document="brka-20240224.htm",
            is_xbrl=True,
        ),
        FilingRef(
            accession_no="0001067983-24-000100",
            form_type="4",
            filing_date=date(2024, 6, 3),
            report_date=None,
            primary_document="primary_doc.xml",
            is_xbrl=False,
        ),
    ]


@respx.mock
async def test_the_13fs_are_found_when_recent_has_none(settings: Settings) -> None:
    """The ticket's case, end to end: filter the merged listing by form."""
    _mock_busy_filer()

    async with _respx_client(settings) as edgar:
        thirteen_fs = filter_forms(await fetch_all_filings(edgar, cik=CIK), "13F-HR")

    assert [f.accession_no for f in thirteen_fs] == [
        "0001067983-14-000003",
        "0001067983-14-000007",
    ]


@respx.mock
async def test_a_second_run_is_served_from_the_disk_cache(tmp_path: Path) -> None:
    """Both pages, second time round, without a request."""
    recent, older = _mock_busy_filer()
    cached = make_settings(edgar_cache_dir=tmp_path)

    async with _respx_client(cached) as edgar:
        first = await fetch_all_filings(edgar, cik=CIK)
    async with _respx_client(cached) as edgar:
        second = await fetch_all_filings(edgar, cik=CIK)

    assert second == first
    assert (recent.call_count, older.call_count) == (1, 1)
    assert (tmp_path / "data.sec.gov" / "submissions" / _OLDER_PAGE).read_bytes() == _BUSY_OLDER


@pytest.mark.parametrize(
    ("form_type", "prefix", "matches"),
    [
        ("13F-HR", "13F-HR", True),
        ("13F-HR/A", "13F-HR", True),
        ("13F-NT", "13F-HR", False),
        ("13F-NT", "13F", True),
        ("4/A", "4", True),
        # Shares leading characters with "4" but is a different form.
        ("424B2", "4", False),
        ("40-F", "4", False),
        ("13F-HR", "13F-HR/A", False),
    ],
)
def test_form_types_match_by_prefix_at_a_form_name_boundary(
    form_type: str, prefix: str, matches: bool
) -> None:
    filing = FilingRef(
        accession_no=ACCESSION,
        form_type=form_type,
        filing_date=date(2024, 5, 15),
        report_date=None,
        primary_document=None,
        is_xbrl=False,
    )
    assert filter_forms([filing], prefix) == ([filing] if matches else [])


async def test_a_row_without_a_filing_date_cannot_be_placed_in_order(
    settings: Settings,
) -> None:
    row = _row()
    row["filingDate"] = ""
    async with _client(settings, {_RECENT_PATH: _submissions(row)}, []) as edgar:
        with pytest.raises(SubmissionMalformedError, match="filingDate"):
            await fetch_all_filings(edgar, cik=CIK)
