"""Working through every 13F we know of, several at a time, in a way that survives dying.

A full backfill is a couple of thousand filings at a shared eight requests a
second, which is the better part of half an hour of network time, and it will
fail partway through at least once. So the property this module is built around
is that **resuming costs nothing**: not a procedure, not a flag, not a note of
where the last run stopped. Running the same command again is the recovery.

The working set comes from the database
---------------------------------------
:func:`plan_backfill` reads it from ``pending_filing`` — what ``discover-filings``
queued — and from ``filing``, never from EDGAR. A filing that is loaded
(:data:`~app.db.models.filing.LOADED_STATUSES`) is skipped there, in the plan,
before any worker starts. A run that died on filing 1,347 is resumed by running
it again, and the 1,346 before it cost one query between them.

Each filing in the plan has one of three fates:

- **skipped** — loaded, and not forced.
- **reprocessed** — loaded, and forced: its archived documents are parsed again
  with the ``filed_at`` its row already holds. No EDGAR request, which is what
  turns a parser fix from a recrawl into a reparse.
- **ingested** — not loaded: fetched, archived, parsed and loaded, in that
  order, exactly as ``ingest-filing`` does it.

One filing's failure is one filing's
------------------------------------
:func:`run_backfill` gives each filing to ``process`` under a semaphore, and
whatever that raises is caught, logged with its traceback, recorded on the queue
row, and reported — and the run carries on. The one exception is
:class:`~app.ingestion.edgar.client.EdgarRateLimited`: SEC blocks by IP, so
every filing after it would fail the same way, and it stops the run instead.

Stopping
--------
Through one :class:`asyncio.Event`, set by the first Ctrl-C
(:func:`stop_on_interrupt`) or by a rate-limit block. It is checked before a
filing starts and never during one, so what is in flight finishes and what has
not started is left for the next run. Nothing would be lost by stopping midway
— the load is one transaction and each archived document is written whole —
but a clean stop is one whose summary adds up.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final, Self

import structlog
from sqlalchemy import Date, Row, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.filer import Filer, FilerCik
from app.db.models.filing import LOADED_STATUSES, Filing
from app.db.models.pending_filing import PendingFiling, PendingStatus
from app.ingestion.discovery import record_failure, tracked_filer_ids
from app.ingestion.edgar.client import EdgarRateLimited

logger = get_logger(__name__)

#: Where EDGAR's ``filingDate`` is reckoned, so that a loaded filing with no
#: queue row is dated the way a queued one is when ``--since`` compares them.
_EDGAR_TIMEZONE: Final = "America/New_York"


# --- the plan ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedFiling:
    """The ``filing`` row of a loaded filing: everything a reprocess needs.

    ``filed_at`` is the reason a reprocess can skip EDGAR at all. It comes from
    the submissions index, not from either document, and it decides the units
    of every value in the filing — so a reparse either has it from here or has
    to go back to the network for it.
    """

    filed_at: datetime
    raw_key: str | None
    source_url: str | None


@dataclass(frozen=True, slots=True)
class PlannedFiling:
    """One filing in a run, and what the database knew about it beforehand."""

    accession_no: str

    cik: str
    """The CIK whose EDGAR archive holds it: the queue row's, when there is one.

    Preferred over ``filing.cik``, which the loader takes from the cover page and
    which, on a co-filed report, names a different filer from the one whose
    directory the documents are in.
    """

    slug: str | None
    """The filer's slug, or ``None`` for a CIK no tracked filer claims."""

    filing_date: date
    """EDGAR's ``filingDate``, for ordering and ``--since``."""

    period: date | None
    """The period it reports, as far as the database knows before parsing."""

    queued: bool
    """Whether it has a ``pending_filing`` row to record the attempt on."""

    loaded: LoadedFiling | None
    """Its row, when it is loaded. In a plan's :attr:`~BackfillPlan.work` this
    means it is there to be reprocessed from the raw store."""

    @property
    def label(self) -> str:
        """How a person reading the progress output knows which filer it is."""
        return self.slug or f"CIK {self.cik}"


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """What a run is going to do, decided before it does any of it."""

    work: tuple[PlannedFiling, ...]
    """Filings to ingest or reprocess, oldest first, ``--limit`` applied."""

    skipped: tuple[PlannedFiling, ...]
    """Loaded, and left alone because the run is not forced."""

    held_back: int
    """Filings that needed work but fell outside ``--limit``."""

    @property
    def to_reprocess(self) -> int:
        return sum(1 for filing in self.work if filing.loaded is not None)

    @property
    def to_ingest(self) -> int:
        return len(self.work) - self.to_reprocess


