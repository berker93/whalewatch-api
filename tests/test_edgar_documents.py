"""Tests for locating a 13F's two documents inside an EDGAR filing directory.

The subject is a naming problem with a silent failure mode. Only one of the two
files has a predictable name, so the other is identified by what is in it — and
the assertions that matter here are the ones about *not* accepting the wrong
file, because the wrong file does not raise anywhere downstream. It parses to
zero rows, and a filing with zero rows is a perfectly legitimate ``13F-NT``.

So there are two tests for every shape of directory: one that the right document
is found, and one that a directory which cannot offer the right document fails
loudly instead of yielding an empty portfolio.

Nothing opens a socket. ``httpx.MockTransport`` answers from a dict of URL path
to body, which is also what lets a test assert on *how many* documents were
fetched — the ranking in ``_ranked`` is only worth having if it saves a request.
"""

import json
from typing import Any, Final

import httpx
import pytest

from app.core.config import Settings
from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.documents import FilingDocumentsError, fetch_13f_documents

ACCESSION: Final = "0001067983-24-000011"
CIK: Final = "0001067983"
DIRECTORY: Final = "/Archives/edgar/data/1067983/000106798324000011"

#: The two documents, trimmed to their root elements — which is all this module
#: ever looks at. The parsers have their own fixtures for the rest.
PRIMARY_DOC: Final = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
    b"<headerData/></edgarSubmission>"
)
INFO_TABLE: Final = (
    b'<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
    b"<infoTable/></informationTable>"
)

#: A document that is neither, and the reason this module exists: a cover letter
#: or an exhibit sitting in the same directory parses to zero holdings rather
#: than to an error.
EXHIBIT: Final = b'<?xml version="1.0"?><exhibit><body>see attached</body></exhibit>'

#: Fast enough that pacing never shows up in these timings. The real bucket is
#: asserted on in tests/test_edgar_client.py, where it is the subject.
_UNTHROTTLED: Final = 10_000.0


def _index(*names: str) -> bytes:
    """An EDGAR ``index.json`` listing exactly these filenames.

    Shaped as EDGAR shapes it, ``type`` included: it looks like a MIME type and
    is in fact the name of the icon the directory listing renders, which is why
    nothing in the module under test reads it.
    """
    return json.dumps(
        {
            "directory": {
                "item": [
                    {"name": name, "type": "text.gif", "size": "1024", "last-modified": ""}
                    for name in names
                ],
                "name": DIRECTORY,
                "parent-dir": DIRECTORY.rsplit("/", 1)[0],
            }
        }
    ).encode()


