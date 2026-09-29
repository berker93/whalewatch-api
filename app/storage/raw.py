"""The raw document archive: every EDGAR document we fetch, exactly as served.

This is the layer that makes a parser bug cheap. Parsing is a pure function from
bytes to rows, so once the bytes are kept, a bug found in week eight is fixed by
re-running the parser over what is already here — minutes over local storage —
rather than by re-crawling two thousand filings at ten requests a second. It is
also the only copy we control: EDGAR can restate, move or withdraw a document,
and a filing fetched once stays fetched.

Hence the rules every implementation keeps:

- **Stored as received.** No decompression, no re-encoding, no normalising line
  endings. The bytes that come back from :meth:`RawStore.get` are the bytes
  EDGAR sent, so "did the document change or did our parser?" has an answer.
- **Write once.** :meth:`RawStore.put` of a key that already exists does
  nothing, unless the caller passes ``overwrite=True`` (``--force`` on the
  command line). The first copy is the one worth keeping; a re-run should not
  quietly replace it with whatever EDGAR serves today.
- **Archive before parse.** Not enforced here, but the reason this module
  exists: the caller writes the bytes, *then* parses them, so a parser that
  crashes has nothing left to lose.

Keys
----
``raw/13f/{cik}/{accession_no}/{filename}`` — the ten-digit CIK whose archive
directory the filing was fetched from, the dashed accession number, and EDGAR's
own filename. Built only by :func:`thirteen_f_key`, so the layout has one home.
Every document of one filing shares a prefix (:func:`thirteen_f_prefix`), which
is what :meth:`RawStore.list` is for.

Two implementations, chosen by ``RAW_STORE_BACKEND``: :class:`LocalRawStore`
under ``./data`` for development, and :class:`~app.storage.s3.S3RawStore` for
any S3-compatible bucket, Cloudflare R2 included. Get one with
:func:`open_raw_store`, never by constructing either directly.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final, Protocol

from app.core.accession import normalise_accession
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The content type a document is stored with when the caller does not say.
#: Both documents of a 13F are XML; ``index.json`` is the exception and says so.
DEFAULT_CONTENT_TYPE: Final = "application/xml"

#: Suffix of a local write in progress. Never a key: :func:`validate_key`
#: refuses a segment that starts with a dot, which every temporary file does.
_PARTIAL_SUFFIX: Final = ".partial"


class RawStoreError(Exception):
    """The archive could not do what was asked: unreachable, refused, full.

    One type for both backends, so a caller can treat "the archive is down" as
    one operational failure without knowing whether that meant ``OSError`` or a
    botocore ``ClientError`` today.
    """


class RawObjectNotFoundError(RawStoreError, LookupError):
    """:meth:`RawStore.get` of a key that has nothing stored under it."""

    def __init__(self, key: str) -> None:
        super().__init__(f"nothing archived at {key}")
        self.key = key


class RawStore(Protocol):
    """Object storage for raw documents, keyed by :func:`thirteen_f_key`."""

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = DEFAULT_CONTENT_TYPE,
        *,
        overwrite: bool = False,
    ) -> str:
        """Store ``data`` under ``key``, unless something already is.

        :returns: ``key``, so a caller can write and record in one expression.
        """
        ...

    async def get(self, key: str) -> bytes:
        """The bytes stored under ``key``.

        :raises RawObjectNotFoundError: If there are none.
        """
        ...

    async def exists(self, key: str) -> bool: ...

    async def list(self, prefix: str = "") -> tuple[str, ...]:
        """Every key starting with ``prefix``, sorted.

        A string prefix, as S3 means it, not a directory:
        ``raw/13f/0001067983/`` lists one filer, and ``raw/13f/0001067983/0001067983-24``
        lists that filer's 2024 filings transmitted under its own CIK.
        """
        ...


# --- keys --------------------------------------------------------------------


def thirteen_f_prefix(cik: int | str, accession_no: str) -> str:
    """``raw/13f/0001067983/0001067983-24-000011/`` — one filing's documents.

    The CIK is padded and the accession number dashed whatever spelling they
    arrive in, because the same filing reaching two prefixes is two archives
    that each look incomplete.

    :raises ValueError: If either is not the shape it should be.
    """
    digits = str(cik).strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"{cik!r} is not a CIK: expected up to ten digits")
    return f"raw/13f/{int(digits):010d}/{normalise_accession(accession_no)}/"


def thirteen_f_key(cik: int | str, accession_no: str, filename: str) -> str:
    """``raw/13f/{cik}/{accession_no}/{filename}``, the key for one document.

    ``filename`` is EDGAR's name for the file, unaltered — ``56757.xml`` stays
    ``56757.xml`` — so the archive can be read against the directory listing
    stored beside it.

    :raises ValueError: If ``filename`` would not be a single path segment.
    """
    if "/" in filename:
        # validate_key would accept it as two segments; a filename is one.
        raise ValueError(f"{filename!r} is not a raw store key segment")
    return validate_key(thirteen_f_prefix(cik, accession_no) + filename)


def validate_key(key: str) -> str:
    """``key``, if it is a relative, slash-separated name with no tricks in it.

    Enforced by both stores rather than trusted to callers, because on the local
    one a key *is* a path: ``raw/../../etc/passwd`` must not be an object key,
    and a store that accepts it on S3 and refuses it on disk has two behaviours.

    :raises ValueError: If it is empty, absolute, uses a backslash, or has an
        empty segment or one that starts with a dot.
    """
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError(f"{key!r} is not a raw store key")
    for segment in key.split("/"):
        if not segment or segment.startswith("."):
            raise ValueError(f"{key!r} is not a raw store key: bad segment {segment!r}")
    return key


def validate_prefix(prefix: str) -> str:
    """A prefix may be empty or end mid-segment, which a key may not."""
    if prefix:
        # A trailing partial segment is fine ("raw/13f/00010"); validate the
        # whole thing as though it were a key with that segment completed.
        validate_key(prefix.rstrip("/") or "/")
    return prefix


# --- local -------------------------------------------------------------------


class LocalRawStore:
    """A :class:`RawStore` on the local filesystem, for development.

    Keys map onto paths under ``root`` segment by segment, so the default root
    of ``./data`` puts every document in ``./data/raw/13f/...`` — the layout
    the bucket has, which is what makes ``aws s3 sync`` between the two a copy.

    ``content_type`` is accepted and discarded: a file has no content type, and
    the suffix EDGAR gave the file already says what it is.

    File I/O runs in a worker thread, because this is called from the same event
    loop that is pacing EDGAR requests, and a blocking write stalls all of them.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = DEFAULT_CONTENT_TYPE,
        *,
        overwrite: bool = False,
    ) -> str:
        path = self._path(key)
        written = await _in_thread(_write_file, path, data, overwrite)
        log_put(key, data, written=written)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise RawObjectNotFoundError(key) from None
        except OSError as failure:
            raise RawStoreError(f"could not read {path}: {failure}") from failure

    async def exists(self, key: str) -> bool:
        return await _in_thread(self._path(key).is_file)

    async def list(self, prefix: str = "") -> tuple[str, ...]:
        return await _in_thread(self._keys_under, validate_prefix(prefix))

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*validate_key(key).split("/"))

    def _keys_under(self, prefix: str) -> tuple[str, ...]:
        """Walk only the directory the prefix names, then filter by the rest.

        ``raw/13f/0001067983/0001067983-24`` walks ``raw/13f/0001067983`` and
        keeps what matches, rather than walking the whole archive to find one
        filer's filings in it.
        """
        directory = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        base = self._root.joinpath(*directory.split("/")) if directory else self._root
        if not base.is_dir():
            return ()
        keys = (
            path.relative_to(self._root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )
        return tuple(sorted(key for key in keys if key.startswith(prefix)))


def _write_file(path: Path, data: bytes, overwrite: bool) -> bool:
    """Write ``data`` to ``path`` unless it exists. Returns whether it wrote.

    Via a temporary file in the same directory and a rename, so a run killed
    mid-write leaves either the whole document or none of it — never a
    truncated one that a later ``put`` would then refuse to replace.

    Two writers racing for one key can both see it absent and both rename; the
    second wins. Harmless, because one key is one EDGAR document and both hold
    its bytes.
    """
    if not overwrite and path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=_PARTIAL_SUFFIX
    )
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise
    return True


async def _in_thread[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """``asyncio.to_thread``, with the filesystem's failures in this module's terms."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except OSError as failure:
        raise RawStoreError(str(failure)) from failure


def log_put(key: str, data: bytes, *, written: bool) -> None:
    """One line per document, so a backfill's log shows what it archived.

    Debug for a skip, because on a resumed backfill that is nearly every line.
    """
    if written:
        logger.info("raw_store.put", key=key, bytes=len(data))
    else:
        logger.debug("raw_store.put_skipped", key=key, reason="exists")


# --- selection ---------------------------------------------------------------


@asynccontextmanager
async def open_raw_store(settings: Settings) -> AsyncIterator[RawStore]:
    """The store ``RAW_STORE_BACKEND`` names, open for the duration of the block.

    A context manager even though the local store needs no closing, because the
    S3 one holds a connection pool and a caller written against the local store
    must not leak it the day the setting changes::

        async with open_raw_store(settings) as store:
            await store.put(key, body)
    """
    if settings.raw_store_backend == "local":
        yield LocalRawStore(settings.raw_store_local_root)
        return

    # Imported here so that a development setup never loads botocore.
    from app.storage.s3 import connect_s3_raw_store

    async with connect_s3_raw_store(settings) as store:
        yield store
