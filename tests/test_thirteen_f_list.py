"""The 13(f) securities list: its parser, and finding it on the SEC's page.

Both against fixtures cut from what sec.gov served on 2026-10-12
(``tests/fixtures/13f_list``), so the cases here are the file's and the page's
real quirks rather than ones imagined for the test. Nothing opens a socket:
``find_list_url`` gets an :class:`~app.ingestion.edgar.client.EdgarClient` on a
``MockTransport``.
"""

from datetime import date
from pathlib import Path
from typing import Final

import httpx
import pytest

from app.core.rate_limit import AsyncTokenBucket
from app.db.models.sec_13f_security import ListStatus
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.thirteen_f_list import (
    LIST_PAGE_URL,
    ThirteenFListUnavailableError,
    filename_quarter,
    find_list_url,
    published_lists,
    text_url,
)
from app.ingestion.parsers.thirteen_f_list import (
    ListedSecurity,
    ThirteenFListParseError,
    parse_13f_list,
)
from tests.conftest import make_settings

FIXTURES: Final = Path(__file__).parent / "fixtures" / "13f_list"
LIST_2026Q1: Final = (FIXTURES / "13flist2026q1.txt").read_bytes()
PAGE: Final = (FIXTURES / "13flists.html").read_bytes()

APPLE_LINE: Final = (
    "037833100*APPLE INC                     COM                                    E"
)


def _by_cusip(body: bytes) -> dict[str, ListedSecurity]:
    return {security.cusip: security for security in parse_13f_list(body).securities}


# --- the parser --------------------------------------------------------------


def test_a_line_is_read_by_its_documented_columns() -> None:
    apple = _by_cusip(LIST_2026Q1)["037833100"]

    assert apple == ListedSecurity(
        cusip="037833100",
        issuer_name="APPLE INC",
        issuer_description="COM",
        status=ListStatus.LISTED,
        has_listed_options=True,
    )


def test_the_status_column() -> None:
    securities = _by_cusip(LIST_2026Q1)

    assert securities["G0R21F121"].status is ListStatus.ADDED
    assert securities["G0223V105"].status is ListStatus.DELETED
    assert securities["084670108"].status is ListStatus.LISTED


def test_the_option_indicator() -> None:
    securities = _by_cusip(LIST_2026Q1)

    assert securities["084670702"].has_listed_options
    assert not securities["084670108"].has_listed_options


def test_option_rows_are_kept_though_their_cusips_fail_the_check_digit() -> None:
    """B38564908 is CMB.TECH's call; its check digit should be 4, not 8."""
    call = _by_cusip(LIST_2026Q1)["B38564908"]

    assert (call.issuer_name, call.issuer_description) == ("CMB.TECH NV", "CALL")


def test_every_repeated_cusip_becomes_one_row_and_is_counted() -> None:
    parsed = parse_13f_list(LIST_2026Q1)

    assert parsed.lines == 19
    assert parsed.collapsed == 4
    assert len(parsed.securities) == 15
    assert len({security.cusip for security in parsed.securities}) == 15


def test_identical_repeats_collapse_to_the_line_they_repeat() -> None:
    alps = _by_cusip(LIST_2026Q1)["00162Q905"]

    assert (alps.issuer_name, alps.status) == ("ALPS ETF TR", ListStatus.LISTED)


def test_a_cusip_deleted_and_added_in_one_list_is_listed() -> None:
    """LIFEWARD's call: one underlying's option deleted, another's added. The
    nine characters were on the last list and are on this one."""
    call = _by_cusip(LIST_2026Q1)["M8216Q909"]

    assert call.status is ListStatus.LISTED


def test_a_repeated_cusip_takes_its_name_from_a_copy_not_deleted() -> None:
    """The file prints 25460E904 deleted as DIREXION SHS ETF TR and listed as
    DIREXION SHARES ETF TRUST."""
    direxion = _by_cusip(LIST_2026Q1)["25460E904"]

    assert direxion.issuer_name == "DIREXION SHARES ETF TRUST"
    assert direxion.status is ListStatus.LISTED


def test_copies_that_agree_keep_their_status_and_any_copys_option_flag() -> None:
    body = (
        b"M8216Q909 LIFEWARD LTD                  CALL                       *D*         E\n"
        b"M8216Q909*LIFEWARD LTD                  CALL                       *D*         E\n"
    )
    [call] = parse_13f_list(body).securities

    assert call.status is ListStatus.DELETED
    assert call.has_listed_options


def test_securities_keep_the_files_order() -> None:
    cusips = [security.cusip for security in parse_13f_list(LIST_2026Q1).securities]

    assert cusips[:3] == ["B38564108", "B38564908", "B38564958"]
    assert cusips[-1] == "25460E904"


def test_crlf_and_trimmed_trailing_blanks_are_the_same_file() -> None:
    expected = _by_cusip(APPLE_LINE.encode())

    assert _by_cusip(APPLE_LINE.encode() + b"\r\n\r\n") == expected
    assert _by_cusip(APPLE_LINE.rstrip(" E").encode()) == expected


def test_runs_of_blanks_in_a_name_are_one_blank() -> None:
    line = "05344R104 AVALON   GLOBOCARE CORP       COM                                    E"

    assert _by_cusip(line.encode())["05344R104"].issuer_name == "AVALON GLOBOCARE CORP"


