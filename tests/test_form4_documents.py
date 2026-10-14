"""Finding a Form 4's documents, archiving them, and reading them back.

The ownership document is named by the filing agent, so it is found in the
directory listing and confirmed by its root element. The acceptance time comes
from the filing's SGML header, in New York time. Nothing opens a socket.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.config import Settings
from app.core.rate_limit import AsyncTokenBucket
from app.ingestion.archive import archive_form4_documents, read_form4_documents
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.form4_documents import (
    Form4Documents,
    Form4DocumentsError,
    accepted_at,
    fetch_form4_documents,
    header_url,
)
from app.storage.raw import LocalRawStore, form4_key, form4_prefix

ACCESSION: Final = "0002152188-26-000007"
CIK: Final = "0001045810"
DIRECTORY: Final = "/Archives/edgar/data/1045810/000215218826000007"
BASE: Final = f"https://www.sec.gov{DIRECTORY}"
PREFIX: Final = f"raw/form4/{CIK}/{ACCESSION}/"

DOCUMENT: Final = (
    Path(__file__).parent / "fixtures" / "form4" / "amendment-nvidia.xml"
).read_bytes()
#: The real header's first lines, as EDGAR serves them for this filing.
HEADER: Final = (
    b"<SEC-HEADER>0002152188-26-000007.hdr.sgml : 20261013\n"
    b"<ACCEPTANCE-DATETIME>20261013170929\n"
    b"<ACCESSION-NUMBER>0002152188-26-000007\n"
    b"<TYPE>4/A\n"
)
ACCEPTED: Final = datetime(2026, 10, 13, 17, 9, 29, tzinfo=ZoneInfo("America/New_York"))


def _index(*names: str) -> bytes:
    """``index.json`` as EDGAR lists a Form 4's directory: the generated
    pages, then the agent's XML."""
    items = [f"{ACCESSION}-index-headers.html", f"{ACCESSION}-index.html", f"{ACCESSION}.txt"]
    return json.dumps(
        {
            "directory": {
                "item": [
                    {"name": name, "type": "text.gif", "size": ""} for name in items + list(names)
                ],
                "name": DIRECTORY,
            }
        }
    ).encode()


async def _fetch(settings: Settings, documents: dict[str, bytes]) -> Form4Documents:
    def handler(request: httpx.Request) -> httpx.Response:
        body = documents.get(request.url.path)
        return httpx.Response(404) if body is None else httpx.Response(200, content=body)

    async with EdgarClient(
        settings, transport=httpx.MockTransport(handler), limiter=AsyncTokenBucket(10_000.0)
    ) as edgar:
        return await fetch_form4_documents(edgar, cik=CIK, accession_no=ACCESSION)


def _served(*names: str, **bodies: bytes) -> dict[str, bytes]:
    return {
        f"{DIRECTORY}/index.json": _index(*names),
        f"{DIRECTORY}/{ACCESSION}.hdr.sgml": HEADER,
        **{f"{DIRECTORY}/{name}": body for name, body in bodies.items()},
    }


# --- fetching ----------------------------------------------------------------------


async def test_the_agents_document_and_the_header_are_fetched(settings: Settings) -> None:
    name = "wk-form4a_1791925767.xml"

    documents = await _fetch(settings, _served(name, **{name: DOCUMENT}))

    assert documents.document_url == f"{BASE}/{name}"
    assert documents.document == DOCUMENT
    assert documents.header_url == f"{BASE}/{ACCESSION}.hdr.sgml"
    assert documents.index == _index(name)
    assert documents.filed_at == ACCEPTED


async def test_an_xml_file_that_is_not_an_ownership_document_is_passed_over(
    settings: Settings,
) -> None:
    documents = await _fetch(
        settings,
        _served(
            "exhibit.xml", "form4.xml", **{"exhibit.xml": b"<exhibit/>", "form4.xml": DOCUMENT}
        ),
    )

    assert documents.document_url == f"{BASE}/form4.xml"


@pytest.mark.parametrize(
    "served",
    [
        _served(),
        _served("exhibit.xml", **{"exhibit.xml": b"<exhibit/>"}),
        _served("a.xml", "b.xml", **{"a.xml": DOCUMENT, "b.xml": DOCUMENT}),
    ],
    ids=["none", "not one", "two"],
)
async def test_anything_but_one_ownership_document_is_refused(
    settings: Settings, served: dict[str, bytes]
) -> None:
    with pytest.raises(Form4DocumentsError, match="expected one <ownershipDocument>"):
        await _fetch(settings, served)


def test_the_acceptance_time_is_new_york_time() -> None:
    assert accepted_at(HEADER) == ACCEPTED
    assert accepted_at(HEADER).isoformat() == "2026-10-13T17:09:29-04:00"


def test_a_header_with_no_acceptance_time_is_refused() -> None:
    with pytest.raises(Form4DocumentsError, match="ACCEPTANCE-DATETIME"):
        accepted_at(b"<SEC-HEADER>\n<TYPE>4\n")


def test_the_header_url() -> None:
    assert header_url("1045810", ACCESSION) == f"{BASE}/{ACCESSION}.hdr.sgml"


def test_the_key_scheme() -> None:
    assert form4_prefix(1045810, ACCESSION.replace("-", "")) == PREFIX
    assert form4_key(CIK, ACCESSION, "form4.xml") == f"{PREFIX}form4.xml"


# --- the archive ---------------------------------------------------------------------


def _documents(name: str = "wk-form4a_1791925767.xml") -> Form4Documents:
    return Form4Documents(
        index_url=f"{BASE}/index.json",
        index=_index(name),
        document_url=f"{BASE}/{name}",
        document=DOCUMENT,
        header_url=f"{BASE}/{ACCESSION}.hdr.sgml",
        header=HEADER,
        filed_at=ACCEPTED,
    )


async def test_the_three_documents_are_archived_and_read_back_as_fetched(
    tmp_path: Path,
) -> None:
    store = LocalRawStore(tmp_path)

    prefix = await archive_form4_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)

    assert prefix == PREFIX
    assert await store.list(prefix) == (
        f"{PREFIX}{ACCESSION}.hdr.sgml",
        f"{PREFIX}index.json",
        f"{PREFIX}wk-form4a_1791925767.xml",
    )
    assert await read_form4_documents(store, prefix, directory_url=BASE) == _documents()


async def test_an_archive_without_its_header_is_refused(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await archive_form4_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)
    (tmp_path / PREFIX / f"{ACCESSION}.hdr.sgml").unlink()

    with pytest.raises(Form4DocumentsError, match="filing header"):
        await read_form4_documents(store, PREFIX, directory_url=BASE)
