"""Writing one parsed 13F into the database, as many times as you like.

This module exists because ingestion is re-run. Not exceptionally — routinely,
and by design. Celery delivers at least once, so a task that timed out after
committing arrives a second time. A backfill that died on filing 40,000 is
resumed from the quarter's start because nobody wrote down where it stopped. A
parser bug found in March is repaired by re-parsing every filing since January
from bytes already on disk. Each of those is a normal Tuesday, and each of them
runs :func:`load_filing` over a filing that is already loaded.

So the property this module is built around is not "does it write the rows" but
**running it twice leaves the database exactly as running it once did**. Two
things get it wrong in ways nobody notices for a quarter:

*Duplicating.* Insert instead of upsert, and a re-run gives one filer two
filings and two sets of holdings for one period. Every total doubles. Nothing
raises, no row looks wrong on its own, and a portfolio reporting twice what it
holds reads as a large fund rather than as a bug.

*Merging.* Upsert the holdings row by row on their natural key and a re-ingest
of a *corrected* filing — an amendment restating a position, a re-parse that
now reads a row the old parser dropped — leaves the positions that are no longer
in the document sitting in the table forever, because nothing in an upsert
deletes. The filing then reports a position it does not contain, which is worse
than reporting nothing.

Hence the shape below: upsert the filing on its accession number, delete every
holding it has and insert the ones the document says it has now. See
:func:`load_filing` for why deleting is right and diffing is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.filer import FilerCik
from app.db.models.filing import Filing
from app.db.models.holding import Holding
from app.db.models.security import Security
from app.ingestion.normalisation import NormalisedFiling, NormalisedHolding
from app.ingestion.parsers.thirteen_f import PrimaryDoc

logger = get_logger(__name__)

#: The columns a holding row carries over from the information table, in the
#: order the natural key uses them. Named because :func:`_positions` groups on
#: exactly this tuple and the unique constraint is spelled the same way.
_NaturalKey = tuple[str, str | None, str]


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What one call to :func:`load_filing` did.

    Returned rather than logged and discarded, because the counts are what a
    backfill's own reporting is built from — "3,000 filings, 412,000 holdings,
    9,100 new securities" is assembled here or not at all — and because
    :attr:`holdings_deferred` is a condition the caller has to be able to act
    on rather than read about in a log.
    """

    filing_id: int
    """The upserted filing's primary key. Stable across re-ingests."""

    filer_id: int | None
    """The filer the filing is linked to now, ``None`` while its CIK is unknown."""

    holdings_loaded: int
    """Rows written to ``holding``. Zero for a NOTICE, and zero when deferred."""

    securities_created: int
    """CUSIPs seen here for the first time, and given an unresolved row."""

    rows_collapsed: int
    """Information-table rows folded into another row sharing its natural key.

    Nonzero is normal for a filer who reports one position across several
    ``otherManager`` lines; see :func:`_positions`.
    """

    holdings_deferred: bool
    """True when the filing loaded but its holdings could not: no filer yet.

    Not an error and not a silence — ``holding.filer_id`` is ``NOT NULL``, so
    there is no row to write until the CIK resolves. The filing is in the table,
    which is what makes re-running this after resolution finish the job.
    """


