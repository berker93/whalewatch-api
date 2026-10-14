"""EDGAR's daily index, its quarter listings, SEC's ticker map, and which days count as read.

Against fixtures cut from what sec.gov served on 2026-10-14
(``tests/fixtures/daily_index``): ``form.20241002.idx`` is the header and a
dozen lines of that day's form index, and ``2026-QTR4-index.json`` the
listing of 2026Q4's daily indexes, which has no 12 October (Columbus Day).
Nothing opens a socket.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx

from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.company_tickers import (
    CompanyTicker,
    CompanyTickersMalformedError,
    parse_company_tickers,
)
from app.ingestion.edgar.daily_index import (
    DailyIndexMalformedError,
    daily_index_url,
    fetch_published_days,
    parse_daily_index,
    parse_listing,
    quarter_listing_url,
)
from app.ingestion.insider_discovery import (
    DayKind,
    IndexCalendar,
    days_between,
    select_filings,
)
from tests.conftest import make_settings

FIXTURES: Final = Path(__file__).parent / "fixtures" / "daily_index"
INDEX: Final = (FIXTURES / "form.20241002.idx").read_bytes()
LISTING_2026Q4: Final = json.loads((FIXTURES / "2026-QTR4-index.json").read_text())

#: S3's answer for a key that does not exist.
_MISSING: Final = httpx.Response(
    403, content=b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>"
)


def _edgar() -> EdgarClient:
    return EdgarClient(make_settings(), limiter=AsyncTokenBucket(10_000.0))


def _line(form: str, name: str, *rest: str) -> bytes:
    """One index line, the form type and name padded to their columns."""
    return (f"{form:<12}{name:<62}" + "    ".join(rest) + "\n").encode()


def _listing(*days: date) -> dict[str, Any]:
    """A quarter's directory listing, with the other files EDGAR puts beside them."""
    items = [{"name": f"form.{day:%Y%m%d}.idx", "type": "file"} for day in days]
    items += [{"name": f"master.{day:%Y%m%d}.idx", "type": "file"} for day in days]
    return {"directory": {"item": items, "name": "daily-index/"}}


# --- the form index ----------------------------------------------------------


def test_only_form_4_and_4a_lines_are_kept() -> None:
    """Exactly: 40-APP/A and 424B2 start with a 4 too."""
    entries = parse_daily_index(INDEX)

    assert {entry.form_type for entry in entries} == {"4", "4/A"}
    assert len(entries) == 7


def test_a_line_is_read_by_its_columns() -> None:
    """A name with a comma and digits in it ("667, L.P.") stays whole."""
    entries = {
        (entry.company_name, entry.accession_no): entry for entry in parse_daily_index(INDEX)
    }

    entry = entries[("667, L.P.", "0000950170-24-111462")]
    assert entry.form_type == "4"
    assert entry.cik == "0001551139"
    assert entry.date_filed == date(2024, 10, 2)
    assert entry.file_name == "edgar/data/1551139/0000950170-24-111462.txt"


def test_a_filing_is_listed_once_per_party() -> None:
    """Under the issuer and under the insider, each line with its own CIK."""
    parties = {
        entry.company_name: entry.cik
        for entry in parse_daily_index(INDEX)
        if entry.accession_no == "0001062993-24-017331"
    }

    assert parties == {"ABBASI SOHAIB": "0001275992", "Elastic N.V.": "0001707753"}


def test_a_form_type_with_a_space_is_read_whole() -> None:
    body = INDEX + _line(
        "SC 13D/A",
        "BERKSHIRE HATHAWAY INC",
        "1067983",
        "20241002",
        "edgar/data/1067983/0000950170-24-111000.txt",
    )

    [entry] = parse_daily_index(body, forms=frozenset({"SC 13D/A"}))

    assert entry.company_name == "BERKSHIRE HATHAWAY INC"


def test_a_name_that_runs_into_the_cik_column_costs_only_its_tail() -> None:
    long_name = "A" * 70
    # Past the CIK column's start (74), with one space before the CIK.
    body = INDEX + _line(
        "4", f"{long_name} ", "320193", "20241002", "edgar/data/320193/0000320193-24-000001.txt"
    )

    entry = parse_daily_index(body)[-1]

    assert entry.cik == "0000320193"
    assert entry.company_name == long_name


def test_an_index_without_its_header_is_refused() -> None:
    with pytest.raises(DailyIndexMalformedError, match="header"):
        parse_daily_index(b"4           3M CO      66740   20241002   edgar/data/66740/x.txt\n")


@pytest.mark.parametrize(
    "fields",
    [
        # No date.
        ("66740", "edgar/data/66740/0001127602-24-024825.txt"),
        # A path under someone else's CIK.
        ("66740", "20241002", "edgar/data/1/0001127602-24-024825.txt"),
        # Not an accession number.
        ("66740", "20241002", "edgar/data/66740/primary.xml"),
    ],
)
def test_an_unreadable_form_4_line_fails_the_day(fields: tuple[str, ...]) -> None:
    """A Form 4 line that cannot be read is a filing that would be missed."""
    with pytest.raises(DailyIndexMalformedError):
        parse_daily_index(INDEX + _line("4", "3M CO", *fields))


def test_an_unreadable_line_of_another_form_is_ignored() -> None:
    body = INDEX + b"8-K         SOMETHING ODD\n"

    assert len(parse_daily_index(body)) == 7


def test_urls() -> None:
    assert daily_index_url(date(2024, 10, 2)) == (
        "https://www.sec.gov/Archives/edgar/daily-index/2024/QTR4/form.20241002.idx"
    )
    assert quarter_listing_url(2026, 1) == (
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR1/index.json"
    )


# --- the quarter listing -----------------------------------------------------


