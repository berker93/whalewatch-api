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
archive.

``httpx.MockTransport`` answers every request, so nothing here reaches
data.sec.gov.
"""

import json
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx
import pytest

from app.core.config import Settings
from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.submissions import (
    SubmissionMalformedError,
    SubmissionNotFoundError,
    find_submission,
)
from app.ingestion.normalisation import resolve_value_multiplier

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