async def load_filing(
    session: AsyncSession,
    *,
    accession_no: str,
    filed_at: datetime,
    primary_doc: PrimaryDoc,
    normalised: NormalisedFiling,
    raw_key: str | None = None,
    source_url: str | None = None,
) -> LoadResult:
    """Write a parsed 13F, replacing whatever an earlier run left behind.

    Idempotent on ``accession_no``: call it three times with the same inputs and
    the database holds one filing and one set of holdings, with the same primary
    keys it had after the first call.

    :param session: The unit of work to write into. Not committed here — see
        below on why the transaction boundary belongs to the caller.
    :param accession_no: EDGAR's dashed identifier, ``0001067983-24-000011``.
        A parameter rather than a field of ``primary_doc`` because the document
        does not contain it: it is assigned by EDGAR at acceptance and known
        only from the index that led us to the document.
    :param filed_at: When EDGAR accepted the submission, timezone-aware. Also
        from the index, and load-bearing — it is what decided the multiplier in
        ``normalised``.
    :param primary_doc: The parsed cover page: filer, form type, period.
    :param normalised: The output of :func:`~app.ingestion.normalisation.normalise_filing`
        — the rows in whole dollars, the multiplier used, and the verdict of
        every guard. The AC calls this argument ``rows``; it is the whole
        :class:`~app.ingestion.normalisation.NormalisedFiling` because the rows
        do not travel alone. ``value_multiplier``, ``parse_status`` and
        ``parse_notes`` are all facts about *these* rows, and passing them as
        four separate arguments makes it possible for a caller to pair one
        filing's positions with another filing's verdict — a mistake with no
        symptom, since both sets of numbers are individually plausible.
    :param raw_key: Object key of the archived document, when the caller knows
        it. ``None`` never erases a key already on the row.
    :param source_url: The EDGAR URL it was fetched from, same rule.
    :returns: A :class:`LoadResult` with the counts and whether holdings had to
        be deferred.

    **One transaction.** Everything below runs inside a savepoint, so a failure
    anywhere — a check constraint on row 2,900 of 3,000, a connection lost
    mid-insert — leaves the database with neither the filing nor any of its
    holdings, rather than with a filing that claims a parse it does not have the
    rows for. The final ``COMMIT`` is the caller's: a load is usually one step
    of a task that also marks a queue entry done, and a loader that committed
    on its own would split that into two units of work that can half-succeed.

    **Delete and reinsert, not diff.** Holdings have no identity of their own in
    the source — an information table is a list, with no row id anywhere in it —
    so "the same position, changed" and "one position gone, another added" are
    indistinguishable from the document. A diff would therefore be inventing an
    identity in order to preserve surrogate keys that nothing references: no
    foreign key points at ``holding.id``, and every derived figure downstream is
    recomputed from the rows rather than patched. That leaves the delete costing
    one index scan on a few thousand rows, against a diff that has to be right
    about a question the data cannot answer.
    """
    filer_id = await session.scalar(
        select(FilerCik.filer_id).where(FilerCik.cik == primary_doc.cik)
    )

    async with session.begin_nested():
        filing_id, linked_filer_id = await _upsert_filing(
            session,
            accession_no=accession_no,
            filed_at=filed_at,
            primary_doc=primary_doc,
            normalised=normalised,
            filer_id=filer_id,
            raw_key=raw_key,
            source_url=source_url,
        )

        positions = _positions(normalised.holdings)
        collapsed = len(normalised.holdings) - len(positions)

        if linked_filer_id is None:
            # holding.filer_id is NOT NULL, and deliberately so: every read path
            # filters on filer and period, which is why both are denormalised
            # onto this table. So there is no such thing as a holding belonging
            # to nobody, and the rows wait until the CIK resolves. Nothing is
            # deleted on this path either — a filing with no filer has no
            # holdings to delete.
            if positions:
                logger.warning(
                    "holdings_deferred_unknown_filer",
                    accession_no=accession_no,
                    cik=primary_doc.cik,
                    rows=len(positions),
                )
            return LoadResult(
                filing_id=filing_id,
                filer_id=None,
                holdings_loaded=0,
                securities_created=0,
                rows_collapsed=collapsed,
                holdings_deferred=True,
            )

        securities_created, security_ids = await _upsert_securities(
            session, _issuer_names(normalised.holdings)
        )

        # Unconditional, including when the filing now has no rows at all: a
        # 13F-HR/A that restates a period down to nothing has to be able to
        # empty the table, and a re-parse that dropped every row should leave a
        # filing with no holdings rather than the previous parse's.
        await session.execute(delete(Holding).where(Holding.filing_id == filing_id))

        if positions:
            await session.execute(
                insert(Holding),
                [
                    {
                        "filing_id": filing_id,
                        "security_id": security_ids[position["cusip"]],
                        "filer_id": linked_filer_id,
                        "period_of_report": primary_doc.period_of_report,
                        **position,
                    }
                    for position in positions
                ],
            )

    if collapsed:
        logger.info(
            "holding_rows_collapsed",
            accession_no=accession_no,
            collapsed=collapsed,
            rows=len(normalised.holdings),
        )

    return LoadResult(
        filing_id=filing_id,
        filer_id=linked_filer_id,
        holdings_loaded=len(positions),
        securities_created=securities_created,
        rows_collapsed=collapsed,
        holdings_deferred=False,
    )


