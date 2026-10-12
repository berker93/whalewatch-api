"""CUSIP to ticker resolution via OpenFIGI.

There is no free, official CUSIP-to-ticker mapping. OpenFIGI's mapping API is
the best available, and it is quota-limited: per minute, and per request. So
batching is not an optimisation here, it is the difference between a backfill
that finishes and one that does not, and both limits live on the client the way
EDGAR's live on :class:`~app.ingestion.edgar.client.EdgarClient`::

    async with OpenFIGIClient(settings, DatabaseResponseLog(sessions)) as figi:
        results = await figi.map_cusips(["037833100", "084670702"])

The quotas
----------
As published at https://www.openfigi.com/api/documentation, checked 2026-10-12:

===========  ======================  =================
             Requests                Jobs per request
===========  ======================  =================
No API key   25 per minute           10
API key      25 per 6 seconds        100
===========  ======================  =================

A job is one CUSIP. With a key, then, a 3,000-CUSIP backfill is 30 requests and
about eight seconds; without one it is 300 requests and the better part of
fifteen minutes. ``OPENFIGI_API_KEY`` is optional but worth the form.

These numbers have changed before. If OpenFIGI starts answering 429 at a pace
this module believes is under quota, or 413 to a batch it believes is in size,
:data:`UNKEYED_QUOTA` and :data:`KEYED_QUOTA` are what to re-check. Requests are
paced at :data:`_QUOTA_HEADROOM` of the published rate, so a window counted
slightly differently on their side than on ours does not cost a 429.

What comes back
---------------
:meth:`OpenFIGIClient.map_cusips` returns one :class:`FIGIResult` per distinct
CUSIP asked about, whatever OpenFIGI said, and raises only when a request as a
whole failed. Per CUSIP:

* **Found** — every instrument OpenFIGI returned, in its order. Commonly more
  than one: an unfiltered CUSIP lookup answers with a listing per exchange (and
  sometimes per share class), so ambiguity is the usual case, not the edge case.
  Nothing here picks one. Which listing a ``security`` row should take is a
  resolution policy, and a policy belongs where it can be read and changed, not
  buried in the transport.
* **Not found** — an explicit result, not an exception and not a missing key.
  Unresolved is a normal state for a delisted name or an obscure convert.
* **Error** — OpenFIGI refused that one job (a malformed identifier, say) while
  answering the rest of the request.

Re-running costs nothing
------------------------
Every job's response is written, verbatim, to ``figi_response_log`` the moment
it arrives — before it is parsed, and per request, so a run that dies at batch
twenty keeps the nineteen it paid for. The next call reads the latest logged
answer for each CUSIP and asks OpenFIGI only about the rest. Errors are logged
but never reused: they say nothing about the CUSIP. ``refresh=True`` asks again
regardless, which is how a miss gets retried once a new listing might have
appeared.

Retrying, and not retrying
--------------------------
The same reasoning as EDGAR's client, for a smaller set of failures:

==================================  ===========================================
``httpx.TransportError``, 502,      Up to 5 attempts, exponential backoff with
503, 504                            jitter from 1s, capped at 60s.
----------------------------------  -------------------------------------------
429 (:class:`OpenFIGIRateLimited`)  Up to 5 attempts, waiting what OpenFIGI's
                                    ``ratelimit-reset`` header says (at least
                                    1s, at most 60s).
----------------------------------  -------------------------------------------
Every other 4xx/5xx                 Not retried. A 401 is a bad key, a 400 or
                                    413 a request we built wrong.
==================================  ===========================================
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Final, Protocol, Self

import httpx
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt, wait_exponential_jitter

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.rate_limit import AsyncTokenBucket, get_openfigi_limiter
from app.db.models.figi_response_log import FIGIOutcome, FigiResponseLog

logger = get_logger(__name__)

OPENFIGI_MAPPING_URL: Final = "https://api.openfigi.com/v3/mapping"

#: The header OpenFIGI reads a key from. Sent only when there is a key: an empty
#: one is a 401, not an unkeyed request.
API_KEY_HEADER: Final = "X-OPENFIGI-APIKEY"


@dataclass(frozen=True, slots=True)
class Quota:
    """One tier of OpenFIGI's mapping quota."""

    requests: int
    window_seconds: float
    jobs_per_request: int

    @property
    def requests_per_second(self) -> float:
        return self.requests / self.window_seconds


#: See the module docstring for where these come from, and when.
UNKEYED_QUOTA: Final = Quota(requests=25, window_seconds=60.0, jobs_per_request=10)
KEYED_QUOTA: Final = Quota(requests=25, window_seconds=6.0, jobs_per_request=100)

