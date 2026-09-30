"""Which 13Fs a tracked filer has filed that we have not loaded.

Discovery is kept apart from ingestion so that each can be stopped and re-run
on its own, and so that the backlog between them is a table someone can query
rather than a loop's local variable. :func:`discover_filings` lists a filer's
13F-HRs and amendments from EDGAR, subtracts the ones already loaded, and
writes the difference to ``pending_filing``; ``ingest-filing`` drains it.

A set difference, not "since the last filing date"
--------------------------------------------------
The obvious incremental design asks EDGAR for everything filed after the newest
filing we hold. It is cheaper by nothing — the submissions index is one
document per CIK whichever way it is read — and it forgets: a filing that
failed to load three weeks ago is older than one that loaded yesterday, so it
is never asked about again. Comparing the whole listing against what is loaded
finds it on every run with no special case, and it finds the filing whose row
someone deleted, and the amendment filed late for a period long closed.

"Loaded" means :data:`~app.db.models.filing.LOADED_STATUSES`, not "has a
``filing`` row". A row whose parse is ``pending`` or ``failed`` has no
holdings, and ``ingest-filing`` treats it as work to do; discovery has to agree
with it or it reports as done a filing the command would refuse to skip.

Short sessions
--------------
:func:`discover_filings` opens one session to read the filer and another to
write the result, and holds neither across the EDGAR requests between them. A
filer with an old CIK is several pages of submissions, each paced by the rate
limiter, and a transaction left idle over that blocks migrations for no reason.
Each filer commits on its own, so a run that dies on filer sixty has queued the
first fifty-nine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.filer import Filer, FilerCik
from app.db.models.filing import LOADED_STATUSES, Filing
from app.db.models.pending_filing import PendingFiling, PendingStatus
from app.ingestion.edgar.client import EdgarClient, EdgarServerError
from app.ingestion.edgar.submissions import (
    FilerNotFoundError,
    FilingRef,
    SubmissionMalformedError,
    filter_forms,
    list_filings,
)

logger = get_logger(__name__)

#: What discovery queues: ``13F-HR`` and, through :func:`filter_forms`'s
#: prefix match, ``13F-HR/A``. Not ``13F-NT``: a notice has no holdings of its
#: own and nothing downstream counts it.
DISCOVERED_FORM: Final = "13F-HR"

#: How far back a run looks unless told otherwise. Far enough for every quarter
#: the site shows by default; ``--all`` goes to the start of a CIK's history.
DEFAULT_LOOKBACK_YEARS: Final = 5


class UnknownFilerError(LookupError):
    """No filer with that id or slug. Almost always a typo in ``--filer``."""


@dataclass(frozen=True, slots=True)
class TrackedFiler:
    """A filer and the CIKs it files under, highest priority last."""

    id: int
    slug: str
    ciks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveredFiling:
    """A listing row, and the CIK whose index it was listed under.

    The CIK travels with the row because :class:`FilingRef` does not carry one,
    and it is the one fact ``ingest-filing`` cannot recover later: the archive
    directory is keyed on it, not on the accession number's leading digits.
    """

    cik: str
    filing: FilingRef

    @property
    def accession_no(self) -> str:
        return self.filing.accession_no


@dataclass(frozen=True, slots=True)
class CikFailure:
    """One CIK whose submissions could not be read, and why."""

    cik: str
    error: str


@dataclass(frozen=True, slots=True)
class FilerDiscovery:
    """What one filer's discovery found, and what it did about it."""

    filer: TrackedFiler

    found: tuple[DiscoveredFiling, ...]
    """Every 13F-HR and 13F-HR/A in range, across the filer's CIKs, oldest first."""

    already_ingested: frozenset[str]
    """Accession numbers in :attr:`found` that are loaded."""

    new: tuple[DiscoveredFiling, ...]
    """:attr:`found` less :attr:`already_ingested`: what is now in ``pending_filing``.

    Includes filings an earlier run queued and nobody has loaded since. That is
    the set difference doing its job, not a double count — the queue holds each
    of them once.
    """

    failures: tuple[CikFailure, ...]
    """CIKs that could not be listed. Their filings are missing from
    :attr:`found`, and the next run will look again."""