async def plan_backfill(
    session: AsyncSession,
    *,
    slug: str | None,
    since: date | None,
    force: bool,
    limit: int | None,
) -> BackfillPlan:
    """Every 13F queued or loaded, for ``slug`` and ``since``, sorted into work and skips.

    Two sources, because each has filings the other lacks. The queue has what
    discovery found and nobody has loaded, which is the backfill proper. The
    ``filing`` table has what is loaded, including filings loaded by hand
    before discovery ever queued them — the ones ``--force`` has to reach.

    ``--filer`` and ``--since`` are applied after the two are merged rather
    than in SQL, so that a filing in both is judged once, on the queue's
    account of it: the two can disagree about its filer while a CIK is being
    moved, and about its date by the hours between acceptance and midnight.

    Also marks ``done`` any queue row whose filing is loaded, as
    ``ingest-filing`` does when it skips one, so the queue does not go on
    listing as outstanding a filing this run counts as finished. Does not
    commit.

    :raises UnknownFilerError: ``slug`` names no filer.
    """
    if slug is not None:
        await tracked_filer_ids(session, slug=slug)

    queue = {
        row.accession_no: row
        for row in await session.execute(
            select(
                PendingFiling.accession_no,
                PendingFiling.cik,
                PendingFiling.filing_date,
                PendingFiling.report_date,
                PendingFiling.status,
                Filer.slug,
            )
            .outerjoin(FilerCik, FilerCik.cik == PendingFiling.cik)
            .outerjoin(Filer, Filer.id == FilerCik.filer_id)
        )
    }
    filed = {
        row.accession_no: row
        for row in await session.execute(
            select(
                Filing.accession_no,
                Filing.cik,
                Filing.period_of_report,
                Filing.filed_at,
                cast(func.timezone(_EDGAR_TIMEZONE, Filing.filed_at), Date).label("filed_on"),
                Filing.parse_status,
                Filing.raw_key,
                Filing.source_url,
                Filer.slug,
            )
            .outerjoin(Filer, Filer.id == Filing.filer_id)
            # The table is shared with Form 4s, which this pipeline does not parse.
            .where(Filing.form_type.startswith("13F"))
        )
    }

    def loaded(row: Row[Any] | None) -> LoadedFiling | None:
        if row is None or row.parse_status not in LOADED_STATUSES:
            return None
        return LoadedFiling(filed_at=row.filed_at, raw_key=row.raw_key, source_url=row.source_url)

    candidates: list[PlannedFiling] = []
    for accession_no, queued in queue.items():
        row = filed.get(accession_no)
        candidates.append(
            PlannedFiling(
                accession_no=accession_no,
                cik=queued.cik,
                slug=queued.slug,
                filing_date=queued.filing_date,
                period=queued.report_date if row is None else row.period_of_report,
                queued=True,
                loaded=loaded(row),
            )
        )
    for accession_no, row in filed.items():
        if accession_no not in queue:
            candidates.append(
                PlannedFiling(
                    accession_no=accession_no,
                    cik=row.cik,
                    slug=row.slug,
                    filing_date=row.filed_on,
                    period=row.period_of_report,
                    queued=False,
                    loaded=loaded(row),
                )
            )

    planned = sorted(
        (
            filing
            for filing in candidates
            if (slug is None or filing.slug == slug)
            and (since is None or filing.filing_date >= since)
        ),
        key=lambda filing: (filing.filing_date, filing.accession_no),
    )

    stale = [
        filing.accession_no
        for filing in planned
        if filing.loaded is not None
        and filing.queued
        and queue[filing.accession_no].status != PendingStatus.DONE
    ]
    if stale:
        await session.execute(
            update(PendingFiling)
            .where(PendingFiling.accession_no.in_(stale))
            .values(status=PendingStatus.DONE.value)
        )

    work = [filing for filing in planned if force or filing.loaded is None]
    return BackfillPlan(
        work=tuple(work if limit is None else work[:limit]),
        skipped=tuple(filing for filing in planned if not force and filing.loaded is not None),
        held_back=0 if limit is None else max(0, len(work) - limit),
    )


# --- one run -----------------------------------------------------------------


class Outcome(StrEnum):
    """What became of one filing a run meant to work on."""

    OK = "ok"
    SUSPECT = "suspect"
    """Loaded, with guard findings on ``filing.parse_notes``. A success: the
    filing is in the database, and the flag is there to be read."""
    FAILED = "failed"
    NOT_STARTED = "not started"
    """The run stopped before reaching it. The next run will."""


