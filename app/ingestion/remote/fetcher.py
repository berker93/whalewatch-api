"""The worker's side of the US-region fetcher: a narrow interface and two backends.

Everything that has to be fetched from a US address — Senate eFD, the House
Clerk — goes through :class:`Fetcher`, which does one thing: fetch a URL,
return the bytes, hand back the cookie jar. What the bytes mean, and whether a
403 is a block or a missing page, is the caller's to decide; this layer only
reports what the target answered.

Two backends, chosen by ``US_FETCHER_BACKEND``:

- ``lambda``: :class:`LambdaFetcher` invokes the function in
  ``infra/us-fetcher/`` and reads the body back from its spool bucket.
- ``local``: :class:`LocalFetcher` runs the same handler code in this process,
  from this machine's address. For development and tests; refused in staging
  and production, where it would be the non-US address the whole point is to
  avoid.

Unset, there is no fetcher, and :func:`open_fetcher` says so.

Failing loudly
--------------
There is no fallback, on purpose. A fetcher that cannot be reached raises
:class:`FetcherUnavailable` and the run stops; nothing quietly retries from the
worker's own address, and nothing serves an archived copy in place of a fresh
one. A disclosure feed that is silently a day stale is worse than one that is
visibly down, because the first is shown to users as current.

Three failures, kept apart because they want different answers:

=============================  ==============================================
:class:`FetcherUnavailable`    The fetcher itself: not configured, cannot be
                               invoked, crashed, timed out, misdeployed, or
                               spooled a body that does not check out. Stop
                               and fix the infrastructure.
-----------------------------  ----------------------------------------------
:class:`UpstreamUnreachable`   The fetcher ran; the target did not answer
                               (DNS, connect, TLS, timeout). Usually the
                               target having a moment — the caller may retry.
-----------------------------  ----------------------------------------------
:class:`FetchRefused`          The fetcher declined: a host not on its
                               allow-list, a body over its cap, a malformed
                               request. A bug or a config gap, not transient.
=============================  ==============================================

An HTTP status of any kind is none of these. A 503 from eFD is a
:class:`FetchResponse` with ``status=503``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlencode

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.remote import handler
from app.ingestion.remote.handler import CookieRecord

if TYPE_CHECKING:
    from types_aiobotocore_lambda import LambdaClient
    from types_aiobotocore_s3 import S3Client

logger = get_logger(__name__)

#: Hosts the local backend may fetch from: the same list the Lambda is deployed
#: with (``infra/us-fetcher/template.yaml``), so a request that works locally is
#: not refused once deployed. ``127.0.0.1`` and ``localhost`` are for tests.
LOCAL_ALLOWED_HOSTS: Final = (
    "efdsearch.senate.gov",
    "disclosures-clerk.house.gov",
    "checkip.amazonaws.com",
    "127.0.0.1",
    "localhost",
)

#: Answers with the caller's address and nothing else: what ``fetcher-check``
#: asks, to show which address the fetcher really leaves from.
CHECKIP_URL: Final = "https://checkip.amazonaws.com/"

#: Longer than the function's own 60s timeout, so a slow fetch comes back as
#: Lambda's "Task timed out" rather than as our socket giving up first.
_INVOKE_READ_TIMEOUT_SECONDS: Final = 75

#: Lambda throttling and 5xx are retried by botocore's standard mode. Three
#: attempts, because an invocation that fails three times is not a blip.
_CLIENT_CONFIG: Final = AioConfig(
    read_timeout=_INVOKE_READ_TIMEOUT_SECONDS,
    connect_timeout=10,
    retries={"max_attempts": 3, "mode": "standard"},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)


class FetcherError(Exception):
    """Base of the three failures in the module docstring."""


class FetcherUnavailable(FetcherError):  # noqa: N818
    """The fetcher could not be used. Never answered by falling back."""


class UpstreamUnreachable(FetcherError):  # noqa: N818
    """The fetcher ran, and the target did not answer."""


class FetchRefused(FetcherError):  # noqa: N818
    """The fetcher declined the request. ``kind`` is the wire format's."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """One request. ``form`` is sent urlencoded; ``body`` as given. Not both."""

    url: str
    method: Literal["GET", "POST", "HEAD"] = "GET"
    headers: Sequence[tuple[str, str]] = ()
    form: Sequence[tuple[str, str]] | None = None
    body: bytes | None = None
    follow_redirects: bool = True
    timeout: float = handler.DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.form is not None and self.body is not None:
            raise ValueError("a request carries a form or a body, not both")

    def to_event(self, cookies: Sequence[CookieRecord]) -> dict[str, Any]:
        """The version-1 request :mod:`~app.ingestion.remote.handler` reads."""
        headers = list(self.headers)
        body = self.body
        if self.form is not None:
            body = urlencode(list(self.form)).encode()
            if not any(name.lower() == "content-type" for name, _ in headers):
                headers.append(("Content-Type", "application/x-www-form-urlencoded"))
        return {
            "v": handler.WIRE_VERSION,
            "url": self.url,
            "method": self.method,
            "headers": [[name, value] for name, value in headers],
            "body_b64": None if body is None else base64.b64encode(body).decode(),
            "cookies": list(cookies),
            "follow_redirects": self.follow_redirects,
            "timeout": self.timeout,
        }


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """What the target answered. ``body`` is exactly the bytes it sent."""

    status: int
    #: After redirects, when they were followed.
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    #: The whole jar after this response: what was sent, plus what it set.
    cookies: tuple[CookieRecord, ...] = field(default=())

    def header(self, name: str) -> str | None:
        """The first header of that name, case-insensitively."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    @property
    def content_type(self) -> str:
        """The media type alone, lower-cased: ``text/html``, not its charset."""
        return (self.header("Content-Type") or "").split(";", 1)[0].strip().lower()


class Fetcher(Protocol):
    """Fetch a URL from a US address. Stateless: the cookie jar goes both ways."""

    @property
    def backend(self) -> str:
        """``lambda`` or ``local``, for logs and ``fetcher-check``."""
        ...

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        """Send ``request`` with ``cookies``; the response carries the new jar.

        :raises FetcherUnavailable: If the fetcher could not be used.
        :raises UpstreamUnreachable: If the target did not answer.
        :raises FetchRefused: If the fetcher declined the request.
        """
        ...


class CookieSession:
    """A :class:`Fetcher` with a cookie jar kept between requests.

    What "honour cookies" means from the caller's side: the jar each response
    returns is the one the next request sends. One per conversation with a
    site — one per eFD session — and not shared between concurrent tasks,
    whose responses would overwrite each other's jar.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._cookies: tuple[CookieRecord, ...] = ()

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        response = await self.fetcher.fetch(request, self._cookies)
        self._cookies = response.cookies
        return response

    def cookie(self, name: str) -> str | None:
        """The value of the cookie ``name``, from whichever domain set it."""
        return next((c["value"] for c in self._cookies if c["name"] == name), None)

    def clear(self) -> None:
        self._cookies = ()


