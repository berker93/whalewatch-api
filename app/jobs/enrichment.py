"""The enrichment jobs: CUSIPs to tickers, and tickers to their latest close.

``resolve-cusips`` and ``fetch-quotes`` are these with a summary; the
``resolve_cusips`` and ``fetch_prices`` tasks are these in ``asyncio.run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.security import ResolutionSource
from app.db.queries.cusip_coverage import CusipCoverage, cusip_coverage
from app.db.session import create_engine, create_session_factory
from app.ingestion.enrichment.figi import DatabaseResponseLog, OpenFIGIClient
from app.ingestion.enrichment.quotes import QuoteReport, fetch_quotes, quote_provider
from app.ingestion.enrichment.resolution import ResolutionReport, resolve_securities
from app.jobs.cache import invalidate_cache
from app.jobs.tracking import TrackedRun, tracked_task

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Resolved:
    """What one resolution run did, and the coverage it left."""

    report: ResolutionReport
    coverage: CusipCoverage


@tracked_task("resolve-cusips")
async def resolve_cusips(
    run: TrackedRun, settings: Settings, *, limit: int | None, force: bool
) -> Resolved:
    """Resolve unresolved securities, highest aggregate holdings value first.

    OpenFIGI, then the SEC's 13(f) list, then fuzzy candidates queued for
    review (app/ingestion/enrichment/resolution.py). Then the coverage that
    leaves, and the API's cache dropped if anything a reader sees changed.

    :param limit: Only the ``limit`` securities of highest value.
    :param force: Every security, asking OpenFIGI again.
    :raises OpenFIGIRateLimited: Over quota, after the client's own retries.
    """
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with OpenFIGIClient(settings, DatabaseResponseLog(sessions)) as figi:
            report = await resolve_securities(sessions, figi, limit=limit, force=force)
        async with sessions() as session:
            coverage = await cusip_coverage(session, top=0)
    finally:
        await engine.dispose()
    run.items_seen = report.candidates
    run.items_written = report.changed
    delistings = report.delistings
    if report.changed or (delistings and (delistings.flagged or delistings.relisted)):
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    run.metrics |= {
        "resolved": {
            (source.value if source is not None else "none"): report.count(source)
            for source in (*ResolutionSource, None)
            if report.count(source)
        },
        "misses_retried": report.misses_retried,
        "kept": report.kept,
        "renamed": report.renamed,
        "delisted_flagged": delistings.flagged if delistings else 0,
        "relisted": delistings.relisted if delistings else 0,
        "collisions": len(coverage.collisions),
        "reviews_queued": report.reviews_queued,
        "reviews_superseded": report.reviews_superseded,
        "coverage_value_pct": float_pct(coverage.value_pct),
        "coverage_row_pct": float_pct(coverage.row_pct),
    }
    logger.info(
        "resolution.finished",
        candidates=report.candidates,
        changed=report.changed,
        openfigi=report.count(ResolutionSource.OPENFIGI),
        sec_13f_list=report.count(ResolutionSource.SEC_13F_LIST),
        unnamed=report.count(None),
        reviews_queued=report.reviews_queued,
        coverage_value_pct=float_pct(coverage.value_pct),
    )
    return Resolved(report=report, coverage=coverage)


def _quotes_context(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    """The provider asked, which is a setting, alongside the tickers."""
    return {"provider": settings.quote_provider, **arguments}


@tracked_task("fetch-quotes", context=_quotes_context)
async def fetch_prices(
    run: TrackedRun, settings: Settings, *, tickers: list[str] | None
) -> QuoteReport:
    """The latest close of every listed security, or of ``tickers``, from the provider.

    One ticker failing is a line of the run's errors and the rest go on; the
    provider failing stops the run, keeping the closes written before it did.

    :param tickers: Only these; ``None`` for every listed security.
    :raises QuoteProviderUnavailableError: The provider is refused here, or
        not installed.
    :raises QuoteFetchError: The provider, not a ticker, failed.
    """
    chosen = quote_provider(settings)
    engine = create_engine(settings)
    try:
        report = await fetch_quotes(create_session_factory(engine), chosen, tickers=tickers or None)
    finally:
        await engine.dispose()
    run.items_seen = report.asked
    run.items_written = report.written
    if report.written:
        run.metrics["cache_invalidated"] = await invalidate_cache(settings)
    run.metrics |= {
        "provider": report.provider,
        "listed": report.listed,
        "latest": report.latest.isoformat() if report.latest else None,
        "missing": len(report.missing),
        "failed": len(report.failed),
        "ambiguous": len(report.ambiguous),
    }
    run.errors += [f"{ticker}: {error}" for ticker, error in report.failed.items()]
    return report


def float_pct(pct: Decimal | None) -> float | None:
    return None if pct is None else round(float(pct), 4)