@dataclass(frozen=True, slots=True)
class FilingResult:
    """One filing's outcome, with what the progress line says about it."""

    filing: PlannedFiling
    outcome: Outcome

    period: date | None
    """The cover page's period when it was parsed, the plan's otherwise."""

    rows: int | None = None
    """Positions loaded — or waiting, when :attr:`deferred`."""

    deferred: bool = False
    """Loaded without its holdings, because its CIK is not yet a known filer."""

    error: str | None = None
    """``ExceptionType: message``, the same text the queue row keeps."""

    @classmethod
    def failed(cls, filing: PlannedFiling, failure: BaseException) -> Self:
        return cls(
            filing=filing,
            outcome=Outcome.FAILED,
            period=filing.period,
            error=describe(failure),
        )

    @classmethod
    def not_started(cls, filing: PlannedFiling) -> Self:
        return cls(filing=filing, outcome=Outcome.NOT_STARTED, period=filing.period)


@dataclass(frozen=True, slots=True)
class BackfillRun:
    """Every filing's result, in plan order, and why the run stopped early if it did."""

    results: tuple[FilingResult, ...]

    rate_limited: EdgarRateLimited | None
    """The block that stopped the run. ``None`` if EDGAR never blocked it."""

    def count(self, outcome: Outcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)


async def run_backfill(
    filings: Sequence[PlannedFiling],
    *,
    process: Callable[[PlannedFiling], Awaitable[FilingResult]],
    on_failure: Callable[[PlannedFiling, Exception], Awaitable[None]],
    on_result: Callable[[FilingResult], None],
    concurrency: int,
    stop: asyncio.Event,
) -> BackfillRun:
    """Give every filing to ``process``, ``concurrency`` at a time, until done or stopped.

    :param process: One filing, start to finish. Raises to fail it.
    :param on_failure: Awaited with whatever ``process`` raised, after it is
        logged. :func:`record_attempt` in production. Must not raise.
    :param on_result: Called as each filing finishes, in the order they
        finish, and not for the ones the run never started.
    :param concurrency: Filings in flight at once. All of them draw on the one
        process-wide EDGAR limiter, so past the handful it takes to keep that
        limiter busy, more buys nothing.
    :param stop: Checked before each filing starts. Set it to let the filings
        in flight finish and leave the rest :attr:`Outcome.NOT_STARTED`. This
        function sets it too, on a rate-limit block.

    Cancelling the call abandons the filings in flight, whose loads roll back.
    """
    semaphore = asyncio.Semaphore(concurrency)
    rate_limited: EdgarRateLimited | None = None

    async def one(filing: PlannedFiling) -> FilingResult:
        nonlocal rate_limited
        async with semaphore:
            if stop.is_set():
                return FilingResult.not_started(filing)
            # Each filing runs in its own task, so this binds for it alone.
            structlog.contextvars.bind_contextvars(
                accession_no=filing.accession_no, cik=filing.cik, filer_slug=filing.slug
            )
            try:
                result = await process(filing)
            except Exception as failure:
                if isinstance(failure, EdgarRateLimited):
                    # Before anything else is awaited, so that no filing waiting
                    # on the semaphore starts in the meantime.
                    stop.set()
                    rate_limited = rate_limited or failure
                logger.exception("filing.failed", error=describe(failure))
                await on_failure(filing, failure)
                result = FilingResult.failed(filing, failure)
        on_result(result)
        return result

    results = await asyncio.gather(*(one(filing) for filing in filings))
    return BackfillRun(results=tuple(results), rate_limited=rate_limited)


async def record_attempt(
    sessions: async_sessionmaker[AsyncSession], filing: PlannedFiling, failure: Exception
) -> None:
    """Count a failed attempt on the filing's queue row, if it has one.

    Best effort, like ``ingest-filing``'s: the likeliest reason this write fails
    is the one the filing failed for — the database is gone — and raising here
    would lose the filing's own error for a second-hand one.
    """
    if not filing.queued:
        return
    try:
        async with sessions.begin() as session:
            await record_failure(session, filing.accession_no, describe(failure))
    except Exception as unrecorded:
        logger.warning("pending_filing.record_failed", error=str(unrecorded))


def describe(failure: BaseException) -> str:
    """``ExceptionType: message``: what the queue row, the log and the summary all say."""
    return f"{type(failure).__name__}: {failure}"


@contextmanager
def stop_on_interrupt(*, notify: Callable[[], None]) -> Iterator[asyncio.Event]:
    """An event the first Ctrl-C inside the block sets, instead of raising.

    The second is left to Python, which raises :class:`KeyboardInterrupt`:
    the handler removes itself as it fires, so whoever is at the terminal can
    always get out of a run whose in-flight work is waiting out a ten-minute
    EDGAR block.

    Must be entered on the running loop, in the main thread. Where signal
    handlers cannot be installed (Windows, or another thread) Ctrl-C keeps its
    default meaning and aborts.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def first() -> None:
        loop.remove_signal_handler(signal.SIGINT)
        stop.set()
        notify()

    try:
        loop.add_signal_handler(signal.SIGINT, first)
    except (NotImplementedError, RuntimeError):
        installed = False
    else:
        installed = True
    try:
        yield stop
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