# --- backends ----------------------------------------------------------------


class LocalFetcher:
    """The handler, run in this process, from this machine's address.

    The same code the Lambda runs, minus the spool: the body comes straight
    back. In a worker thread, because urllib blocks.
    """

    backend: Final = "local"

    def __init__(self, allowed_hosts: Sequence[str] = LOCAL_ALLOWED_HOSTS) -> None:
        self._allowed = tuple(allowed_hosts)

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        event = request.to_event(cookies)
        try:
            fetched = await asyncio.to_thread(handler.perform, event, self._allowed)
        except handler.FetchError as failure:
            raise _failure(failure.kind, str(failure)) from failure
        return FetchResponse(
            status=fetched["status"],
            url=fetched["url"],
            headers=tuple((name, value) for name, value in fetched["headers"]),
            body=fetched["body"],
            cookies=tuple(fetched["cookies"]),
        )


class LambdaFetcher:
    """The function in ``infra/us-fetcher/``, invoked synchronously.

    Takes open clients rather than building them, as
    :class:`~app.storage.s3.S3RawStore` does, so :func:`open_fetcher` owns the
    connection pools and a test can point them anywhere.
    """

    backend: Final = "lambda"

    def __init__(self, lambda_client: LambdaClient, s3_client: S3Client, function: str) -> None:
        self._lambda = lambda_client
        self._s3 = s3_client
        self._function = function

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        payload = json.dumps(request.to_event(cookies)).encode()
        with self._unavailable_on_aws_failure("invoking"):
            invoked = await self._lambda.invoke(
                FunctionName=self._function, InvocationType="RequestResponse", Payload=payload
            )
            raw = await invoked["Payload"].read()

        if "FunctionError" in invoked:
            # The function raised or timed out: Lambda's error payload, not ours.
            raise FetcherUnavailable(
                f"the US fetcher {self._function} failed ({invoked['FunctionError']}): "
                f"{_error_message(raw)}"
            )
        reply = _reply(raw, self._function)
        if not reply["ok"]:
            error = reply.get("error") or {}
            raise _failure(str(error.get("kind")), str(error.get("message")))

        body = await self._spooled(reply["body"])
        logger.debug("us_fetcher.fetched", status=reply["status"], bytes=len(body), url=request.url)
        return FetchResponse(
            status=int(reply["status"]),
            url=str(reply["url"]),
            headers=tuple((str(name), str(value)) for name, value in reply["headers"]),
            body=body,
            cookies=tuple(reply["cookies"]),
        )

    async def _spooled(self, where: Mapping[str, Any]) -> bytes:
        """The body from the spool, checked against the digest, then deleted.

        The delete is best-effort: the bucket's lifecycle rule expires what it
        misses, and a body already in hand is not worth failing over.
        """
        bucket, key = str(where["bucket"]), str(where["key"])
        with self._unavailable_on_aws_failure(f"reading s3://{bucket}/{key}"):
            response = await self._s3.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                body = await stream.read()
        if len(body) != where["size"] or hashlib.sha256(body).hexdigest() != where["sha256"]:
            raise FetcherUnavailable(
                f"s3://{bucket}/{key} does not match what the US fetcher spooled "
                f"({len(body)} bytes, expected {where['size']})"
            )
        try:
            await self._s3.delete_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError) as failure:
            logger.warning("us_fetcher.spool_not_deleted", key=key, error=str(failure))
        return body

    @contextmanager
    def _unavailable_on_aws_failure(self, doing: str) -> Iterator[None]:
        try:
            yield
        except ClientError as failure:
            code = failure.response.get("Error", {}).get("Code", "")
            raise FetcherUnavailable(
                f"the US fetcher is unreachable: {doing} {self._function} failed ({code}): "
                f"{failure}"
            ) from failure
        except BotoCoreError as failure:
            raise FetcherUnavailable(
                f"the US fetcher is unreachable: {doing} {self._function} failed: {failure}"
            ) from failure


