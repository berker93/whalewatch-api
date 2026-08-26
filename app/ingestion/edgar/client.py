"""EDGAR HTTP client: fetch + archive raw documents.

SEC enforces two rules on EDGAR and both are hard failures rather than
degradations: a request without a descriptive ``User-Agent`` gets a 403, and an
IP over roughly 10 requests/second is blocked for about ten minutes. Neither is
survivable halfway through a backfill, and neither can be left to call sites —
"remember to pass the header" and "remember to sleep" are the two instructions
that hold right up until the one loop that forgets.

So both live on the client. The header is set on the underlying
``httpx.AsyncClient``, which means it goes out on every request this object
makes and there is no code path that can omit it. The pacing is a
process-global token bucket (:mod:`app.core.rate_limit`), shared by every
instance, because the budget belongs to our IP address rather than to any one
object.

One client, held open
---------------------
``httpx.AsyncClient`` is a connection pool. Constructing one per request throws
away the pooled connection and its TLS handshake — measurably slower over a few
thousand fetches, and it defeats keep-alive against a host that rewards it. So
the client is long-lived and scoped by an ``async with``::

    async with EdgarClient(settings) as edgar:
        raw = await edgar.get_json(EdgarClient.submissions_url("0000320193"))
"""

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Final, Self

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.rate_limit import AsyncTokenBucket, get_edgar_limiter

logger = get_logger(__name__)

#: Company metadata and the submissions history live on the data subdomain,
#: which wants a zero-padded CIK. Raw filing documents live under Archives,
#: which wants an unpadded one. That inconsistency is EDGAR's, and the reason
#: every URL in this codebase is built by one of the functions below.
SEC_DATA_BASE: Final = "https://data.sec.gov"
SEC_ARCHIVES_BASE: Final = "https://www.sec.gov/Archives/edgar"

#: What to suggest waiting when SEC does not say. A 403 from a request that
#: carried a valid User-Agent means the IP is blocked, and that block runs about
#: ten minutes; a 429 is ordinary throttling and clears far sooner.
_BLOCKED_SECONDS: Final = 600.0
_THROTTLED_SECONDS: Final = 60.0

#: Status codes that mean "you are being limited", as opposed to "that document
#: does not exist". Kept apart from the 4xx family on purpose: a 404 should fail
#: one filing, one of these should stop the whole run.
_LIMITED_STATUSES: Final = frozenset({403, 429})


# The suppression below is for pep8-naming's ``Error`` suffix rule. This reads
# at the call site as the condition it is — ``except EdgarRateLimited`` — and it
# is the name the rest of the ingestion code is written against.
class EdgarRateLimited(Exception):  # noqa: N818
    """EDGAR refused the request under its fair-access rules.

    Distinct from ``httpx.HTTPStatusError`` because the correct response is
    different in kind: not "retry this URL" but "stop making EDGAR requests for
    :attr:`retry_after` seconds". Carrying the suggested wait on the exception
    means the caller deciding that does not have to re-derive it from a status
    code it can no longer see.
    """

    def __init__(self, *, url: str, status_code: int, retry_after: float) -> None:
        super().__init__(f"EDGAR returned {status_code} for {url}; back off for {retry_after:.0f}s")
        self.url = url
        self.status_code = status_code
        self.retry_after = retry_after


