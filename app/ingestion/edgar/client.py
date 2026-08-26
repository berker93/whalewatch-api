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

Retrying, and not retrying
--------------------------
EDGAR fails in three ways that want three different answers, and the expensive
mistake is treating them as one. A dropped connection or a 503 is EDGAR having a
moment: the same request a second later usually works. A 404 is a document that
does not exist, and no number of attempts will conjure it — retrying one only
spends wall-clock time to arrive at the same answer, multiplied by every missing
document in a backfill. A 403 is worse than useless: the IP is already blocked,
and each extra request lands on a host that is counting them.

So :data:`_EDGAR_RETRY` sorts failures by what a retry would actually accomplish:

===================================  ===========================================
``httpx.TransportError`` (connect    Up to 5 attempts, exponential backoff with
errors, read timeouts, dropped       jitter from 1s, capped at 120s.
connections), 502/503/504
-----------------------------------  -------------------------------------------
403, 429 (:class:`EdgarRateLimited`) At most 2 retries, never sooner than 60s —
                                     and preferring SEC's own ``Retry-After``
                                     when it sent one.
-----------------------------------  -------------------------------------------
400, 404, every other 4xx            Not retried. Raised on the first attempt.
===================================  ===========================================

The jitter is not decoration. Twenty concurrent fetches that fail together would,
under a deterministic backoff, sleep for the same interval and wake in the same
millisecond — reconverging into exactly the synchronized burst that provoked the
failure. Spreading the wakeups across the interval is what stops a retry storm
from being indistinguishable from an attack.

Note that a rate-limit retry can sleep for the length of SEC's block (about ten
minutes) before its next attempt. That is deliberate — coming back sooner means
knocking on a door we know is shut — but it means a task hitting a 403 is parked,
not failed, and a caller that would rather abandon the unit of work should catch
:class:`EdgarRateLimited` and decide for itself rather than await it blindly.
"""

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Final, Self

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    stop_after_attempt,
    wait_exponential_jitter,
)

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

#: Status codes that mean EDGAR is briefly unwell rather than wrong about us: a
#: gateway that could not reach the origin, a load shedder, a timeout upstream.
#: Only these three, and not the whole 5xx range — a 500 is an unhandled error on
#: their side and a 501 is a request they will never serve, and neither improves
#: by being sent again.
_RETRYABLE_SERVER_STATUSES: Final = frozenset({502, 503, 504})

#: Attempt budgets. Five for the transient failures, which at the backoff below
#: spans roughly two minutes of trying before giving up. Three for a rate limit —
#: the point of retrying a 403 at all is to survive a block that ends while we
#: wait, and if two long waits did not outlast it, a third will not either.
_MAX_ATTEMPTS: Final = 5
_RATE_LIMIT_ATTEMPTS: Final = 3

#: Exponential backoff with jitter, in seconds: ~1, ~2, ~4, ~8 between attempts,
#: each spread by up to a second, and never longer than the cap. The cap is what
#: bounds a retry to something a job scheduler can reason about.
_BACKOFF_INITIAL_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 120.0


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


class EdgarServerError(Exception):
    """EDGAR returned a status that means "try again", not "you are wrong".

    Its own type rather than an ``httpx.HTTPStatusError`` carrying a 503, because
    the retry policy has to tell 503 from 404 and matching on exception type is
    the only way tenacity gets to make that distinction. A caller that reaches
    this has already had five attempts spent on its behalf.
    """

    def __init__(self, *, url: str, status_code: int) -> None:
        super().__init__(f"EDGAR returned {status_code} for {url}")
        self.url = url
        self.status_code = status_code


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


# --- retry policy -----------------------------------------------------------
# Three small functions rather than a stack of tenacity's combinators, because
# the decision genuinely depends on both the exception *and* the attempt number
# (a 503 gets five tries, a 403 gets three) and no combinator expresses that
# without being harder to read than the ``if`` that replaces it.


def _failure(state: RetryCallState) -> BaseException | None:
    """The exception the last attempt raised, or ``None`` if it succeeded."""
    outcome = state.outcome
    if outcome is None or not outcome.failed:
        return None
    return outcome.exception()


def _should_retry(state: RetryCallState) -> bool:
    """Decide whether the failure that just happened is worth repeating.

    Anything not named here — ``httpx.HTTPStatusError`` for a 404 or a 400, a
    ``json`` decode failure, a bug in our own code — falls through to ``False``
    and is raised to the caller on the first attempt. That default is the right
    way round: a new failure mode should surface immediately rather than be
    quietly attempted five times.
    """
    exc = _failure(state)
    if isinstance(exc, EdgarRateLimited):
        # Counted separately and stopped earlier than the transient failures.
        # ``attempt_number`` is the attempt that just failed, so this permits
        # _RATE_LIMIT_ATTEMPTS in total — two retries.
        return state.attempt_number < _RATE_LIMIT_ATTEMPTS
    return isinstance(exc, httpx.TransportError | EdgarServerError)


def _rate_limit_wait(retry_after: float) -> float:
    """How long to wait before re-approaching a host that just refused us.

    Floored at :data:`_THROTTLED_SECONDS` so that even a server suggesting "one
    second" is met with a minute — SEC's counter runs on a window longer than
    any single request, and obeying a short hint just spends an attempt. Ceilinged
    at :data:`_BLOCKED_SECONDS`, the length of the longest block SEC actually
    imposes, so a malformed or absurd ``Retry-After`` (an HTTP-date a week out)
    cannot park a worker indefinitely.
    """
    return min(max(_THROTTLED_SECONDS, retry_after), _BLOCKED_SECONDS)


def _retry_wait(state: RetryCallState) -> float:
    """Seconds to sleep before the next attempt.

    Rate limits get the flat, long wait above; everything retryable gets
    exponential backoff with jitter. Two curves and not one because they are
    answering different questions — "has EDGAR recovered yet?" wants to ask
    sooner and sooner-ish, "has our block expired yet?" has a known answer and
    asking early only extends it.
    """
    exc = _failure(state)
    if isinstance(exc, EdgarRateLimited):
        return _rate_limit_wait(exc.retry_after)
    return _BACKOFF(state)


def _log_retry(state: RetryCallState) -> None:
    """One line per retry, before the sleep that follows it.

    Emitted here rather than by tenacity's ``before_sleep_log`` so the attempt
    number, the wait and the reason arrive as fields — a backfill that finishes
    late is diagnosed by asking how many ``edgar.retry`` events it logged and
    against which URLs, and that is not a question you can ask of a sentence.
    """
    exc = _failure(state)
    logger.warning(
        "edgar.retry",
        url=_retry_url(state),
        attempt=state.attempt_number,
        max_attempts=_MAX_ATTEMPTS,
        sleep_seconds=round(state.upcoming_sleep, 2),
        error=type(exc).__name__,
        status=getattr(exc, "status_code", None),
    )


def _retry_url(state: RetryCallState) -> str | None:
    """Recover the URL from the wrapped ``_get(self, url)`` call's arguments."""
    url = state.kwargs.get("url", state.args[1] if len(state.args) > 1 else None)
    return url if isinstance(url, str) else None