async def _upsert_filing(
    session: AsyncSession,
    *,
    accession_no: str,
    filed_at: datetime,
    primary_doc: PrimaryDoc,
    normalised: NormalisedFiling,
    filer_id: int | None,
    raw_key: str | None,
    source_url: str | None,
) -> tuple[int, int | None]:
    """Insert the filing, or bring the existing row up to date with this parse.

    :returns: The filing's id and the filer it is linked to *after* the write,
        which is not always the ``filer_id`` passed in — see the coalesces
        below. The holdings hang off the stored value, so it is the one that has
        to come back.
    """
    values: dict[str, Any] = {
        "accession_no": accession_no,
        "cik": primary_doc.cik,
        "filer_id": filer_id,
        "form_type": primary_doc.form_type,
        "period_of_report": primary_doc.period_of_report,
        "filed_at": filed_at,
        "value_multiplier": normalised.value_multiplier,
        "amendment_kind": primary_doc.amendment_kind,
        "report_type": primary_doc.report_type,
        "parsed_at": func.now(),
        # A parse that got this far is not a failed one. Clearing this is what
        # makes re-parsing the fix for a filing that failed under an older
        # parser, rather than something that leaves a stale reason behind on a
        # row that now has holdings.
        "parse_error": None,
        "parse_status": normalised.parse_status.value,
        "parse_notes": normalised.parse_notes_json,
        "raw_key": raw_key,
        "source_url": source_url,
        "ingested_at": func.now(),
    }

    statement = pg_insert(Filing).values(**values)
    upsert = statement.on_conflict_do_update(
        index_elements=[Filing.accession_no],
        set_={
            "cik": statement.excluded.cik,
            # coalesce, not a plain overwrite: this runs with whatever
            # filer_cik held a moment ago, and a lookup that missed is not
            # evidence that the link is wrong. Overwriting would let a re-ingest
            # during a filer merge unlink every filing it touched, taking the
            # holdings' NOT NULL filer with it. A *changed* mapping still
            # propagates, because then the excluded value is not null.
            "filer_id": func.coalesce(statement.excluded.filer_id, Filing.filer_id),
            "form_type": statement.excluded.form_type,
            "period_of_report": statement.excluded.period_of_report,
            "filed_at": statement.excluded.filed_at,
            "value_multiplier": statement.excluded.value_multiplier,
            "amendment_kind": statement.excluded.amendment_kind,
            "report_type": statement.excluded.report_type,
            "parsed_at": statement.excluded.parsed_at,
            "parse_error": statement.excluded.parse_error,
            "parse_status": statement.excluded.parse_status,
            "parse_notes": statement.excluded.parse_notes,
            # Same rule as filer_id, for the same reason: a re-parse from bytes
            # already on disk knows the rows but not where they were fetched
            # from, and it must not blank the only pointer to them.
            "raw_key": func.coalesce(statement.excluded.raw_key, Filing.raw_key),
            "source_url": func.coalesce(statement.excluded.source_url, Filing.source_url),
            "ingested_at": statement.excluded.ingested_at,
        },
        # Deliberately absent: id, accession_no and amends_id. The first two are
        # the identity being conflicted on. amends_id belongs to the step that
        # links an amendment to what it amends, which needs the other filing to
        # exist and so cannot run here; overwriting it with the NULL this
        # statement inserts would undo that work on every re-ingest.
    ).returning(Filing.id, Filing.filer_id)

    row = (await session.execute(upsert)).one()
    return int(row.id), row.filer_id