def _suggested_wait(response: httpx.Response) -> float:
    """How long to stay off EDGAR, preferring what the server actually said.

    ``Retry-After`` is defined as either a number of seconds or an HTTP-date,
    and servers use both. Parsing it beats our own guess whenever it is there;
    a malformed value falls back to the guess rather than raising, because
    failing to parse a hint is not a reason to lose the 429 itself.
    """
    raw = response.headers.get("retry-after", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            deadline = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            pass
        else:
            # An HTTP-date without a zone is UTC by RFC 9110.
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    return _BLOCKED_SECONDS if response.status_code == 403 else _THROTTLED_SECONDS


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class EdgarClient:
    """A rate-limited, correctly-identified HTTP client for EDGAR.

    Every request goes through :meth:`_get`, so the limiter, the access log and
    the 403/429 mapping apply to all of them by construction rather than by
    convention.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        limiter: AsyncTokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client. ``limiter`` and ``transport`` are test seams.

        Both default to the real thing: the process-global bucket, and httpx's
        own network transport. A test passes its own bucket to assert on pacing
        without waiting on the shared one, or a ``MockTransport`` to assert on
        what we send without touching sec.gov.
        """
        self._limiter = (
            limiter
            if limiter is not None
            else get_edgar_limiter(settings.sec_rate_limit_per_second)
        )
        self._client = httpx.AsyncClient(
            headers={
                # Built by Settings so the contact address has one home, and set
                # here rather than per-call so no request can go out without it.
                "User-Agent": settings.sec_user_agent,
                # EDGAR's JSON is large and compresses well; submissions files
                # for a big filer run to megabytes uncompressed.
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            # EDGAR redirects between www.sec.gov and its CDN hostnames.
            follow_redirects=True,
            # Below the 10/s ceiling on purpose. The limiter is what enforces
            # the rate; this just stops a runaway caller from opening fifty
            # sockets to a host that would rather we did not.
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool. Idempotent, as httpx's own close is."""
        await self._client.aclose()

    # --- requests ------------------------------------------------------------

    async def get_bytes(self, url: str) -> bytes:
        """Fetch ``url`` and return its body verbatim.

        Bytes and not text: filing documents are archived exactly as EDGAR
        served them, and decoding on the way in would mean the copy we keep is
        not the copy we were given.
        """
        response = await self._get(url)
        return response.content

    async def get_json(self, url: str) -> Any:
        """Fetch ``url`` and parse it as JSON.

        Untyped by design — the shape of a submissions or index document is
        EDGAR's business, and validating it belongs to the parser that knows
        which document this is, not to the transport.
        """
        response = await self._get(url)
        return response.json()

    async def _get(self, url: str) -> httpx.Response:
        """The single path every EDGAR request takes."""
        async with self._limiter:
            started = time.perf_counter()
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                # Logged here because a timeout or a DNS failure produces no
                # response and would otherwise leave no trace of the attempt at
                # all — the one case where the access line below never runs.
                logger.warning(
                    "edgar.request_failed",
                    url=url,
                    duration_ms=_elapsed_ms(started),
                    error=type(exc).__name__,
                )
                raise

        logger.info(
            "edgar.request",
            url=url,
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
            bytes=len(response.content),
        )

        if response.status_code in _LIMITED_STATUSES:
            raise EdgarRateLimited(
                url=url,
                status_code=response.status_code,
                retry_after=_suggested_wait(response),
            )
        response.raise_for_status()
        return response

    # --- URL construction ----------------------------------------------------
    # Static, so a caller assembling a work queue can build URLs without holding
    # a client open, and so they can be tested without one.

    @staticmethod
    def submissions_url(cik: int | str) -> str:
        """Every filing a company has made, newest first.

        The path wants the CIK zero-padded to ten digits; ``CIK320193.json`` is
        a 404.
        """
        return f"{SEC_DATA_BASE}/submissions/CIK{_padded_cik(cik)}.json"

    @staticmethod
    def filing_index_url(cik: int | str, accession_no: str) -> str:
        """The document manifest for one filing.

        Mirror image of :meth:`submissions_url`: here the CIK is *unpadded* and
        the accession number is stripped of its dashes, while the dashed form is
        what appears everywhere else including our own logs and database.
        """
        return (
            f"{SEC_ARCHIVES_BASE}/data/{_unpadded_cik(cik)}/"
            f"{accession_no.replace('-', '')}/index.json"
        )


def _padded_cik(cik: int | str) -> str:
    """``320193`` or ``"0000320193"`` -> ``"0000320193"``."""
    return f"{int(cik):010d}"


def _unpadded_cik(cik: int | str) -> int:
    """``"0000320193"`` or ``320193`` -> ``320193``."""
    return int(cik)