def test_a_byte_that_is_not_utf8_costs_a_character_not_the_import() -> None:
    line = APPLE_LINE.replace("APPLE INC", "APPL\xc9 INC").encode("latin-1")

    assert _by_cusip(line)["037833100"].issuer_name == "APPLÉ INC"


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (APPLE_LINE + " ", "longer than 80"),
        ("03783310X" + APPLE_LINE[9:], "columns 1-9 are not a CUSIP"),
        ("037833100#" + APPLE_LINE[10:], "column 10"),
        (APPLE_LINE[:67] + "*X*" + APPLE_LINE[70:], "columns 68-70"),
        (APPLE_LINE[:10] + " " * 30 + APPLE_LINE[40:], "no issuer name"),
    ],
)
def test_a_line_out_of_layout_fails_the_whole_parse_and_says_where(line: str, reason: str) -> None:
    body = APPLE_LINE + "\n" + line + "\n"

    with pytest.raises(ThirteenFListParseError, match=f"line 2: {reason}") as raised:
        parse_13f_list(body.encode())
    assert raised.value.line_no == 2


def test_an_html_page_served_as_the_list_fails() -> None:
    with pytest.raises(ThirteenFListParseError, match="line 1: columns 1-9 are not a CUSIP"):
        parse_13f_list(PAGE)


@pytest.mark.parametrize("body", [b"", b"\n\n   \n"])
def test_an_empty_list_fails(body: bytes) -> None:
    with pytest.raises(ThirteenFListParseError, match="no securities"):
        parse_13f_list(body)


# --- the page ----------------------------------------------------------------


def test_every_quarter_the_page_links_is_found() -> None:
    lists = published_lists(PAGE)

    assert list(lists) == [
        date(2019, 12, 31),
        date(2025, 3, 31),
        date(2025, 12, 31),
        date(2026, 3, 31),
        date(2026, 6, 30),
        date(2026, 9, 30),
    ]


def test_relative_links_resolve_against_sec_gov() -> None:
    assert published_lists(PAGE)[date(2026, 9, 30)].text_urls == (
        "https://www.sec.gov/files/investment/13flist2026q3.txt",
    )


def test_a_suffixed_filename_is_still_its_quarters() -> None:
    assert published_lists(PAGE)[date(2026, 6, 30)].text_urls == (
        "https://www.sec.gov/files/investment/13flist2026q2-txt.txt",
    )


def test_a_link_belongs_to_the_quarter_its_filename_names_not_its_row() -> None:
    """The 2025Q1 row links 2025Q4's PDF. Q1 gets its text file and no PDF, and
    Q4's PDF, linked twice, counts once."""
    lists = published_lists(PAGE)

    assert lists[date(2025, 3, 31)].pdf_urls == ()
    assert lists[date(2025, 12, 31)].pdf_urls == (
        "https://www.sec.gov/files/investment/13flist2025q4.pdf",
    )


def test_a_link_off_sec_gov_is_ignored() -> None:
    assert date(2019, 9, 30) not in published_lists(PAGE)


def test_an_empty_page_links_no_quarters() -> None:
    """And so is refused by text_url, as a page with no lists, not a traceback."""
    with pytest.raises(ThirteenFListUnavailableError, match="lists no quarters at all"):
        text_url(published_lists(b""), date(2026, 3, 31))


def test_text_url_is_the_quarters_one_text_file() -> None:
    url = text_url(published_lists(PAGE), date(2026, 3, 31))

    assert url == "https://www.sec.gov/files/investment/13flist2026q1.txt"


def test_a_pdf_only_quarter_is_refused_naming_the_pdf() -> None:
    with pytest.raises(ThirteenFListUnavailableError, match=r"2019Q4's list only as a PDF.*2020Q1"):
        text_url(published_lists(PAGE), date(2019, 12, 31))


def test_a_quarter_not_on_the_page_is_refused_naming_the_latest() -> None:
    with pytest.raises(
        ThirteenFListUnavailableError, match=r"no list for 2026Q4.*latest it has is 2026Q3"
    ):
        text_url(published_lists(PAGE), date(2026, 12, 31))


def test_two_text_files_for_one_quarter_are_refused() -> None:
    page = PAGE.replace(
        b"</body>", b'<a href="/files/investment/13flist2026q1_0.txt">again</a></body>'
    )

    with pytest.raises(ThirteenFListUnavailableError, match="more than one text file for 2026Q1"):
        text_url(published_lists(page), date(2026, 3, 31))


@pytest.mark.parametrize(
    ("filename", "period"),
    [
        ("13flist2026q1.txt", date(2026, 3, 31)),
        ("13flist2026q2-txt.txt", date(2026, 6, 30)),
        ("13FLIST2019Q4.PDF", date(2019, 12, 31)),
        ("my-download.txt", None),
    ],
)
def test_filename_quarter(filename: str, period: date | None) -> None:
    assert filename_quarter(filename) == period


async def test_find_list_url_reads_the_page_through_the_edgar_client() -> None:
    """With the client's User-Agent, at the page's old address."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PAGE)

    edgar = EdgarClient(
        make_settings(),
        limiter=AsyncTokenBucket(10_000.0),
        transport=httpx.MockTransport(handler),
    )
    async with edgar:
        url = await find_list_url(edgar, date(2026, 6, 30))

    assert url == "https://www.sec.gov/files/investment/13flist2026q2-txt.txt"
    [request] = requests
    assert str(request.url) == LIST_PAGE_URL
    assert request.headers["User-Agent"] == make_settings().sec_user_agent
