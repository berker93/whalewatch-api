"""The raw store on S3 — or on anything that speaks its API, R2 included.

Nothing here is AWS-specific on purpose. Cloudflare R2 is reached by setting
``RAW_STORE_S3_ENDPOINT_URL`` to the account's ``r2.cloudflarestorage.com`` host
and ``RAW_STORE_S3_REGION`` to ``auto``; MinIO and moto the same way. The code
path is identical, which is why the tests exercise it through an endpoint URL
rather than through an in-process mock of AWS.

Checksums, and the one setting that is not the default
------------------------------------------------------
Recent botocore attaches a CRC32 integrity checksum to every upload by default
and asks for one back on every download. AWS supports that; S3-compatible stores
have lagged behind it and rejected or mishandled uploads that carried one. Both
are set to ``when_required`` here, which is botocore's behaviour before the
change and what every S3-compatible store accepts. The integrity check this
gives up is TLS's to provide anyway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Final

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.storage.raw import (
    DEFAULT_CONTENT_TYPE,
    RawObjectNotFoundError,
    RawStoreError,
    log_put,
    validate_key,
    validate_prefix,
)

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

#: Error codes S3 answers "no such object" with. ``GetObject`` says
#: ``NoSuchKey``; ``HeadObject`` has no body to say anything in, so botocore
#: reports the bare status.
_NOT_FOUND_CODES: Final = frozenset({"NoSuchKey", "404", "NotFound"})

_CLIENT_CONFIG: Final = AioConfig(
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
    retries={"max_attempts": 5, "mode": "standard"},
)


class S3RawStore:
    """A :class:`~app.storage.raw.RawStore` over one bucket.

    Takes an open client rather than building one, so the connection pool's
    lifetime belongs to :func:`connect_s3_raw_store` and a test can hand in a
    client pointed wherever it likes.
    """

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = DEFAULT_CONTENT_TYPE,
        *,
        overwrite: bool = False,
    ) -> str:
        """Upload ``data`` unless ``key`` exists — a HEAD, then a PUT.

        Not a conditional ``If-None-Match`` PUT, though that would be one
        request instead of two: support for it is recent and uneven across
        S3-compatible stores, and the race it closes is harmless here — two
        writers of one key are writing one EDGAR document's bytes.
        """
        validate_key(key)
        if not overwrite and await self.exists(key):
            log_put(key, data, written=False)
            return key
        with _translated(key):
            await self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        log_put(key, data, written=True)
        return key

    async def get(self, key: str) -> bytes:
        validate_key(key)
        with _translated(key):
            response = await self._client.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as body:
                return await body.read()

    async def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            with _translated(key):
                await self._client.head_object(Bucket=self._bucket, Key=key)
        except RawObjectNotFoundError:
            return False
        return True

    async def list(self, prefix: str = "") -> tuple[str, ...]:
        validate_prefix(prefix)
        keys: list[str] = []
        with _translated(prefix):
            paginator = self._client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys.extend(item["Key"] for item in page.get("Contents", ()) if "Key" in item)
        # S3 lists in UTF-8 byte order already; sorted anyway so both stores
        # promise the same order without relying on a store's documentation.
        return tuple(sorted(keys))


@asynccontextmanager
async def connect_s3_raw_store(settings: Settings) -> AsyncIterator[S3RawStore]:
    """An :class:`S3RawStore` for the bucket in ``settings``, closed on exit."""
    if settings.raw_store_s3_bucket is None:
        # Settings refuses this combination; restated for the type checker and
        # for anyone constructing Settings with validation bypassed.
        raise RawStoreError("RAW_STORE_S3_BUCKET is not set")
    secret = settings.raw_store_s3_secret_access_key
    async with get_session().create_client(
        "s3",
        endpoint_url=settings.raw_store_s3_endpoint_url,
        region_name=settings.raw_store_s3_region,
        aws_access_key_id=settings.raw_store_s3_access_key_id,
        aws_secret_access_key=None if secret is None else secret.get_secret_value(),
        config=_CLIENT_CONFIG,
    ) as client:
        yield S3RawStore(client, settings.raw_store_s3_bucket)


@contextmanager
def _translated(key: str) -> Iterator[None]:
    """botocore's failures, as :class:`RawStoreError` and its not-found subclass."""
    try:
        yield
    except ClientError as failure:
        code = str(failure.response.get("Error", {}).get("Code", ""))
        if code in _NOT_FOUND_CODES:
            raise RawObjectNotFoundError(key) from None
        raise RawStoreError(f"{key}: {failure}") from failure
    except BotoCoreError as failure:
        raise RawStoreError(f"{key}: {failure}") from failure