def test_a_listing_gives_the_days_with_a_form_index() -> None:
    days = parse_listing(LISTING_2026Q4)

    assert min(days) == date(2026, 10, 1)
    assert max(days) == date(2026, 10, 13)
    # Columbus Day, and the weekends either side of it.
    assert date(2026, 10, 12) not in days
    assert date(2026, 10, 10) not in days
    assert len(days) == 8


def test_a_listing_that_is_not_one_is_refused() -> None:
    with pytest.raises(DailyIndexMalformedError):
        parse_listing({"files": []})


@respx.mock
async def test_a_quarter_without_a_directory_has_no_days() -> None:
    """The first minutes of a quarter. S3's 403, once, not a minute's wait."""
    route = respx.get(quarter_listing_url(2027, 1)).mock(return_value=_MISSING)

    async with _edgar() as edgar:
        assert await fetch_published_days(edgar, 2027, 1) == frozenset()

    assert route.call_count == 1


# --- which days count as read --------------------------------------------------


@respx.mock
async def test_each_kind_of_day() -> None:
    respx.get(quarter_listing_url(2026, 4)).mock(
        return_value=httpx.Response(200, json=LISTING_2026Q4)
    )

    async with _edgar() as edgar:
        calendar = IndexCalendar(edgar, today=date(2026, 10, 14))
        assert await calendar.kind(date(2026, 10, 9)) is DayKind.INDEXED
        assert await calendar.kind(date(2026, 10, 10)) is DayKind.WEEKEND
        # A weekday with no index, and a later day with one: a holiday.
        assert await calendar.kind(date(2026, 10, 12)) is DayKind.NO_INDEX
        # Today, before the evening: no index, and none later.
        assert await calendar.kind(date(2026, 10, 14)) is DayKind.UNPUBLISHED


@respx.mock
async def test_a_quarters_last_holiday_is_settled_by_the_next_quarters_index() -> None:
    """New Year's Day observed on Friday 31 December 2021: the last weekday of
    the quarter, with no later day in its quarter. January's index settles it."""
    respx.get(quarter_listing_url(2021, 4)).mock(
        return_value=httpx.Response(200, json=_listing(date(2021, 12, 30)))
    )
    respx.get(quarter_listing_url(2022, 1)).mock(
        return_value=httpx.Response(200, json=_listing(date(2022, 1, 3), date(2022, 1, 4)))
    )

    async with _edgar() as edgar:
        calendar = IndexCalendar(edgar, today=date(2022, 1, 5))
        assert await calendar.kind(date(2021, 12, 31)) is DayKind.NO_INDEX


@respx.mock
async def test_on_a_quarters_first_morning_the_last_quarter_says_how_far_edgar_got() -> None:
    respx.get(quarter_listing_url(2026, 4)).mock(return_value=_MISSING)
    respx.get(quarter_listing_url(2026, 3)).mock(
        return_value=httpx.Response(200, json=_listing(date(2026, 9, 29), date(2026, 9, 30)))
    )

    async with _edgar() as edgar:
        calendar = IndexCalendar(edgar, today=date(2026, 10, 1))
        assert await calendar.latest_published() == date(2026, 9, 30)
        assert await calendar.kind(date(2026, 10, 1)) is DayKind.UNPUBLISHED


@respx.mock
async def test_a_listing_is_read_once_per_run() -> None:
    route = respx.get(quarter_listing_url(2026, 4)).mock(
        return_value=httpx.Response(200, json=LISTING_2026Q4)
    )

    async with _edgar() as edgar:
        calendar = IndexCalendar(edgar, today=date(2026, 10, 14))
        for day in days_between(date(2026, 10, 1), date(2026, 10, 14)):
            await calendar.kind(day)

    assert route.call_count == 1


# --- choosing filings -----------------------------------------------------------


def test_a_filing_matches_on_any_of_its_parties_and_is_kept_once() -> None:
    elastic, abbasi = "0001707753", "0001275992"

    [matched] = select_filings(parse_daily_index(INDEX), frozenset({elastic, abbasi}))

    assert matched.accession_no == "0001062993-24-017331"
    assert matched.filing.form_type == "4"
    assert matched.filing.report_date is None


def test_a_filing_with_no_party_in_the_universe_is_left() -> None:
    assert select_filings(parse_daily_index(INDEX), frozenset({"0000320193"})) == ()


def test_an_amendment_is_matched_like_an_original() -> None:
    [matched] = select_filings(parse_daily_index(INDEX), frozenset({"0001888654"}))

    assert matched.filing.form_type == "4/A"


def test_days_between_includes_both_ends() -> None:
    assert days_between(date(2026, 10, 9), date(2026, 10, 11)) == [
        date(2026, 10, 9),
        date(2026, 10, 10),
        date(2026, 10, 11),
    ]
    assert days_between(date(2026, 10, 11), date(2026, 10, 9)) == []


# --- SEC's ticker map -------------------------------------------------------------


def test_the_ticker_map_is_read_and_padded() -> None:
    document = {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
        "2": {"cik_str": 1067983, "ticker": "brk-a", "title": "BERKSHIRE HATHAWAY INC"},
    }

    assert parse_company_tickers(document) == [
        CompanyTicker(ticker="NVDA", cik="0001045810", name="NVIDIA CORP"),
        CompanyTicker(ticker="BRK-B", cik="0001067983", name="BERKSHIRE HATHAWAY INC"),
        CompanyTicker(ticker="BRK-A", cik="0001067983", name="BERKSHIRE HATHAWAY INC"),
    ]


@pytest.mark.parametrize("document", [{}, [], {"0": {"ticker": "NVDA"}}])
def test_a_ticker_map_that_is_empty_or_unreadable_is_refused(document: Any) -> None:
    with pytest.raises(CompanyTickersMalformedError):
        parse_company_tickers(document)