def _reply(raw: bytes, function: str) -> dict[str, Any]:
    try:
        reply = json.loads(raw)
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or reply.get("v") != handler.WIRE_VERSION:
        raise FetcherUnavailable(
            f"the US fetcher {function} replied with something other than a version "
            f"{handler.WIRE_VERSION} reply — is the deployed code current? {raw[:200]!r}"
        )
    return reply


def _error_message(raw: bytes) -> str:
    try:
        error = json.loads(raw)
    except ValueError:
        return raw[:500].decode(errors="replace")
    if isinstance(error, dict):
        return str(error.get("errorMessage") or error)
    return str(error)


def _failure(kind: str, message: str) -> FetcherError:
    if kind == "upstream_unreachable":
        return UpstreamUnreachable(message)
    return FetchRefused(kind, message)


# --- selection ---------------------------------------------------------------


@asynccontextmanager
async def open_fetcher(settings: Settings) -> AsyncIterator[Fetcher]:
    """The fetcher ``US_FETCHER_BACKEND`` names, open for the block.

    :raises FetcherUnavailable: If none is configured — on entry, before any
        work is done, rather than at the first fetch of a run.
    """
    if settings.us_fetcher_backend == "none":
        raise FetcherUnavailable(
            "no US fetcher is configured: set US_FETCHER_BACKEND=lambda (and "
            "US_FETCHER_FUNCTION), or =local in development"
        )
    if settings.us_fetcher_backend == "local":
        yield LocalFetcher()
        return

    function = settings.us_fetcher_function
    if function is None:
        # Settings refuses this; restated for the type checker.
        raise FetcherUnavailable("US_FETCHER_BACKEND=lambda needs US_FETCHER_FUNCTION")
    region = settings.us_fetcher_region
    key_id = settings.us_fetcher_access_key_id
    secret = settings.us_fetcher_secret_access_key
    key = None if secret is None else secret.get_secret_value()
    session = get_session()
    async with (
        session.create_client(
            "lambda",
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=key,
            config=_CLIENT_CONFIG,
        ) as lambda_client,
        session.create_client(
            "s3",
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=key,
            config=_CLIENT_CONFIG,
        ) as s3_client,
    ):
        yield LambdaFetcher(lambda_client, s3_client, function)