def default_since(today: date) -> date:
    """:data:`DEFAULT_LOOKBACK_YEARS` before ``today``; 29 February falls back a day."""
    try:
        return today.replace(year=today.year - DEFAULT_LOOKBACK_YEARS)
    except ValueError:
        return today.replace(year=today.year - DEFAULT_LOOKBACK_YEARS, day=28)


async def tracked_filer_ids(session: AsyncSession, *, slug: str | None = None) -> list[int]:
    """Every filer's id, by slug, or the one filer ``slug`` names.

    :raises UnknownFilerError: ``slug`` is given and no filer has it.
    """
    statement = select(Filer.id).order_by(Filer.slug)
    if slug is not None:
        statement = statement.where(Filer.slug == slug)
    ids = list(await session.scalars(statement))
    if slug is not None and not ids:
        raise UnknownFilerError(f"no filer has the slug {slug!r}")
    return ids


async def discover_filings(
    sessions: async_sessionmaker[AsyncSession],
    edgar: EdgarClient,
    filer_id: int,
    *,
    since: date | None,
) -> FilerDiscovery:
    """List one filer's 13Fs, and queue the ones that are not loaded.

    :param sessions: Where to open the two short sessions described in the
        module docstring. The write commits before this returns.
    :param edgar: An open client. One request per CIK, plus overflow pages.
    :param filer_id: The filer to discover for.
    :param since: The earliest ``filingDate`` to include, or ``None`` for the
        whole history.
    :raises UnknownFilerError: No filer has that id.
    :raises EdgarRateLimited: EDGAR is blocking us. Not caught per CIK,
        because every CIK after this one would be blocked too.
    """
    async with sessions() as session:
        filer = await _tracked_filer(session, filer_id)

    found, failures = await find_filings(edgar, filer, since=since)

    async with sessions.begin() as session:
        already_ingested, new = await enqueue(session, found)

    logger.info(
        "filings.discovered",
        filer=filer.slug,
        found=len(found),
        already_ingested=len(already_ingested),
        new=len(new),
        failed_ciks=len(failures),
    )
    return FilerDiscovery(
        filer=filer,
        found=found,
        already_ingested=already_ingested,
        new=new,
        failures=failures,
    )


async def find_filings(
    edgar: EdgarClient, filer: TrackedFiler, *, since: date | None
) -> tuple[tuple[DiscoveredFiling, ...], tuple[CikFailure, ...]]:
    """The EDGAR half: every 13F-HR in range under any of ``filer``'s CIKs.

    A CIK that cannot be read is recorded and skipped rather than failing the
    filer, so that one dead predecessor CIK does not stop the live one being
    discovered. An accession number listed under two of the filer's CIKs — a
    co-filed report — is kept once, under the first.
    """
    found: dict[str, DiscoveredFiling] = {}
    failures: list[CikFailure] = []
    for cik in filer.ciks:
        try:
            history = await list_filings(edgar, cik=cik)
        except (
            FilerNotFoundError,
            SubmissionMalformedError,
            EdgarServerError,
            # A 404 on an overflow page, a 400, a connection lost after retries.
            httpx.HTTPError,
        ) as failure:
            logger.warning(
                "filings.discovery_failed", filer=filer.slug, cik=cik, error=str(failure)
            )
            failures.append(CikFailure(cik=cik, error=str(failure)))
            continue
        for filing in filter_forms(history.filings, DISCOVERED_FORM):
            if since is None or filing.filing_date >= since:
                found.setdefault(
                    filing.accession_no, DiscoveredFiling(cik=history.cik, filing=filing)
                )

    ordered = sorted(
        found.values(), key=lambda row: (row.filing.filing_date, row.filing.accession_no)
    )
    return tuple(ordered), tuple(failures)


