"""Reading the House Clerk's yearly index, and fetching and archiving its ZIP.

The parser against XML in the real file's shape (tests/house_index_fixtures.py);
the client over a real socket through :class:`LocalFetcher`, as the Senate
client's tests do, so the request that is tested is the one the handler sends.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pytest

from app.ingestion.congress.house_clerk import (
    HouseClerk,
    HouseClerkError,
    IndexNotFound,
    index_url,
)
from app.ingestion.congress.house_index import (
    HouseIndexError,
    index_xml,
    parse_house_index,
)
from app.ingestion.remote.fetcher import FetcherUnavailable, FetchRequest, LocalFetcher
from app.storage.raw import LocalRawStore, RawStore, house_index_key
from tests.fake_http import FakeHttpServer, Reply, Seen
from tests.house_index_fixtures import house_xml, house_zip, member

NOW: Final = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
USER_AGENT: Final = "WhaleWatch ops@whalewatch.io"


# --- the parser ------------------------------------------------------------------


def test_only_ptrs_are_kept_and_every_type_is_counted() -> None:
    index = parse_house_index(
        house_xml(
            member("20033725", "Pelosi", "Nancy", filed="1/23/2026"),
            member("10072640", "Abel", filing_type="C", office="", filed=""),
            member("40003749", "Aaron", filing_type="O"),
            member("20034201", "Alford", "Mark", office="MO04", filed="3/31/2026"),
        )
    )

    assert [ptr.doc_id for ptr in index.ptrs] == ["20033725", "20034201"]
    assert index.listed == 4
    assert index.types == {"P": 2, "C": 1, "O": 1}
    pelosi = index.ptrs[0]
    assert (pelosi.last_name, pelosi.first_name, pelosi.prefix, pelosi.suffix) == (
        "Pelosi",
        "Nancy",
        "Hon.",
        None,
    )
    assert (pelosi.office, pelosi.year, pelosi.filing_date) == ("CA11", 2026, date(2026, 1, 23))


def test_a_ptr_row_missing_what_a_filing_needs_is_left_out_and_said() -> None:
    index = parse_house_index(
        house_xml(
            member("20000001", "Good"),
            member("", "NoDocId"),
            member("20000003", ""),
            member("20000004", "BadDate", filed="2026-03-02"),
            member("20000005", "BadYear", year=26),
        )
    )

    assert [ptr.doc_id for ptr in index.ptrs] == ["20000001"]
    assert [(bad.position, bad.doc_id) for bad in index.malformed] == [
        (2, None),
        (3, "20000003"),
        (4, "20000004"),
        (5, "20000005"),
    ]
    assert "M/D/YYYY" in index.malformed[2].reason


def test_a_verbatim_repeat_collapses_and_a_conflicting_one_is_malformed() -> None:
    index = parse_house_index(
        house_xml(
            member("20000001", "Same"),
            member("20000001", "Same"),
            member("20000002", "First"),
            member("20000002", "Second"),
        )
    )

    assert [(ptr.doc_id, ptr.last_name) for ptr in index.ptrs] == [
        ("20000001", "Same"),
        ("20000002", "First"),
    ]
    assert index.duplicates == 1
    assert [(bad.position, bad.doc_id) for bad in index.malformed] == [(4, "20000002")]


def test_xml_that_is_not_an_index_raises() -> None:
    with pytest.raises(HouseIndexError, match="well-formed"):
        parse_house_index(b"<FinancialDisclosure><Member>")
    with pytest.raises(HouseIndexError, match="FinancialDisclosure"):
        parse_house_index(b"<html><body>Not found</body></html>")


def test_the_index_is_read_out_of_the_zip_by_its_year() -> None:
    xml = index_xml(house_zip(2025, member("20000001", "Aderholt", year=2025)), 2025)

    assert xml.startswith(b"\xef\xbb\xbf<?xml")
    assert [ptr.year for ptr in parse_house_index(xml).ptrs] == [2025]


def test_a_download_that_is_not_a_zip_with_an_index_raises() -> None:
    with pytest.raises(HouseIndexError, match="not a readable ZIP"):
        index_xml(b"<html>maintenance</html>", 2026)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("2026FD.txt", b"Prefix\tLast\r\n")
    with pytest.raises(HouseIndexError, match=r"no 2026FD\.xml"):
        index_xml(buffer.getvalue(), 2026)


def test_the_archive_key_is_the_year_and_a_checksum() -> None:
    digest = hashlib.sha256(b"x").hexdigest()

    assert house_index_key(2026, digest) == f"raw/house-clerk/index/2026/{digest}.zip"
    with pytest.raises(ValueError, match="SHA-256"):
        house_index_key(2026, "../../etc")
    with pytest.raises(ValueError, match="four-digit"):
        house_index_key(26, digest)


# --- the client ------------------------------------------------------------------


class FakeClerk:
    """Serves whatever ZIP each year has been given; a 404 for any other."""

    def __init__(self) -> None:
        self.zips: dict[int, bytes] = {}
        self.statuses: dict[int, int] = {}

    def __call__(self, seen: Seen) -> Reply:
        year = int(seen.path.rsplit("/", 1)[-1].removesuffix("FD.zip"))
        if year in self.statuses:
            return Reply(
                self.statuses[year], b"<html>error</html>", [("Content-Type", "text/html")]
            )
        if year not in self.zips:
            return Reply(404, b"<html>404</html>", [("Content-Type", "text/html")])
        return Reply(200, self.zips[year], [("Content-Type", "application/x-zip-compressed")])


@pytest.fixture
def clerk_site() -> Iterator[tuple[FakeClerk, FakeHttpServer]]:
    fake = FakeClerk()
    with FakeHttpServer(fake) as server:
        yield fake, server


@pytest.fixture
def store(tmp_path: Path) -> LocalRawStore:
    return LocalRawStore(tmp_path)


def _clerk(server: FakeHttpServer, store: RawStore) -> HouseClerk:
    return HouseClerk(
        LocalFetcher(), store, user_agent=USER_AGENT, base_url=server.base, clock=lambda: NOW
    )


async def test_a_years_zip_is_archived_as_served_under_its_index_checksum(
    clerk_site: tuple[FakeClerk, FakeHttpServer], store: RawStore
) -> None:
    fake, server = clerk_site
    fake.zips[2026] = house_zip(2026, member("20033725", "Pelosi"))

    fetched = await _clerk(server, store).fetch_index(2026)

    digest = hashlib.sha256(fetched.xml).hexdigest()
    assert fetched.key == f"raw/house-clerk/index/2026/{digest}.zip"
    assert fetched.new
    assert await store.get(fetched.key) == fake.zips[2026]
    [request] = server.seen
    assert request.path == "/public_disc/financial-pdfs/2026FD.zip"
    assert request.headers["User-Agent"] == USER_AGENT


async def test_a_nightly_rebuild_of_an_unchanged_index_is_not_archived_again(
    clerk_site: tuple[FakeClerk, FakeHttpServer], store: RawStore
) -> None:
    fake, server = clerk_site
    clerk = _clerk(server, store)
    entry = member("20033725", "Pelosi")
    fake.zips[2026] = house_zip(2026, entry, built=datetime(2026, 11, 4, 9, 0))
    first = await clerk.fetch_index(2026)
    fake.zips[2026] = house_zip(2026, entry, built=datetime(2026, 11, 5, 9, 0))

    second = await clerk.fetch_index(2026)

    assert second.key == first.key
    assert not second.new
    assert await store.list("raw/house-clerk/") == (first.key,)


async def test_a_changed_index_is_a_second_version(
    clerk_site: tuple[FakeClerk, FakeHttpServer], store: RawStore
) -> None:
    fake, server = clerk_site
    clerk = _clerk(server, store)
    fake.zips[2026] = house_zip(2026, member("20033725", "Pelosi"))
    first = await clerk.fetch_index(2026)
    fake.zips[2026] = house_zip(2026, member("20033725", "Pelosi"), member("20034201", "Alford"))

    second = await clerk.fetch_index(2026)

    assert second.new
    assert await store.list("raw/house-clerk/index/2026/") == tuple(sorted((first.key, second.key)))


async def test_a_404_is_index_not_found_and_another_status_an_error(
    clerk_site: tuple[FakeClerk, FakeHttpServer], store: RawStore
) -> None:
    fake, server = clerk_site
    fake.statuses[2025] = 503
    clerk = _clerk(server, store)

    with pytest.raises(IndexNotFound):
        await clerk.fetch_index(2027)
    with pytest.raises(HouseClerkError) as raised:
        await clerk.fetch_index(2025)

    assert raised.value.status == 503
    assert await store.list("raw/house-clerk/") == ()


async def test_a_200_that_is_not_a_zip_raises_and_archives_nothing(
    clerk_site: tuple[FakeClerk, FakeHttpServer], store: RawStore
) -> None:
    fake, server = clerk_site
    fake.zips[2026] = b"<html>We are down for maintenance</html>"

    with pytest.raises(HouseIndexError):
        await _clerk(server, store).fetch_index(2026)

    assert await store.list("raw/house-clerk/") == ()


class _Down:
    backend = "lambda"

    async def fetch(self, request: FetchRequest, cookies: object = ()) -> object:
        raise FetcherUnavailable("the US fetcher is unreachable: invoking failed")


async def test_an_unreachable_fetcher_propagates(store: RawStore) -> None:
    clerk = HouseClerk(_Down(), store, user_agent=USER_AGENT)  # type: ignore[arg-type]

    with pytest.raises(FetcherUnavailable):
        await clerk.fetch_index(2026)


def test_the_index_url_is_the_clerks() -> None:
    assert index_url(2026) == (
        "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip"
    )
