"""Archiving one fetched 13F: which files, under which keys, as which bytes.

Against :class:`LocalRawStore` in a temp directory. The store's own contract —
write-once, byte-exact, the same on S3 — is tests/test_raw_store.py's subject;
what is asserted here is that a filing's documents all reach it, under the one
prefix, and that ``overwrite`` is passed through rather than decided here.
"""

from pathlib import Path
from typing import Final

import pytest

from app.ingestion.archive import archive_13f_documents, read_13f_documents
from app.ingestion.edgar.documents import FilingDocuments, FilingDocumentsError
from app.storage.raw import LocalRawStore, RawObjectNotFoundError, RawStoreError

ACCESSION: Final = "0001067983-24-000011"
CIK: Final = "0001067983"
DIRECTORY: Final = "https://www.sec.gov/Archives/edgar/data/1067983/000106798324000011"
PREFIX: Final = f"raw/13f/{CIK}/{ACCESSION}/"

INDEX: Final = b'{"directory": {"item": []}}'
PRIMARY_DOC: Final = b"<edgarSubmission/>"
INFO_TABLE: Final = b"<informationTable/>"


def _documents(*, info_table: bytes | None = INFO_TABLE) -> FilingDocuments:
    return FilingDocuments(
        index_url=f"{DIRECTORY}/index.json",
        index=INDEX,
        primary_doc_url=f"{DIRECTORY}/primary_doc.xml",
        primary_doc=PRIMARY_DOC,
        info_table_url=None if info_table is None else f"{DIRECTORY}/56757.xml",
        info_table=info_table,
    )


async def test_all_three_documents_land_under_the_accession_prefix(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)

    prefix = await archive_13f_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)

    assert prefix == PREFIX
    assert await store.list(prefix) == (
        f"{PREFIX}56757.xml",
        f"{PREFIX}index.json",
        f"{PREFIX}primary_doc.xml",
    )
    assert await store.get(f"{PREFIX}index.json") == INDEX
    assert await store.get(f"{PREFIX}primary_doc.xml") == PRIMARY_DOC
    assert await store.get(f"{PREFIX}56757.xml") == INFO_TABLE


async def test_a_notice_archives_its_cover_page_and_listing_only(tmp_path: Path) -> None:
    """A 13F-NT has no information table, and that is not a failure here
    either."""
    store = LocalRawStore(tmp_path)

    await archive_13f_documents(store, _documents(info_table=None), cik=CIK, accession_no=ACCESSION)

    assert await store.list(PREFIX) == (f"{PREFIX}index.json", f"{PREFIX}primary_doc.xml")


async def test_the_cik_and_accession_number_are_normalised_into_the_key(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)

    prefix = await archive_13f_documents(
        store, _documents(), cik="1067983", accession_no=ACCESSION.replace("-", "")
    )

    assert prefix == PREFIX


async def test_an_archived_filing_is_not_overwritten_by_default(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await store.put(f"{PREFIX}primary_doc.xml", b"as first fetched")

    await archive_13f_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)

    assert await store.get(f"{PREFIX}primary_doc.xml") == b"as first fetched"


async def test_overwrite_replaces_what_is_archived(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await store.put(f"{PREFIX}primary_doc.xml", b"as first fetched")

    await archive_13f_documents(
        store, _documents(), cik=CIK, accession_no=ACCESSION, overwrite=True
    )

    assert await store.get(f"{PREFIX}primary_doc.xml") == PRIMARY_DOC


async def test_a_store_failure_is_raised_as_itself(tmp_path: Path) -> None:
    """Not wrapped in an ExceptionGroup, which the CLI would not recognise as
    an archive outage and would print as a traceback."""
    blocker = tmp_path / "raw"
    blocker.write_bytes(b"a file where the raw/ directory should be")

    with pytest.raises(RawStoreError):
        await archive_13f_documents(
            LocalRawStore(tmp_path), _documents(), cik=CIK, accession_no=ACCESSION
        )


# --- reading it back ---------------------------------------------------------


async def test_what_is_archived_reads_back_as_it_was_fetched(tmp_path: Path) -> None:
    """The round trip ``backfill --force`` depends on: the same bytes and the
    same URLs, with nothing fetched."""
    store = LocalRawStore(tmp_path)
    await archive_13f_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)

    assert await read_13f_documents(store, PREFIX, directory_url=DIRECTORY) == _documents()


async def test_a_notice_reads_back_with_no_information_table(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await archive_13f_documents(store, _documents(info_table=None), cik=CIK, accession_no=ACCESSION)

    documents = await read_13f_documents(store, PREFIX, directory_url=DIRECTORY)

    assert documents.info_table is None
    assert documents.info_table_url is None
    assert documents.primary_doc == PRIMARY_DOC


async def test_the_documents_are_told_apart_by_root_element_not_by_name(tmp_path: Path) -> None:
    """The information table's name is the filing agent's choice, and the
    cover page is ``primary_doc.xml`` only by convention."""
    store = LocalRawStore(tmp_path)
    await store.put(f"{PREFIX}index.json", INDEX)
    await store.put(f"{PREFIX}a-cover.xml", PRIMARY_DOC)
    await store.put(f"{PREFIX}b-holdings.xml", INFO_TABLE)

    documents = await read_13f_documents(store, PREFIX, directory_url=DIRECTORY)

    assert documents.primary_doc_url == f"{DIRECTORY}/a-cover.xml"
    assert documents.info_table_url == f"{DIRECTORY}/b-holdings.xml"


async def test_a_filing_never_archived_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(RawObjectNotFoundError, match=PREFIX):
        await read_13f_documents(LocalRawStore(tmp_path), PREFIX, directory_url=DIRECTORY)


async def test_an_archive_without_its_listing_is_not_found(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await store.put(f"{PREFIX}primary_doc.xml", PRIMARY_DOC)

    with pytest.raises(RawObjectNotFoundError, match=r"index\.json"):
        await read_13f_documents(store, PREFIX, directory_url=DIRECTORY)


async def test_an_archive_without_a_cover_page_is_refused(tmp_path: Path) -> None:
    store = LocalRawStore(tmp_path)
    await store.put(f"{PREFIX}index.json", INDEX)
    await store.put(f"{PREFIX}56757.xml", INFO_TABLE)

    with pytest.raises(FilingDocumentsError, match="cover page"):
        await read_13f_documents(store, PREFIX, directory_url=DIRECTORY)


async def test_two_archived_information_tables_are_refused_rather_than_guessed(
    tmp_path: Path,
) -> None:
    """Picking the wrong one parses to a different portfolio without raising."""
    store = LocalRawStore(tmp_path)
    await archive_13f_documents(store, _documents(), cik=CIK, accession_no=ACCESSION)
    await store.put(f"{PREFIX}infotable.xml", INFO_TABLE)

    with pytest.raises(FilingDocumentsError, match="more than one"):
        await read_13f_documents(store, PREFIX, directory_url=DIRECTORY)
