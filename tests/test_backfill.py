"""The backfill engine: how many at once, what one failure costs, and how it stops.

No database and no EDGAR: ``process`` is a stand-in, so what is asserted here is
the engine's own contract — the semaphore, the isolation of one filing's failure
from the rest, and a stop that finishes what is in flight and starts nothing
else. What a filing's processing does to the database is
tests/integration/test_cli_backfill.py's subject.
"""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Final

import structlog

from app.ingestion.backfill import (
    FilingResult,
    Outcome,
    PlannedFiling,
    run_backfill,
    stop_on_interrupt,
)
from app.ingestion.edgar.client import EdgarRateLimited

PERIOD: Final = date(2024, 3, 31)


def _filing(number: int) -> PlannedFiling:
    return PlannedFiling(
        accession_no=f"0001067983-24-{number:06d}",
        cik="0001067983",
        slug="berkshire-hathaway",
        filing_date=date(2024, 5, 15),
        period=PERIOD,
        queued=True,
        loaded=None,
    )


def _ok(filing: PlannedFiling) -> FilingResult:
    return FilingResult(filing=filing, outcome=Outcome.OK, period=PERIOD, rows=41)


class _Recorder:
    """Stands in for the queue: what ``on_failure`` and ``on_result`` were given."""

    def __init__(self) -> None:
        self.failures: list[tuple[str, Exception]] = []
        self.reported: list[FilingResult] = []

    async def on_failure(self, filing: PlannedFiling, failure: Exception) -> None:
        self.failures.append((filing.accession_no, failure))

    def on_result(self, result: FilingResult) -> None:
        self.reported.append(result)


async def _run(
    filings: list[PlannedFiling],
    process: Callable[[PlannedFiling], Awaitable[FilingResult]],
    recorder: _Recorder,
    *,
    concurrency: int = 5,
    stop: asyncio.Event | None = None,
) -> tuple[FilingResult, ...]:
    run = await run_backfill(
        filings,
        process=process,
        on_failure=recorder.on_failure,
        on_result=recorder.on_result,
        concurrency=concurrency,
        stop=stop or asyncio.Event(),
    )
    return run.results


async def test_no_more_than_concurrency_filings_are_in_flight() -> None:
    in_flight = 0
    most = 0

    async def process(filing: PlannedFiling) -> FilingResult:
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _ok(filing)

    results = await _run([_filing(n) for n in range(12)], process, _Recorder(), concurrency=3)

    assert most == 3
    assert [result.outcome for result in results] == [Outcome.OK] * 12


async def test_results_come_back_in_plan_order_whatever_order_they_finish_in() -> None:
    async def process(filing: PlannedFiling) -> FilingResult:
        # The first filing is the slowest, so it finishes last.
        await asyncio.sleep(0.03 if filing.accession_no.endswith("0") else 0)
        return _ok(filing)

    filings = [_filing(n) for n in range(4)]
    recorder = _Recorder()

    results = await _run(filings, process, recorder)

    assert [result.filing for result in results] == filings
    assert recorder.reported[-1].filing == filings[0]


async def test_a_failing_filing_is_recorded_and_the_rest_carry_on() -> None:
    boom = ValueError("no <infoTable> in the document")

    async def process(filing: PlannedFiling) -> FilingResult:
        if filing.accession_no.endswith("2"):
            raise boom
        return _ok(filing)

    recorder = _Recorder()

    results = await _run([_filing(n) for n in range(5)], process, recorder)

    assert [result.outcome for result in results] == [
        Outcome.OK,
        Outcome.OK,
        Outcome.FAILED,
        Outcome.OK,
        Outcome.OK,
    ]
    assert results[2].error == "ValueError: no <infoTable> in the document"
    assert recorder.failures == [(_filing(2).accession_no, boom)]
    assert len(recorder.reported) == 5


async def test_stopping_finishes_what_is_in_flight_and_starts_nothing_else() -> None:
    """What the first Ctrl-C does. The filings already started complete —
    loaded, not abandoned — and the rest are left for the next run."""
    stop = asyncio.Event()
    started: list[str] = []

    async def process(filing: PlannedFiling) -> FilingResult:
        started.append(filing.accession_no)
        if len(started) == 2:
            stop.set()
        await asyncio.sleep(0.01)
        return _ok(filing)

    recorder = _Recorder()

    results = await _run(
        [_filing(n) for n in range(6)], process, recorder, concurrency=2, stop=stop
    )

    assert [result.outcome for result in results] == [Outcome.OK] * 2 + [Outcome.NOT_STARTED] * 4
    assert len(started) == 2
    # Only what ran is reported; the summary accounts for the rest.
    assert [result.outcome for result in recorder.reported] == [Outcome.OK] * 2


async def test_a_rate_limit_block_stops_the_run() -> None:
    """SEC blocks by IP, so every filing after this one would fail the same way."""
    blocked = EdgarRateLimited(
        url="https://data.sec.gov/submissions/CIK0001067983.json",
        status_code=403,
        retry_after=600.0,
    )

    async def process(filing: PlannedFiling) -> FilingResult:
        if filing.accession_no.endswith("1"):
            raise blocked
        return _ok(filing)

    recorder = _Recorder()
    run = await run_backfill(
        [_filing(n) for n in range(5)],
        process=process,
        on_failure=recorder.on_failure,
        on_result=recorder.on_result,
        concurrency=1,
        stop=asyncio.Event(),
    )

    assert run.rate_limited is blocked
    assert [result.outcome for result in run.results] == [
        Outcome.OK,
        Outcome.FAILED,
        Outcome.NOT_STARTED,
        Outcome.NOT_STARTED,
        Outcome.NOT_STARTED,
    ]
    # Recorded like any other failure: the queue row says why, and the next
    # run retries it.
    assert recorder.failures == [(_filing(1).accession_no, blocked)]


async def test_each_filing_logs_under_its_own_accession_number() -> None:
    """Interleaved filings must not overwrite each other's context, or one
    grep for an accession number returns another filing's lines."""
    seen: dict[str, object] = {}

    async def process(filing: PlannedFiling) -> FilingResult:
        await asyncio.sleep(0)
        seen[filing.accession_no] = structlog.contextvars.get_contextvars()["accession_no"]
        return _ok(filing)

    await _run([_filing(n) for n in range(6)], process, _Recorder(), concurrency=6)

    assert seen == {accession: accession for accession in seen}
    assert len(seen) == 6


# --- Ctrl-C ------------------------------------------------------------------


async def test_the_first_ctrl_c_sets_the_event_and_the_second_is_left_to_python() -> None:
    notified: list[bool] = []

    with stop_on_interrupt(notify=lambda: notified.append(True)) as stop:
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop.wait(), timeout=1)

        assert notified == [True]
        # The handler has removed itself, so the next Ctrl-C raises
        # KeyboardInterrupt the ordinary way and aborts the run.
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


async def test_leaving_the_block_restores_the_default_ctrl_c() -> None:
    with stop_on_interrupt(notify=lambda: None) as stop:
        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler

    assert not stop.is_set()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
