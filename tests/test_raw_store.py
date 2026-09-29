"""The raw store's contract, held to by both implementations.

Every behavioural test here runs twice: against :class:`LocalRawStore` in a temp
directory, and against :class:`S3RawStore` talking to a moto server over HTTP.
One suite rather than two, because the point of the protocol is that code
written against the local store in development behaves the same against the
bucket in production — and two suites drift into testing two contracts.

The S3 half goes through :func:`open_raw_store` with an endpoint URL, which is
exactly how an R2 deployment is configured, so what passes here is the code path
production takes and not a mock of it. It is skipped when moto is not installed.

The assertions that matter most are about bytes. "Stored as received" is the
property a parser fix depends on, so it is tested with the documents most likely
to be quietly altered on the way through: a gzip stream, CRLF line endings, a
byte-order mark, and content that is not valid UTF-8 at all.
"""

import gzip
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import botocore.session
import pytest

from app.core.config import Settings
from app.storage.raw import (
    LocalRawStore,
    RawObjectNotFoundError,
    RawStore,
    open_raw_store,
    thirteen_f_key,
    thirteen_f_prefix,
    validate_key,
)
from tests.conftest import make_settings

ACCESSION: Final = "0001067983-24-000011"
CIK: Final = "0001067983"
KEY: Final = f"raw/13f/{CIK}/{ACCESSION}/primary_doc.xml"

_REGION: Final = "us-east-1"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="session")
def moto_endpoint() -> Iterator[str]:
    """One moto S3 server for the session, on a port the OS picks."""
    server_module = pytest.importorskip("moto.server")
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_settings(moto_endpoint: str) -> Settings:
    """Settings for a bucket of this test's own, created empty.

    The bucket is made with plain botocore, synchronously, because this fixture
    is resolved from inside the async ``store`` fixture and cannot start an
    event loop of its own there.
    """
    bucket = f"raw-{uuid.uuid4().hex[:12]}"
    _admin_client(moto_endpoint).create_bucket(Bucket=bucket)
    return make_settings(
        raw_store_backend="s3",
        raw_store_s3_bucket=bucket,
        raw_store_s3_endpoint_url=moto_endpoint,
        raw_store_s3_region=_REGION,
        raw_store_s3_access_key_id="testing",
        raw_store_s3_secret_access_key="testing",
    )


def _admin_client(endpoint: str) -> Any:
    """A synchronous client on the moto server, for setting up and inspecting."""
    return botocore.session.get_session().create_client(
        "s3",
        endpoint_url=endpoint,
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture(params=["local", "s3"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[RawStore]:
    """Each backend, opened the way the application opens it."""
    if request.param == "local":
        settings = make_settings(raw_store_backend="local", raw_store_local_root=tmp_path)
    else:
        settings = request.getfixturevalue("s3_settings")
    async with open_raw_store(settings) as opened:
        yield opened


# --- round trips -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<edgarSubmission/>", id="xml"),
        pytest.param(gzip.compress(b"<informationTable/>"), id="gzip-not-decompressed"),
        pytest.param(b"<a>\r\n<b/>\r\n</a>\r\n", id="crlf-not-normalised"),
        pytest.param(b"\xef\xbb\xbf<edgarSubmission/>", id="bom-kept"),
        pytest.param(b"<name>Soci\xe9t\xe9 G\xe9n\xe9rale</name>", id="latin-1-not-decoded"),
        pytest.param(b"", id="empty"),
    ],
)
async def test_what_goes_in_comes_out_byte_for_byte(store: RawStore, body: bytes) -> None:
    assert await store.put(KEY, body) == KEY

    assert await store.get(KEY) == body


async def test_exists_reports_what_has_been_put(store: RawStore) -> None:
    assert not await store.exists(KEY)

    await store.put(KEY, b"<edgarSubmission/>")

    assert await store.exists(KEY)


async def test_getting_a_key_never_put_says_so(store: RawStore) -> None:
    with pytest.raises(RawObjectNotFoundError) as missing:
        await store.get(KEY)

    assert missing.value.key == KEY


# --- write once --------------------------------------------------------------


async def test_a_second_put_leaves_the_first_copy_alone(store: RawStore) -> None:
    """The first copy is the one worth keeping: a re-run must not replace a
    document with whatever EDGAR serves today."""
    await store.put(KEY, b"as first fetched")

    assert await store.put(KEY, b"as restated later") == KEY

    assert await store.get(KEY) == b"as first fetched"


async def test_overwrite_replaces_it(store: RawStore) -> None:
    """What ``--force`` asks for."""
    await store.put(KEY, b"as first fetched")

    await store.put(KEY, b"as restated later", overwrite=True)

    assert await store.get(KEY) == b"as restated later"


# --- listing -----------------------------------------------------------------


async def test_list_returns_every_key_under_a_prefix_sorted(store: RawStore) -> None:
    prefix = thirteen_f_prefix(CIK, ACCESSION)
    for name in ("primary_doc.xml", "index.json", "56757.xml"):
        await store.put(prefix + name, b"x")
    await store.put(thirteen_f_key(CIK, "0001067983-24-000099", "primary_doc.xml"), b"x")

    assert await store.list(prefix) == (
        f"{prefix}56757.xml",
        f"{prefix}index.json",
        f"{prefix}primary_doc.xml",
    )


async def test_a_prefix_is_a_string_prefix_not_a_directory(store: RawStore) -> None:
    """As S3 means it, and so as the local store must too: a prefix that ends
    mid-segment matches every key that continues it."""
    first = thirteen_f_key(CIK, "0001067983-24-000011", "primary_doc.xml")
    second = thirteen_f_key(CIK, "0001067983-24-000012", "primary_doc.xml")
    other_year = thirteen_f_key(CIK, "0001067983-23-000011", "primary_doc.xml")
    for key in (first, second, other_year):
        await store.put(key, b"x")

    assert await store.list(f"raw/13f/{CIK}/0001067983-24") == (first, second)


async def test_list_with_no_prefix_returns_everything(store: RawStore) -> None:
    await store.put(KEY, b"x")

    assert await store.list() == (KEY,)


async def test_an_empty_prefix_lists_nothing_rather_than_failing(store: RawStore) -> None:
    assert await store.list("raw/13f/0000000001/") == ()


# --- keys --------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/raw/13f/x.xml",
        "raw/../../etc/passwd",
        "raw/./x.xml",
        "raw//x.xml",
        "raw\\13f\\x.xml",
        "raw/13f/.hidden",
    ],
)
async def test_a_key_that_is_not_a_plain_relative_name_is_refused(
    store: RawStore, key: str
) -> None:
    """Refused by both stores, not just by the one where a key is a path —
    otherwise a key that works in production escapes the directory in
    development."""
    with pytest.raises(ValueError, match="not a raw store key"):
        await store.put(key, b"x")