async def _upsert_securities(
    session: AsyncSession, names: dict[str, str | None]
) -> tuple[int, dict[str, int]]:
    """Make sure every CUSIP in the filing has a security row, and map them to ids.

    An unseen CUSIP gets a row with the filing's own ``nameOfIssuer`` and
    nothing else: no ticker, no FIGI, no ``resolved_at``. That is what
    *unresolved* is spelled as in this schema, and it is a normal, permanent
    state for delisted names and obscure instruments — never a reason to drop
    the holding, which would quietly shrink the portfolio it belongs to.

    ``ON CONFLICT DO NOTHING`` rather than ``DO UPDATE``, and the "nothing" is
    the point twice over. It makes two workers parsing two filings that both
    hold Apple safe against each other — the loser of the race reads the
    winner's row instead of raising. And it stops a re-ingest from overwriting
    enrichment: by the time a filing is loaded a second time, OpenFIGI may have
    filled in the ticker, and a ``DO UPDATE`` carrying the filing's abbreviated
    ``nameOfIssuer`` would undo that on every pass.

    :param names: ``cusip -> nameOfIssuer`` for every CUSIP in the filing.
    :returns: How many rows this call created, and ``cusip -> security.id`` for
        every CUSIP passed in.
    """
    if not names:
        return 0, {}

    created = await session.scalars(
        pg_insert(Security)
        .values([{"cusip": cusip, "name": name} for cusip, name in names.items()])
        .on_conflict_do_nothing(index_elements=[Security.cusip])
        .returning(Security.cusip)
    )
    created_count = len(created.all())

    # A second statement because DO NOTHING returns only the rows it inserted:
    # the ones that already existed are exactly what RETURNING cannot see, and
    # they are most of them on any run after the first.
    rows = await session.execute(
        select(Security.cusip, Security.id).where(Security.cusip.in_(names))
    )
    return created_count, {cusip: security_id for cusip, security_id in rows}


def _positions(holdings: tuple[NormalisedHolding, ...]) -> list[dict[str, Any]]:
    """The information table as holding rows, one per natural key.

    Order is preserved from the document, so the rows land in the table in the
    order the filer wrote them.

    The collapsing is not tidiness, it is the difference between loading a real
    filing and failing on one. ``holding``'s natural key is
    ``(filing_id, cusip, put_call, sshprnamt_type)`` with ``NULLS NOT
    DISTINCT``, and a filer who reports one position across several
    ``otherManager`` lines — normal, and how affiliated managers disclose a
    jointly managed book — writes several information-table rows that reduce to
    that same key. ``holding`` has no ``otherManager`` column to tell them
    apart, so the schema has already decided these are one position; the only
    question left is what its numbers are.

    Summed, therefore, rather than last-one-wins. The filing's own
    ``tableValueTotal`` is the sum over every row, and the checksum guard in
    :mod:`app.ingestion.normalisation` has already checked the rows against it,
    so summing is what keeps the loaded portfolio equal to the filing's declared
    total. Keeping only the last line would silently drop the rest of the
    position — a real number, quietly missing, on the filings most likely to be
    large.

    The descriptive fields — issuer name, discretion — come from the first line
    of the group, which is the one the filer led with.
    """
    grouped: dict[_NaturalKey, dict[str, Any]] = {}
    for holding in holdings:
        row = holding.row
        key: _NaturalKey = (row.cusip, row.put_call, row.sh_prn_type)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "cusip": row.cusip,
                "value_usd": holding.value_usd,
                "shares": row.shares,
                "sshprnamt_type": row.sh_prn_type,
                "put_call": row.put_call,
                "investment_discretion": row.investment_discretion,
                "voting_sole": row.voting_sole,
                "voting_shared": row.voting_shared,
                "voting_none": row.voting_none,
            }
            continue
        existing["value_usd"] += holding.value_usd
        existing["shares"] += row.shares
        existing["voting_sole"] = _add(existing["voting_sole"], row.voting_sole)
        existing["voting_shared"] = _add(existing["voting_shared"], row.voting_shared)
        existing["voting_none"] = _add(existing["voting_none"], row.voting_none)

    return list(grouped.values())


def _issuer_names(holdings: tuple[NormalisedHolding, ...]) -> dict[str, str | None]:
    """``cusip -> nameOfIssuer``, taking the first spelling the filing used.

    First rather than last, and not reconciled between them: one filing can
    write two different names against one CUSIP (an abbreviation on one line, a
    fuller form on another), and there is no basis in the document for calling
    either the better one. The name is a display fallback for an unresolved
    security, superseded the moment enrichment or an issuer table has something
    real; picking deterministically matters more than picking well.
    """
    names: dict[str, str | None] = {}
    for holding in holdings:
        names.setdefault(holding.row.cusip, holding.row.name_of_issuer)
    return names


def _add(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """Sum two voting-authority figures, either of which may not have been filed.

    ``None`` means "this filer did not report it", which is not zero and must
    not become zero by being added to something: a group where one line reports
    its voting authority and another does not has a known part and an unknown
    part, and the honest total is the part that was reported.
    """
    if left is None:
        return right
    if right is None:
        return left
    return left + right