async def enqueue(
    session: AsyncSession, found: tuple[DiscoveredFiling, ...]
) -> tuple[frozenset[str], tuple[DiscoveredFiling, ...]]:
    """The set difference, written down. Returns (already ingested, new).

    New filings are inserted as ``pending``; one already queued keeps its row —
    its ``discovered_at``, its attempts and its last error — unless the row
    says ``done``, which a filing that is not loaded cannot be, so it goes back
    to ``pending``. The loaded side is reconciled the same way: a queue row for
    a filing that is loaded is marked ``done`` whoever loaded it. Both are how
    the queue heals when something changed ``filing`` without going through
    ``ingest-filing``.

    Does not commit.
    """
    if not found:
        return frozenset(), ()

    loaded = frozenset(
        await session.scalars(
            select(Filing.accession_no).where(
                Filing.accession_no.in_([row.accession_no for row in found]),
                Filing.parse_status.in_(LOADED_STATUSES),
            )
        )
    )
    new = tuple(row for row in found if row.accession_no not in loaded)

    if new:
        insert = pg_insert(PendingFiling).values(
            [
                {
                    "accession_no": row.accession_no,
                    "cik": row.cik,
                    "form_type": row.filing.form_type,
                    "filing_date": row.filing.filing_date,
                    "report_date": row.filing.report_date,
                }
                for row in new
            ]
        )
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=[PendingFiling.accession_no],
                set_={"status": PendingStatus.PENDING.value},
                where=PendingFiling.status == PendingStatus.DONE.value,
            )
        )
    if loaded:
        await session.execute(
            update(PendingFiling)
            .where(
                PendingFiling.accession_no.in_(loaded),
                PendingFiling.status != PendingStatus.DONE.value,
            )
            .values(status=PendingStatus.DONE.value)
        )
    return loaded, new


# --- what ingestion writes back ----------------------------------------------


@dataclass(frozen=True, slots=True)
class QueuedFiling:
    """The queue row's two columns ``ingest-filing`` reads before it starts."""

    cik: str
    status: str


async def queued_filing(session: AsyncSession, accession_no: str) -> QueuedFiling | None:
    row = (
        await session.execute(
            select(PendingFiling.cik, PendingFiling.status).where(
                PendingFiling.accession_no == accession_no
            )
        )
    ).first()
    return None if row is None else QueuedFiling(cik=row.cik, status=row.status)


async def mark_ingested(session: AsyncSession, accession_no: str) -> None:
    """Mark a queued filing ``done``. A no-op for one that was never queued.

    Called inside the loader's transaction, so the queue cannot say ``done``
    for a load that rolled back.
    """
    await session.execute(
        update(PendingFiling)
        .where(
            PendingFiling.accession_no == accession_no,
            PendingFiling.status != PendingStatus.DONE.value,
        )
        .values(status=PendingStatus.DONE.value)
    )


async def record_failure(session: AsyncSession, accession_no: str, error: str) -> None:
    """Count a failed attempt on a queued filing and keep its message.

    Leaves a ``done`` row alone: that is a forced reload of a filing that is
    still loaded, since the failed attempt rolled back, and marking it failed
    would queue for replay something that needs nothing.
    """
    await session.execute(
        update(PendingFiling)
        .where(
            PendingFiling.accession_no == accession_no,
            PendingFiling.status != PendingStatus.DONE.value,
        )
        .values(
            status=PendingStatus.FAILED.value,
            attempts=PendingFiling.attempts + 1,
            last_error=error,
        )
    )


async def _tracked_filer(session: AsyncSession, filer_id: int) -> TrackedFiler:
    slug = await session.scalar(select(Filer.slug).where(Filer.id == filer_id))
    if slug is None:
        raise UnknownFilerError(f"no filer has the id {filer_id}")
    ciks = await session.scalars(
        select(FilerCik.cik)
        .where(FilerCik.filer_id == filer_id)
        .order_by(FilerCik.priority, FilerCik.cik)
    )
    return TrackedFiler(id=filer_id, slug=slug, ciks=tuple(ciks))