#: The fraction of the published rate actually used. The bucket spaces requests
#: evenly, so at 100% the 26th request of a run lands exactly one window after
#: the first — legal or not depending on whether their window's edges are open
#: or closed. Ten percent off costs ninety seconds on a keyless 3,000-CUSIP run
#: and removes the question.
_QUOTA_HEADROOM: Final = 0.9

#: A 429's wait when OpenFIGI sends no usable ``ratelimit-reset``, and the
#: bounds on one that it does: never sooner than a second, which would just
#: spend an attempt, and never longer than the unkeyed window, which is the
#: longest a quota can be exhausted for.
_MIN_RATE_LIMIT_WAIT_SECONDS: Final = 1.0
_MAX_RATE_LIMIT_WAIT_SECONDS: Final = 60.0

_RETRYABLE_SERVER_STATUSES: Final = frozenset({502, 503, 504})
_MAX_ATTEMPTS: Final = 5
_BACKOFF_INITIAL_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 60.0

#: A CUSIP is nine characters, and ``figi_response_log.cusip`` is ``CHAR(9)``:
#: anything shorter would be stored space-padded and come back as a different
#: key from the one asked about.
_CUSIP_LENGTH: Final = 9


# --- results ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FIGIMatch:
    """One instrument OpenFIGI mapped a CUSIP to.

    Every field but :attr:`figi` is optional because OpenFIGI omits or nulls
    them freely — a ticker is often missing for a fund or a warrant.
    """

    figi: str
    ticker: str | None
    name: str | None
    exchange_code: str | None
    security_type: str | None
    security_type2: str | None
    market_sector: str | None
    composite_figi: str | None
    share_class_figi: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> FIGIMatch:
        return cls(
            figi=raw["figi"],
            ticker=raw.get("ticker"),
            name=raw.get("name"),
            exchange_code=raw.get("exchCode"),
            security_type=raw.get("securityType"),
            security_type2=raw.get("securityType2"),
            market_sector=raw.get("marketSector"),
            composite_figi=raw.get("compositeFIGI"),
            share_class_figi=raw.get("shareClassFIGI"),
        )


@dataclass(frozen=True, slots=True)
class FIGIResult:
    """What OpenFIGI said about one CUSIP, and when.

    :attr:`matches` is empty unless :attr:`outcome` is ``found``, and then holds
    every match, unnarrowed. :attr:`message` is OpenFIGI's ``warning`` or
    ``error`` text, for the outcomes that have one.
    """

    cusip: str
    outcome: FIGIOutcome
    matches: tuple[FIGIMatch, ...]
    message: str | None
    requested_at: datetime
    """When OpenFIGI gave this answer: this call, or an earlier one whose answer
    came from the log. Older than the call means no quota was spent on it."""

    @property
    def found(self) -> bool:
        return self.outcome is FIGIOutcome.FOUND

    @property
    def is_ambiguous(self) -> bool:
        """More than one instrument came back. Usually one listing per exchange;
        see the module docstring for why nothing here chooses between them."""
        return len(self.matches) > 1

    @classmethod
    def from_response(
        cls, cusip: str, response: dict[str, Any], requested_at: datetime
    ) -> FIGIResult:
        """Parse one job's element of a mapping response."""
        outcome = classify(response)
        matches: tuple[FIGIMatch, ...] = ()
        message: str | None = None
        if outcome is FIGIOutcome.FOUND:
            matches = tuple(FIGIMatch.from_api(raw) for raw in response["data"])
        elif "warning" in response or "error" in response:
            message = str(response.get("warning", response.get("error")))
        elif outcome is FIGIOutcome.ERROR:
            message = f"unrecognised response: {response!r}"
        return cls(cusip, outcome, matches, message, requested_at)


def classify(response: dict[str, Any]) -> FIGIOutcome:
    """The outcome of one job, from the shape of its response.

    An empty ``data`` array is a miss, not a match with nothing in it. Anything
    that is none of the three documented shapes is an error, so a format change
    on OpenFIGI's side is retried next run rather than reused as an answer.
    """
    if response.get("data"):
        return FIGIOutcome.FOUND
    if "warning" in response or response.get("data") == []:
        return FIGIOutcome.NOT_FOUND
    return FIGIOutcome.ERROR


# --- the response log ---------------------------------------------------------


