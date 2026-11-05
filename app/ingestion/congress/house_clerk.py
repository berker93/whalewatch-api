"""The House Clerk's yearly disclosure ZIPs, through the US fetcher, archived.

``https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip``:
every financial disclosure a member or candidate filed under that year, as an
index (:mod:`app.ingestion.congress.house_index`). From 2008. A year the Clerk
has not started is a 404 — the current year's, in the first days of January.

No conditional GET: the Clerk rebuilds every year's ZIP each night (every
``Last-Modified`` is the same minute), and does not answer ``If-None-Match``
with a 304 anyway. A year is one download of 20 to 110 KB, so it is fetched
whole and its index compared by checksum.

Archive before parse
--------------------
The ZIP is written to the raw store, as served, before anything reads its
rows: :func:`~app.storage.raw.house_index_key`, under the year and the SHA-256
of the XML inside. Write-once, so an index that has not changed since the last
fetch is already there and is not written again. Opening the ZIP to find that
checksum is the one thing done first, and a download that is not a ZIP with an
index in it raises and is not archived — the Senate client does the same with
an answer that is not what was asked for.

Nothing falls back to the archive when the Clerk cannot be reached. The
fetcher's failures propagate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.core.logging import get_logger
from app.ingestion.congress.house_index import index_xml
from app.ingestion.remote.fetcher import Fetcher, FetchRequest
from app.storage.raw import RawStore, house_index_key

logger = get_logger(__name__)

HOUSE_CLERK_BASE: Final = "https://disclosures-clerk.house.gov"

#: The first year the Clerk publishes an index for.
FIRST_YEAR: Final = 2008

_ZIP_CONTENT_TYPE: Final = "application/zip"


class HouseClerkError(Exception):
    """The Clerk answered, and not with a year's ZIP."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class IndexNotFound(HouseClerkError):  # noqa: N818
    """A 404 for the year: not started yet, if it is the current one."""


@dataclass(frozen=True, slots=True)
class ArchivedIndex:
    """One year's ZIP, where it was archived, and the XML index out of it."""

    year: int
    key: str
    sha256: str
    """Of :attr:`xml`, which is what the key is named by."""
    xml: bytes
    size: int
    """Of the ZIP, as served."""
    new: bool
    """Whether this fetch wrote it: ``False`` when the archive already had
    this version of the index."""
    fetched_at: datetime


def index_url(year: int, base_url: str = HOUSE_CLERK_BASE) -> str:
    return f"{base_url.rstrip('/')}/public_disc/financial-pdfs/{year}FD.zip"


class HouseClerk:
    """Fetches and archives the Clerk's yearly ZIPs. Stateless: no session."""

    def __init__(
        self,
        fetcher: Fetcher,
        store: RawStore,
        *,
        user_agent: str,
        base_url: str = HOUSE_CLERK_BASE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._user_agent = user_agent
        self._base = base_url
        self._clock = clock

    async def fetch_index(self, year: int) -> ArchivedIndex:
        """``year``'s ZIP, archived, with the XML index read out of it.

        :raises IndexNotFound: The Clerk has no ZIP for ``year``.
        :raises HouseClerkError: Any other status.
        :raises HouseIndexError: The download is not a ZIP with an index in it.
        :raises FetcherError: As the fetcher raises it.
        """
        url = index_url(year, self._base)
        response = await self._fetcher.fetch(
            FetchRequest(url=url, headers=[("User-Agent", self._user_agent), ("Accept", "*/*")])
        )
        if response.status == 404:
            raise IndexNotFound(f"the House Clerk has no {year} index", status=404)
        if response.status != 200:
            raise HouseClerkError(
                f"the House Clerk answered {response.status} for the {year} index",
                status=response.status,
            )
        xml = index_xml(response.body, year)
        digest = hashlib.sha256(xml).hexdigest()
        key = house_index_key(year, digest)
        new = not await self._store.exists(key)
        if new:
            await self._store.put(key, response.body, _ZIP_CONTENT_TYPE)
        logger.info(
            "house_clerk.index_fetched", year=year, bytes=len(response.body), key=key, new=new
        )
        return ArchivedIndex(
            year=year,
            key=key,
            sha256=digest,
            xml=xml,
            size=len(response.body),
            new=new,
            fetched_at=self._clock(),
        )