def _client(settings: Settings, documents: dict[str, bytes], fetched: list[str]) -> EdgarClient:
    """A client answering from ``documents``, recording every path it served.

    ``fetched`` is the list the request-count assertions read. It records the
    request rather than the reply so that a 404 still shows up in it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.path)
        body = documents.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return EdgarClient(
        settings,
        transport=httpx.MockTransport(handler),
        limiter=AsyncTokenBucket(_UNTHROTTLED),
    )


async def _fetch(settings: Settings, documents: dict[str, bytes]) -> Any:
    async with _client(settings, documents, []) as edgar:
        return await fetch_13f_documents(edgar, cik=CIK, accession_no=ACCESSION)


# --- the ordinary directory --------------------------------------------------


async def test_the_two_documents_are_found_and_fetched(settings: Settings) -> None:
    documents = await _fetch(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("primary_doc.xml", "form13fInfoTable.xml"),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
            f"{DIRECTORY}/form13fInfoTable.xml": INFO_TABLE,
        },
    )

    assert documents.primary_doc == PRIMARY_DOC
    assert documents.info_table == INFO_TABLE
    assert documents.primary_doc_url.endswith(f"{DIRECTORY}/primary_doc.xml")
    assert documents.info_table_url is not None
    assert documents.info_table_url.endswith(f"{DIRECTORY}/form13fInfoTable.xml")


@pytest.mark.parametrize(
    "name",
    [
        "form13fInfoTable.xml",
        "infotable.xml",
        "0001234-info.xml",
        # Berkshire's, where the number is the filing agent's internal job id and
        # carries no hint at all. The one that breaks every name-matching rule.
        "56757.xml",
    ],
)
async def test_the_information_table_is_found_whatever_it_is_called(
    settings: Settings, name: str
) -> None:
    """Filing agents do not agree on a name and EDGAR does not impose one, so the
    only rule that holds across all four of these is "not the cover page"."""
    documents = await _fetch(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("primary_doc.xml", name),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
            f"{DIRECTORY}/{name}": INFO_TABLE,
        },
    )

    assert documents.info_table == INFO_TABLE


async def test_a_namespace_prefix_on_the_root_element_still_matches(
    settings: Settings,
) -> None:
    """``<ns1:informationTable>`` is the same document as ``<informationTable>``.

    Asserted for the same reason the parsers use ``local-name()`` XPaths: the
    13F namespace URI has changed between schema versions, and a rule that
    pinned a prefix or a URI would reject a valid document by matching nothing.
    """
    prefixed = (
        b'<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf'
        b'/informationtable"><ns1:infoTable/></ns1:informationTable>'
    )
    documents = await _fetch(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("primary_doc.xml", "table.xml"),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
            f"{DIRECTORY}/table.xml": prefixed,
        },
    )

    assert documents.info_table == prefixed


# --- directories that would otherwise load an empty portfolio ----------------


async def test_a_stray_xml_file_does_not_become_the_portfolio(settings: Settings) -> None:
    """The failure this module exists for, in its cheapest form.

    Two candidates, and the name-based rule cannot choose between them. Picking
    the exhibit yields a filing with no holdings and no error, which reads as a
    fund that sold everything.
    """
    fetched: list[str] = []
    async with _client(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("primary_doc.xml", "exhibit99.xml", "infotable.xml"),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
            f"{DIRECTORY}/exhibit99.xml": EXHIBIT,
            f"{DIRECTORY}/infotable.xml": INFO_TABLE,
        },
        fetched,
    ) as edgar:
        documents = await fetch_13f_documents(edgar, cik=CIK, accession_no=ACCESSION)

    assert documents.info_table == INFO_TABLE
    # And the hint in the name earned its keep: the exhibit was never fetched.
    assert f"{DIRECTORY}/exhibit99.xml" not in fetched


async def test_a_lone_candidate_is_still_confirmed_before_it_is_accepted(
    settings: Settings,
) -> None:
    """One candidate is not evidence, because the fallback is silent.

    A directory holding the cover page and one unrelated XML file is the case
    where "the only other .xml" is confidently wrong. Raising is the only
    outcome distinguishable from a legitimate empty filing.
    """
    with pytest.raises(FilingDocumentsError, match="informationTable"):
        await _fetch(
            settings,
            {
                f"{DIRECTORY}/index.json": _index("primary_doc.xml", "exhibit99.xml"),
                f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
                f"{DIRECTORY}/exhibit99.xml": EXHIBIT,
            },
        )


async def test_the_stylesheet_rendering_of_the_cover_page_is_not_a_candidate(
    settings: Settings,
) -> None:
    """``xslForm13F_X02/primary_doc.xml`` is the document with a stylesheet on it.

    EDGAR lists it in the same index, and fetching it returns a transformed copy
    rather than the bytes the filer submitted — which is not what belongs in an
    archive, and not what the parser's checksum was computed against.
    """
    fetched: list[str] = []
    async with _client(
        settings,
        {
            f"{DIRECTORY}/index.json": _index(
                "xslForm13F_X02/primary_doc.xml", "primary_doc.xml", "infotable.xml"
            ),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
            f"{DIRECTORY}/infotable.xml": INFO_TABLE,
        },
        fetched,
    ) as edgar:
        await fetch_13f_documents(edgar, cik=CIK, accession_no=ACCESSION)

    assert not any("xsl" in path for path in fetched)


# --- the filings that legitimately have one document -------------------------


async def test_a_notice_with_no_information_table_is_not_a_failure(
    settings: Settings,
) -> None:
    """A ``13F-NT`` reports no holdings, so its directory holds the cover page
    alone. ``None`` here is the honest answer; an exception would fail a valid
    filing and an empty ``bytes`` would erase the distinction from a document we
    could not find."""
    documents = await _fetch(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("primary_doc.xml"),
            f"{DIRECTORY}/primary_doc.xml": PRIMARY_DOC,
        },
    )

    assert documents.info_table is None
    assert documents.info_table_url is None


# --- the cover page ----------------------------------------------------------


async def test_a_renamed_cover_page_is_found_by_its_root_element(
    settings: Settings,
) -> None:
    """Every 13F in the archive names this file ``primary_doc.xml``. The fallback
    is for the day one does not, and the cost of not having it is losing the
    period, the form type and the filer's own totals."""
    documents = await _fetch(
        settings,
        {
            f"{DIRECTORY}/index.json": _index("cover.xml", "infotable.xml"),
            f"{DIRECTORY}/cover.xml": PRIMARY_DOC,
            f"{DIRECTORY}/infotable.xml": INFO_TABLE,
        },
    )

    assert documents.primary_doc == PRIMARY_DOC
    assert documents.info_table == INFO_TABLE


async def test_a_directory_with_no_cover_page_fails(settings: Settings) -> None:
    with pytest.raises(FilingDocumentsError, match="edgarSubmission"):
        await _fetch(
            settings,
            {
                f"{DIRECTORY}/index.json": _index("exhibit99.xml"),
                f"{DIRECTORY}/exhibit99.xml": EXHIBIT,
            },
        )


async def test_an_empty_directory_fails(settings: Settings) -> None:
    with pytest.raises(FilingDocumentsError):
        await _fetch(settings, {f"{DIRECTORY}/index.json": _index()})


# --- listings that are not listings ------------------------------------------


@pytest.mark.parametrize(
    "listing",
    [
        b"[]",
        b'{"nothing": true}',
        b'{"directory": {}}',
        b'{"directory": {"item": "not an array"}}',
    ],
)
async def test_an_unreadable_index_is_an_error_not_an_empty_directory(
    settings: Settings, listing: bytes
) -> None:
    """Because "no files here" and "I could not read the file list" would
    otherwise both arrive as a filing with no holdings."""
    with pytest.raises(FilingDocumentsError):
        await _fetch(settings, {f"{DIRECTORY}/index.json": listing})
