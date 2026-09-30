"""Archiving a fetched 13F into the raw store, before anything parses it.

The order is the point. A parser that raises on a document has, by then, already
had the document written somewhere it cannot lose it, so the fix is a re-parse
of stored bytes rather than a re-fetch — and a document EDGAR later restates or
withdraws is still here in the form it was first read.

Three files per filing, all under one accession prefix
(:func:`~app.storage.raw.thirteen_f_prefix`): the cover page, the information
table when there is one, and EDGAR's ``index.json`` for the directory. The last
is small and is what a re-parse is checked against — the full list of what EDGAR
had there, including the candidates rejected as not being the portfolio.

:func:`read_13f_documents` is the way back: the same three files, read from the
store instead of EDGAR, which is what ``backfill --force`` reparses.
"""

from __future__ import annotations

import asyncio
from typing import Final

from app.ingestion.edgar.documents import (
    FilingDocuments,
    FilingDocumentsError,
    is_cover_page,
    is_information_table,
)
from app.storage.raw import (
    DEFAULT_CONTENT_TYPE,
    RawObjectNotFoundError,
    RawStore,
    thirteen_f_key,
    thirteen_f_prefix,
)

#: By suffix, because EDGAR's own ``type`` field on a directory item is an icon
#: name. Anything not listed is one of the XML documents.
_CONTENT_TYPES: Final = {".json": "application/json"}

#: The directory listing's filename, which EDGAR fixes and the archive keeps.
_INDEX_NAME: Final = "index.json"


async def archive_13f_documents(
    store: RawStore,
    documents: FilingDocuments,
    *,
    cik: str,
    accession_no: str,
    overwrite: bool = False,
) -> str:
    """Write every document of one filing to ``store``.

    :param cik: The CIK whose archive directory the documents were fetched
        from — EDGAR's path CIK, so the key says where the bytes came from.
    :param overwrite: Replace documents already archived. Off by default: the
        first copy of a filing is the one worth keeping, and a routine re-run
        should not swap it for whatever EDGAR serves today.
    :returns: The filing's prefix, which names all of its documents at once.

    The puts run concurrently and all of them are allowed to finish; the first
    failure is then raised as itself. Itself rather than wrapped in an
    ``ExceptionGroup``, which is what a ``TaskGroup`` would produce and what the
    CLI's handling of :class:`~app.storage.raw.RawStoreError` would not match —
    an archive outage has to read as one line, not as a traceback.
    """
    fetched = [(documents.index_url, documents.index)]
    fetched.append((documents.primary_doc_url, documents.primary_doc))
    if documents.info_table_url is not None and documents.info_table is not None:
        fetched.append((documents.info_table_url, documents.info_table))

    puts = []
    for url, body in fetched:
        filename = url.rsplit("/", 1)[-1]
        key = thirteen_f_key(cik, accession_no, filename)
        puts.append(store.put(key, body, _content_type(filename), overwrite=overwrite))

    outcomes = await asyncio.gather(*puts, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return thirteen_f_prefix(cik, accession_no)


async def read_13f_documents(
    store: RawStore, prefix: str, *, directory_url: str
) -> FilingDocuments:
    """One filing's documents as :func:`archive_13f_documents` stored them.

    Told apart by root element rather than by name, the same test the fetcher
    applied before archiving them: the information table's filename is the
    filing agent's choice, and the cover page is only ``primary_doc.xml`` by
    convention.

    :param prefix: The filing's archive prefix, as ``filing.raw_key`` holds it.
    :param directory_url: The EDGAR directory the documents came from. The
        returned URLs are it plus each archived filename, which is what they
        were when fetched.
    :raises RawObjectNotFoundError: Nothing is archived under ``prefix``, or
        its ``index.json`` is missing.
    :raises FilingDocumentsError: The archive holds no single cover page, or
        more than one information table. Refused rather than guessed at, for
        the reason :mod:`app.ingestion.edgar.documents` confirms what it fetches:
        the wrong file parses to an empty portfolio without raising.
    """
    keys = await store.list(prefix)
    if not keys:
        raise RawObjectNotFoundError(prefix)
    index_key = prefix + _INDEX_NAME
    if index_key not in keys:
        raise RawObjectNotFoundError(index_key)

    # One at a time: two or three small files, and a failure raised as itself.
    index = await store.get(index_key)
    documents = {key.removeprefix(prefix): await store.get(key) for key in keys if key != index_key}

    covers = [name for name, body in documents.items() if is_cover_page(body)]
    tables = [name for name, body in documents.items() if is_information_table(body)]
    if len(covers) != 1:
        raise FilingDocumentsError(
            f"{prefix}: expected one archived cover page, found {covers or 'none'} "
            f"among {sorted(documents)}"
        )
    if len(tables) > 1:
        raise FilingDocumentsError(f"{prefix}: more than one archived information table: {tables}")

    [cover] = covers
    table = tables[0] if tables else None
    return FilingDocuments(
        index_url=f"{directory_url}/{_INDEX_NAME}",
        index=index,
        primary_doc_url=f"{directory_url}/{cover}",
        primary_doc=documents[cover],
        info_table_url=None if table is None else f"{directory_url}/{table}",
        info_table=None if table is None else documents[table],
    )


def _content_type(filename: str) -> str:
    suffix = filename[filename.rfind(".") :].casefold() if "." in filename else ""
    return _CONTENT_TYPES.get(suffix, DEFAULT_CONTENT_TYPE)