class ResponseLog(Protocol):
    """Where :class:`OpenFIGIClient` keeps what it has paid for.

    A protocol so the client's tests can hold the log in memory, and so the
    client does not care how a session is come by. :class:`DatabaseResponseLog`
    is the only implementation that ships.
    """

    async def latest(self, cusips: Sequence[str]) -> dict[str, FIGIResult]:
        """The most recent logged answer for each of ``cusips`` that has one."""
        ...

    async def record(self, responses: Sequence[tuple[str, dict[str, Any]]]) -> datetime:
        """Durably log ``(cusip, response)`` pairs from one request; return the
        time they are recorded as received."""
        ...


class DatabaseResponseLog:
    """:class:`ResponseLog` on ``figi_response_log``.

    Takes a session factory rather than a session and commits each
    :meth:`record` in its own transaction, so a response is durable before the
    client moves on to the next request — the point of logging it at all — and
    a caller's transaction rolling back cannot take it with it.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def latest(self, cusips: Sequence[str]) -> dict[str, FIGIResult]:
        if not cusips:
            return {}
        log = FigiResponseLog
        statement = (
            select(log.cusip, log.response, log.requested_at)
            .where(log.cusip.in_(cusips))
            .distinct(log.cusip)
            .order_by(log.cusip, log.requested_at.desc(), log.id.desc())
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).all()
        return {
            row.cusip: FIGIResult.from_response(row.cusip, row.response, row.requested_at)
            for row in rows
        }

    async def record(self, responses: Sequence[tuple[str, dict[str, Any]]]) -> datetime:
        statement = (
            insert(FigiResponseLog)
            .values(
                [
                    {"cusip": cusip, "outcome": classify(response).value, "response": response}
                    for cusip, response in responses
                ]
            )
            .returning(FigiResponseLog.requested_at)
        )
        async with self._sessions.begin() as session:
            # One statement, one transaction, one now(): every row shares it.
            return (await session.execute(statement)).scalars().all()[0]


# --- errors and the retry policy ----------------------------------------------


# N818 suppressed for the reason given on EdgarRateLimited: it reads at the
# call site as the condition it is.
class OpenFIGIRateLimited(Exception):  # noqa: N818
    """OpenFIGI answered 429: over quota for the current window."""

    def __init__(self, *, retry_after: float) -> None:
        super().__init__(f"OpenFIGI returned 429; quota resets in {retry_after:.0f}s")
        self.retry_after = retry_after


class OpenFIGIServerError(Exception):
    """A status that means "try again" rather than "you are wrong"; its own type
    so the retry policy can tell it from a 400."""

    def __init__(self, *, status_code: int) -> None:
        super().__init__(f"OpenFIGI returned {status_code}")
        self.status_code = status_code


class OpenFIGIResponseError(Exception):
    """A 200 whose body is not one response per job, in order — which would
    make pairing answers with CUSIPs a guess. Not retried, and nothing logged."""


def _reset_seconds(response: httpx.Response) -> float:
    """Seconds until the quota resets, from ``ratelimit-reset``, bounded."""
    try:
        reset = float(response.headers.get("ratelimit-reset", ""))
    except ValueError:
        reset = _MAX_RATE_LIMIT_WAIT_SECONDS
    return min(max(reset, _MIN_RATE_LIMIT_WAIT_SECONDS), _MAX_RATE_LIMIT_WAIT_SECONDS)


def _failure(state: RetryCallState) -> BaseException | None:
    outcome = state.outcome
    if outcome is None or not outcome.failed:
        return None
    return outcome.exception()


def _should_retry(state: RetryCallState) -> bool:
    return isinstance(
        _failure(state), httpx.TransportError | OpenFIGIServerError | OpenFIGIRateLimited
    )


_BACKOFF: Final = wait_exponential_jitter(
    initial=_BACKOFF_INITIAL_SECONDS, max=_BACKOFF_MAX_SECONDS
)


def _retry_wait(state: RetryCallState) -> float:
    exc = _failure(state)
    if isinstance(exc, OpenFIGIRateLimited):
        return exc.retry_after
    return _BACKOFF(state)


def _log_retry(state: RetryCallState) -> None:
    exc = _failure(state)
    logger.warning(
        "openfigi.retry",
        attempt=state.attempt_number,
        max_attempts=_MAX_ATTEMPTS,
        sleep_seconds=round(state.upcoming_sleep, 2),
        error=type(exc).__name__,
        status=getattr(exc, "status_code", 429 if isinstance(exc, OpenFIGIRateLimited) else None),
    )


#: An object, not a decoration, for the reason given on EDGAR's: a test can swap
#: the wait for a zero one and keep every decision.
_OPENFIGI_RETRY: Final = AsyncRetrying(
    retry=_should_retry,
    wait=_retry_wait,
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    before_sleep=_log_retry,
    reraise=True,
)


# --- the client -----------------------------------------------------------------


class OpenFIGIClient:
    """A batched, rate-limited, logged client for OpenFIGI's mapping API."""

    def __init__(
        self,
        settings: Settings,
        log: ResponseLog,
        *,
        limiter: AsyncTokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client. ``limiter`` and ``transport`` are test seams, as on
        :class:`~app.ingestion.edgar.client.EdgarClient`."""
        key = settings.openfigi_api_key
        self.quota = KEYED_QUOTA if key is not None else UNKEYED_QUOTA
        self._log = log
        self._limiter = (
            limiter
            if limiter is not None
            else get_openfigi_limiter(self.quota.requests_per_second * _QUOTA_HEADROOM)
        )
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers[API_KEY_HEADER] = key.get_secret_value()
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
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
        await self._client.aclose()

    async def map_cusips(
        self, cusips: Iterable[str], *, refresh: bool = False
    ) -> dict[str, FIGIResult]:
        """One :class:`FIGIResult` for every distinct CUSIP in ``cusips``.

        Answers already in the log are returned from it unless ``refresh``;
        the rest are asked for in batches of the quota's job cap, one request
        at a time, each logged before the next is sent. Raises
        :class:`ValueError` up front for anything that is not nine characters,
        before any quota is spent.
        """
        wanted = list(dict.fromkeys(cusips))
        malformed = [cusip for cusip in wanted if len(cusip) != _CUSIP_LENGTH]
        if malformed:
            raise ValueError(f"not nine-character CUSIPs: {malformed[:10]!r}")

        results: dict[str, FIGIResult] = {}
        if not refresh:
            logged = await self._log.latest(wanted)
            results = {
                cusip: result
                for cusip, result in logged.items()
                if result.outcome is not FIGIOutcome.ERROR
            }
        reused = len(results)

        to_fetch = [cusip for cusip in wanted if cusip not in results]
        for batch in _batches(to_fetch, self.quota.jobs_per_request):
            results.update(await self._map_batch(batch))

        logger.info(
            "openfigi.mapped",
            cusips=len(wanted),
            from_log=reused,
            requested=len(to_fetch),
            requests=-(-len(to_fetch) // self.quota.jobs_per_request),
            found=sum(r.outcome is FIGIOutcome.FOUND for r in results.values()),
            not_found=sum(r.outcome is FIGIOutcome.NOT_FOUND for r in results.values()),
            errors=sum(r.outcome is FIGIOutcome.ERROR for r in results.values()),
            ambiguous=sum(r.is_ambiguous for r in results.values()),
        )
        # In the order asked, whichever source each answer came from.
        return {cusip: results[cusip] for cusip in wanted}

    async def _map_batch(self, batch: Sequence[str]) -> dict[str, FIGIResult]:
        jobs = await self._post([{"idType": "ID_CUSIP", "idValue": cusip} for cusip in batch])
        if not isinstance(jobs, list) or len(jobs) != len(batch):
            raise OpenFIGIResponseError(
                f"expected a list of {len(batch)} job responses, got "
                f"{len(jobs) if isinstance(jobs, list) else type(jobs).__name__}"
            )
        if not all(isinstance(job, dict) for job in jobs):
            raise OpenFIGIResponseError("a job response is not a JSON object")

        pairs = list(zip(batch, jobs, strict=True))
        requested_at = await self._log.record(pairs)
        return {cusip: FIGIResult.from_response(cusip, job, requested_at) for cusip, job in pairs}

    @_OPENFIGI_RETRY.wraps
    async def _post(self, jobs: list[dict[str, str]]) -> Any:
        """The single path every OpenFIGI request takes. Each attempt draws its
        own token, so retries stay inside the quota rather than bypassing it."""
        async with self._limiter:
            started = time.perf_counter()
            try:
                response = await self._client.post(OPENFIGI_MAPPING_URL, json=jobs)
            except httpx.HTTPError as exc:
                logger.warning(
                    "openfigi.request_failed",
                    jobs=len(jobs),
                    duration_ms=_elapsed_ms(started),
                    error=type(exc).__name__,
                )
                raise

        logger.info(
            "openfigi.request",
            jobs=len(jobs),
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
            remaining=response.headers.get("ratelimit-remaining"),
        )
        if response.status_code == 429:
            raise OpenFIGIRateLimited(retry_after=_reset_seconds(response))
        if response.status_code in _RETRYABLE_SERVER_STATUSES:
            raise OpenFIGIServerError(status_code=response.status_code)
        response.raise_for_status()
        return response.json()


def _batches(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