def test_the_key_scheme() -> None:
    assert thirteen_f_key(CIK, ACCESSION, "primary_doc.xml") == KEY


def test_the_key_is_the_same_whatever_spelling_the_identifiers_arrive_in() -> None:
    """The same filing reaching two prefixes is two archives that each look
    incomplete."""
    assert thirteen_f_key(1067983, "000106798324000011", "primary_doc.xml") == KEY
    assert thirteen_f_key("1067983", ACCESSION, "primary_doc.xml") == KEY


@pytest.mark.parametrize("filename", ["", "xslForm13F_X02/primary_doc.xml", ".."])
def test_a_filename_must_be_one_segment(filename: str) -> None:
    with pytest.raises(ValueError, match="not a raw store key"):
        thirteen_f_key(CIK, ACCESSION, filename)


@pytest.mark.parametrize(("cik", "accession"), [("12345678901", ACCESSION), (CIK, "24-000011")])
def test_a_malformed_cik_or_accession_number_is_refused(cik: str, accession: str) -> None:
    with pytest.raises(ValueError):
        thirteen_f_prefix(cik, accession)


def test_validate_key_returns_what_it_accepts() -> None:
    assert validate_key(KEY) == KEY


# --- backend specifics -------------------------------------------------------


async def test_the_local_store_lays_keys_out_as_paths_under_its_root(tmp_path: Path) -> None:
    """So the default root of ./data puts documents in ./data/raw/13f/…, the
    layout the bucket has."""
    store = LocalRawStore(tmp_path)

    await store.put(KEY, b"<edgarSubmission/>")

    assert (tmp_path / KEY).read_bytes() == b"<edgarSubmission/>"


async def test_the_local_store_does_not_list_a_write_in_progress(tmp_path: Path) -> None:
    """A run killed mid-write leaves its temporary file behind; it is not a
    document and must not come back from a listing as one."""
    store = LocalRawStore(tmp_path)
    await store.put(KEY, b"x")
    (tmp_path / KEY).with_name(".primary_doc.xml.abc123.partial").write_bytes(b"half")

    assert await store.list() == (KEY,)


async def test_the_s3_store_keeps_the_content_type(s3_settings: Settings) -> None:
    async with open_raw_store(s3_settings) as store:
        await store.put(KEY.replace("primary_doc.xml", "index.json"), b"{}", "application/json")

    head = _admin_client(str(s3_settings.raw_store_s3_endpoint_url)).head_object(
        Bucket=str(s3_settings.raw_store_s3_bucket),
        Key=KEY.replace("primary_doc.xml", "index.json"),
    )

    assert head["ContentType"] == "application/json"
    # Stored as received means no Content-Encoding either: nothing downstream
    # should ever be invited to decompress what EDGAR sent.
    assert "ContentEncoding" not in head


async def test_the_backend_is_chosen_by_settings(tmp_path: Path, s3_settings: Settings) -> None:
    local = make_settings(raw_store_backend="local", raw_store_local_root=tmp_path)
    async with open_raw_store(local) as store:
        assert isinstance(store, LocalRawStore)

    from app.storage.s3 import S3RawStore

    async with open_raw_store(s3_settings) as store:
        assert isinstance(store, S3RawStore)
