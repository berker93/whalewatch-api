"""Finding and fetching one quarter's Official List of Section 13(f) Securities.

The SEC does not give the list a stable URL. The files have lived under
``/divisions/investment/13f/`` and now live under ``/files/investment/``, and
the name is not stable within one location either: 2026Q2's text file is
``13flist2026q2-txt.txt`` and 2026Q1's is ``13flist2026q1.txt``. So nothing here
builds a list's URL. It reads the Division of Investment Management's page that
links every quarter's files (:data:`LIST_PAGE_URL`) and takes the link it finds
there.

:data:`LIST_PAGE_URL` is the page's old address, ``/divisions/investment/13flists``,
which redirects to its current one. The old one has survived one move of the
page already, so it is the better bet to survive the next.

Which link is which quarter
---------------------------
By the link's **filename**, not the text beside it. The page labels its rows
("1st quarter 2025:") and the labels are not reliable: the 2025Q1 row's PDF link
is ``13flist2025q4.pdf``. Every filename so far names its own quarter as
``13flist{year}q{n}``, with or without a suffix.

A filename can still name the wrong contents. The SEC's 2025Q3 text file,
``13flist2025q3-txt.txt``, is byte for byte its 2025Q1 file. Nothing on the page
reveals that. The import catches it by comparing the list with the quarter
before (:func:`~app.ingestion.loaders.thirteen_f_list.check_additions`).

Text, never PDF
---------------
A quarter is fetched as its text file or not at all. Text files start with
2020Q1. A quarter that only has a PDF raises :class:`ThirteenFListUnavailableError`
naming it, as does a quarter the page does not list yet.

Through :class:`~app.ingestion.edgar.client.EdgarClient`, because these are
sec.gov requests like any other: the same User-Agent, the same rate limit.
With ``EDGAR_CACHE_DIR`` set, the page is cached like everything else and never
expires, so a development cache will not see a new quarter until it is deleted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import urljoin, urlsplit

import lxml.html

from app.core.logging import get_logger
from app.core.periods import quarter_end, quarter_label
from app.ingestion.edgar.client import EdgarClient

logger = get_logger(__name__)

LIST_PAGE_URL: Final = "https://www.sec.gov/divisions/investment/13flists"

#: The only host a list link may point at. The client sends our User-Agent,
#: contact address included, wherever it is pointed.
_SEC_HOST: Final = "www.sec.gov"

#: ``13flist2026q2-txt.txt``, ``13flist2019q4.pdf``: the quarter, then anything
#: up to the extension (the CMS appends ``_0`` to a name it already has).
_FILENAME: Final = re.compile(r"13flist(\d{4})q([1-4])[^/]*\.(txt|pdf)", re.IGNORECASE)


class ThirteenFListUnavailableError(Exception):
    """No text file for the quarter: not published yet, PDF only, or ambiguous."""


@dataclass(frozen=True, slots=True)
class PublishedList:
    """What the page links for one quarter."""

    period: date
    text_urls: tuple[str, ...]
    pdf_urls: tuple[str, ...]


def published_lists(page: bytes, *, base_url: str = LIST_PAGE_URL) -> dict[date, PublishedList]:
    """Every quarter the page links a list for, keyed by quarter end.

    Links off sec.gov and links that are not a list file are ignored. Repeats
    of the same URL count once; the page links 2025Q4's PDF twice.
    """
    links: defaultdict[date, dict[str, set[str]]] = defaultdict(
        lambda: {"txt": set(), "pdf": set()}
    )
    if not page.strip():
        # lxml raises on an empty document; an empty page simply links nothing.
        return {}
    for anchor in lxml.html.fromstring(page).iter("a"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(base_url, href.strip())
        parts = urlsplit(url)
        if parts.hostname != _SEC_HOST:
            continue
        filename = parts.path.rsplit("/", 1)[-1]
        if (match := _FILENAME.fullmatch(filename)) is None:
            continue
        period = quarter_end(int(match[1]), int(match[2]))
        links[period][match[3].lower()].add(url)
    return {
        period: PublishedList(period, tuple(sorted(found["txt"])), tuple(sorted(found["pdf"])))
        for period, found in sorted(links.items())
    }


def text_url(lists: dict[date, PublishedList], period: date) -> str:
    """The one text file the page links for ``period``.

    :raises ThirteenFListUnavailableError: None is linked, or more than one.
    """
    label = quarter_label(period)
    published = lists.get(period)
    if published is None:
        latest = quarter_label(max(lists)) if lists else None
        raise ThirteenFListUnavailableError(
            f"the SEC's 13(f) list page has no list for {label}"
            + (f"; the latest it has is {latest}" if latest else "; it lists no quarters at all")
            + f" ({LIST_PAGE_URL})"
        )
    if not published.text_urls:
        raise ThirteenFListUnavailableError(
            f"the SEC publishes {label}'s list only as a PDF ({', '.join(published.pdf_urls)}), "
            "which is not parsed; text files start with 2020Q1"
        )
    if len(published.text_urls) > 1:
        raise ThirteenFListUnavailableError(
            f"the SEC's 13(f) list page links more than one text file for {label}: "
            f"{', '.join(published.text_urls)}. Download the right one and pass it with --file"
        )
    return published.text_urls[0]


async def find_list_url(edgar: EdgarClient, period: date) -> str:
    """Read the SEC's page and return the URL of ``period``'s text file.

    :raises ThirteenFListUnavailableError: See :func:`text_url`.
    """
    lists = published_lists(await edgar.get_bytes(LIST_PAGE_URL))
    url = text_url(lists, period)
    logger.info(
        "thirteen_f_list.found", quarter=quarter_label(period), url=url, quarters=len(lists)
    )
    return url


def filename_quarter(filename: str) -> date | None:
    """The quarter end a list's filename names, or ``None`` if it names none."""
    match = _FILENAME.fullmatch(filename)
    return None if match is None else quarter_end(int(match[1]), int(match[2]))