#: Backoff for transient failures, built once: ``wait_exponential_jitter`` is
#: stateless and derives everything from the attempt number on the state passed
#: to it, so one instance serves every concurrent request.
_BACKOFF: Final = wait_exponential_jitter(
    initial=_BACKOFF_INITIAL_SECONDS, max=_BACKOFF_MAX_SECONDS
)

#: The policy itself, as an object rather than a ``@retry(...)`` decoration, for
#: two reasons: it can be documented and referenced by name, and a test can swap
#: the wait for a zero one without monkeypatching an attribute that tenacity
#: merely happens to hang off the wrapped function. ``wraps`` copies it per call,
#: so concurrent requests do not share iteration state.
_EDGAR_RETRY: Final = AsyncRetrying(
    retry=_should_retry,
    wait=_retry_wait,
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    before_sleep=_log_retry,
    # So callers see httpx.ConnectTimeout or EdgarRateLimited — the exceptions
    # the rest of the ingestion code is written to catch — rather than a
    # tenacity.RetryError wrapping the one that matters.
    reraise=True,
)


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

    @_EDGAR_RETRY.wraps
    async def _get(self, url: str) -> httpx.Response:
        """The single path every EDGAR request takes.

        Retried here rather than around :meth:`get_bytes`, which is the same
        thing for a byte fetch and additionally covers :meth:`get_json` — the
        transient failures the policy exists for are properties of the request,
        not of what the caller intends to do with the body. Retrying inside the
        limiter's scope also means each attempt draws its own token, so a burst
        of retries stays inside SEC's rate ceiling instead of bypassing it.
        """
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
        if response.status_code in _RETRYABLE_SERVER_STATUSES:
            raise EdgarServerError(url=url, status_code=response.status_code)
        # Everything else 4xx/5xx: a 404 for a document that is not there, a 400
        # for a URL we built wrong. Both are ours to fix, neither improves with
        # another attempt, so they leave here as an ordinary HTTPStatusError that
        # the retry policy declines to match.
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
