"""Writing one parsed Form 4 into the database, as many times as you like.

The same property as the 13F loader (:mod:`app.ingestion.loaders.filing`):
running it twice leaves the database as running it once did. A Form 4 makes
that harder in one way and easier in another.

Harder, because its lines have no identity in the source. Two tranches of one
sale, 500 shares at $41.20 on the same day, are equal in every field the form
has. The identity is the line's position in its document
(:attr:`~app.ingestion.parsers.form4.Form4Transaction.line_number`), which the
parser numbers and ``insider_transaction``'s unique key is built on, so a
re-run's lines conflict with the first run's and ``ON CONFLICT DO NOTHING``
drops them. The rows keep their ids, which ``insider_cluster`` will point at.

Easier, because amendments are simple. A 4/A replaces the Form 4 it amends
entirely, so the original's lines are deleted and the amendment's kept. There
are no additions to merge as there are for a 13F.

Order does not matter
---------------------
A 4/A names the filing it amends by the day it was filed
(``dateOfOriginalSubmission``), not by accession number. The loader finds it
among the issuer's Form 4s filed that day that share a reporting owner with
the amendment, and records it as :attr:`~app.db.models.filing.Filing.amends_id`.

Either one can be loaded first. A crawl of an earlier week after a later one
loads the amendment before its original. So whichever arrives second makes the
link, and then every filing in the chain of amendments loses its lines except
the latest filed. Loading the original again, even with ``replace``, leaves it
with none.

Two loads of one issuer are serialised by an advisory lock, because two
transactions loading an original and its amendment at once would each miss
the other, and neither would make the link.

The security
------------
A Form 4 names its issuer by CIK. ``security`` has CUSIPs and tickers, so the
link is CIK to tickers (SEC's ``company_tickers.json``, stored by
``crawl-form4``) to the security each ticker named on the day of the earliest
trade (:func:`~app.db.queries.tickers.lookup_query`). An issuer with several
listed classes is narrowed by the symbol the filing gives. When that still
leaves more than one, or none, the lines load with ``security_id`` null: the
link is non-blocking, and the lines are still found by issuer CIK.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Date,
    and_,
    cast,
    delete,
    exists,
    func,
    literal,
    null,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.tickers import normalise_ticker
from app.db.models.filing import Filing, ParseStatus
from app.db.models.insider import Insider, ReportingOwner
from app.db.models.insider_transaction import InsiderTransaction
from app.db.models.sec_company_ticker import SecCompanyTicker
from app.db.queries.tickers import lookup_query
from app.ingestion.parsers.form4 import FORM_TYPES, Form4Document, Form4Transaction, Issuer
from app.ingestion.parsers.form4 import ReportingOwner as ParsedOwner

logger = get_logger(__name__)

#: Where EDGAR's filing dates are reckoned. A Form 4 accepted by 10pm is filed
#: that day, so its filing date is its acceptance date here.
_EDGAR_TIMEZONE: Final = "America/New_York"

_FORM_TYPES: Final = tuple(sorted(FORM_TYPES))

#: The constraint the insert conflicts on: the natural key.
_NATURAL_KEY: Final = "uq_insider_transaction_filing_id_line_number_transaction_date"


class SecurityLink(StrEnum):
    """How :attr:`Form4LoadResult.security_id` was decided."""

    LINKED = "linked"
    UNLISTED = "unlisted"
    """SEC's ticker map has no ticker for the issuer's CIK: a private company,
    a fund, or one delisted before the map was first stored."""
    NO_SECURITY = "no security"
    """The issuer has tickers, and no security answers to any of them: no
    tracked investor has ever held it, or its CUSIP has no ticker yet."""
    AMBIGUOUS = "ambiguous"
    """The issuer's tickers name several securities, and the filing's symbol
    does not say which."""


class AmendmentLink(StrEnum):
    """What became of a 4/A's search for the filing it amends."""

    LINKED = "linked"
    NOT_LOADED = "original not loaded"
    """No Form 4 of the issuer's, filed that day, shares an owner with it. The
    original is outside what has been crawled, or not loaded yet; loading it
    later makes the link."""
    AMBIGUOUS = "ambiguous"
    """Several do, with the same owners. Left unlinked, and all of them keep
    their lines, rather than guess which one the amendment corrects."""
    NO_DATE = "no date"
    """The 4/A does not say when the original was filed."""


@dataclass(frozen=True, slots=True)
class Form4LoadResult:
    """What one call to :func:`load_form4` did."""

    filing_id: int

    insider_id: int
    """The insider every line is attributed to: the first owner named."""

    owners: int
    """Reporting owners on the filing, each with a ``reporting_owner`` row."""

    lines: int
    """Transaction lines in the document."""

    lines_written: int
    """Lines this call inserted. Zero when an earlier call had already loaded
    them, and zero when the filing is superseded."""

    security_id: int | None
    security_link: SecurityLink

    amendment: AmendmentLink | None
    """For a 4/A, whether the original was found. ``None`` for a Form 4."""

    amends_id: int | None
    """The filing a 4/A amends, when it is linked."""

    superseded_by: int | None
    """The later 4/A whose lines replace this filing's, if there is one."""

    lines_superseded: int
    """Lines deleted from the earlier filings this one supersedes."""


async def load_form4(
    session: AsyncSession,
    *,
    accession_no: str,
    filed_at: datetime,
    document: Form4Document,
    raw_key: str | None = None,
    source_url: str | None = None,
    replace: bool = False,
) -> Form4LoadResult:
    """Write a parsed Form 4 or 4/A: the filing, its owners and its lines.

    Idempotent on ``accession_no`` and each line's number. Called three times
    with the same inputs, the database holds one filing, its owners once, and
    its lines once, with the ids the first call gave them.

    :param accession_no: Dashed. From the index, as is ``filed_at``: the
        document names neither.
    :param filed_at: When EDGAR accepted it. What orders amendments, and what
        a 4/A's ``dateOfOriginalSubmission`` is matched against.
    :param raw_key: The archive prefix. ``None`` never erases one.
    :param source_url: Where the document was fetched from. Same rule.
    :param replace: Delete the filing's owners and lines first, so that a
        re-parse that reads a line differently is written. Without it, lines
        already loaded are left as they are (``ON CONFLICT DO NOTHING``), which
        is what a redelivered or resumed load wants. ``ingest-form4 --force``
        passes it.

    In one savepoint. The commit is the caller's, so that marking the queue
    row done is in the same transaction.
    """
    async with session.begin_nested():
        # Held to the end of the caller's transaction; see the module docstring.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"form4:{document.issuer.cik}"},
        )
        filing_id = await _upsert_filing(
            session,
            accession_no=accession_no,
            filed_at=filed_at,
            document=document,
            raw_key=raw_key,
            source_url=source_url,
        )
        if replace:
            # Cascades to the lines, through the composite foreign key.
            await session.execute(
                delete(ReportingOwner).where(ReportingOwner.filing_id == filing_id)
            )

        owners = _distinct_owners(document.owners)
        insider_ids = await _upsert_insiders(
            session, owners, filing_id=filing_id, filed_at=filed_at, accession_no=accession_no
        )
        await session.execute(
            pg_insert(ReportingOwner)
            .values([_owner_row(owner, filing_id, insider_ids[owner.cik]) for owner in owners])
            .on_conflict_do_nothing(index_elements=["filing_id", "insider_id"])
        )

        security_id, security_link = await link_security(
            session, document.issuer, on=document.period_of_report
        )
        attributed = insider_ids[owners[0].cik]
        lines_written = 0
        if document.transactions:
            rows = [
                _transaction_row(
                    line,
                    filing_id=filing_id,
                    insider_id=attributed,
                    issuer=document.issuer,
                    security_id=security_id,
                )
                for line in document.transactions
            ]
            inserted = await session.scalars(
                pg_insert(InsiderTransaction)
                .values(rows)
                .on_conflict_do_nothing(constraint=_NATURAL_KEY)
                .returning(InsiderTransaction.id)
            )
            lines_written = len(inserted.all())

        amendment = (
            await _link_amendment(session, filing_id) if document.document_type == "4/A" else None
        )
        for later in await _unlinked_amendments_of(session, filing_id):
            await _link_amendment(session, later)
        keeper, lines_superseded = await _supersede(session, filing_id)

        amends_id = await session.scalar(select(Filing.amends_id).where(Filing.id == filing_id))

    superseded_by = keeper if keeper is not None and keeper != filing_id else None
    if superseded_by is not None:
        # Written and deleted again: its own, not lines it superseded.
        lines_written = lines_superseded = 0
    if amendment is not None and amendment is not AmendmentLink.LINKED:
        logger.info(
            "form4.amendment_unlinked",
            accession_no=accession_no,
            reason=amendment.value,
            date_of_original_submission=_iso(document.date_of_original_submission),
        )
    return Form4LoadResult(
        filing_id=filing_id,
        insider_id=attributed,
        owners=len(owners),
        lines=len(document.transactions),
        lines_written=lines_written,
        security_id=security_id,
        security_link=security_link,
        amendment=amendment,
        amends_id=amends_id,
        superseded_by=superseded_by,
        lines_superseded=lines_superseded,
    )


# --- the filing and its owners -------------------------------------------------


async def _upsert_filing(
    session: AsyncSession,
    *,
    accession_no: str,
    filed_at: datetime,
    document: Form4Document,
    raw_key: str | None,
    source_url: str | None,
) -> int:
    """Insert the filing, or bring the row up to date with this parse.

    ``cik`` is the issuer's: the company the form is about, which is what a
    4/A is matched to its original by. Never a filer, so ``filer_id`` stays
    null, and no period: a Form 4 reports days. ``value_multiplier`` is 1
    because the column requires one, and Form 4 prices are in dollars.

    ``amends_id`` is left out of the update for the 13F loader's reason: the
    linking step owns it.
    """
    values: dict[str, Any] = {
        "accession_no": accession_no,
        "cik": document.issuer.cik,
        "form_type": document.document_type,
        "period_of_report": None,
        "filed_at": filed_at,
        "value_multiplier": 1,
        "date_of_original_submission": (
            document.date_of_original_submission if document.document_type == "4/A" else None
        ),
        "parsed_at": func.now(),
        "parse_error": None,
        "parse_status": ParseStatus.OK.value,
        "parse_notes": None,
        "raw_key": raw_key,
        "source_url": source_url,
        "ingested_at": func.now(),
    }
    statement = pg_insert(Filing).values(**values)
    upsert = statement.on_conflict_do_update(
        index_elements=[Filing.accession_no],
        set_={
            "cik": statement.excluded.cik,
            "form_type": statement.excluded.form_type,
            "filed_at": statement.excluded.filed_at,
            "date_of_original_submission": statement.excluded.date_of_original_submission,
            "parsed_at": statement.excluded.parsed_at,
            "parse_error": statement.excluded.parse_error,
            "parse_status": statement.excluded.parse_status,
            "parse_notes": statement.excluded.parse_notes,
            "raw_key": func.coalesce(statement.excluded.raw_key, Filing.raw_key),
            "source_url": func.coalesce(statement.excluded.source_url, Filing.source_url),
            "ingested_at": statement.excluded.ingested_at,
        },
    ).returning(Filing.id)
    return int((await session.execute(upsert)).scalar_one())


def _distinct_owners(owners: tuple[ParsedOwner, ...]) -> list[ParsedOwner]:
    """The owners in document order, each CIK once: the first time it is named.

    A CIK named twice would be two ``reporting_owner`` rows with one key.
    """
    seen: dict[str, ParsedOwner] = {}
    for owner in owners:
        seen.setdefault(owner.cik, owner)
    return list(seen.values())


async def _upsert_insiders(
    session: AsyncSession,
    owners: list[ParsedOwner],
    *,
    filing_id: int,
    filed_at: datetime,
    accession_no: str,
) -> dict[str, int]:
    """``insider`` rows for every owner, upserted on CIK. Returns ``cik -> id``.

    The name is updated only when no other filing of the insider's was filed
    later, so it is the latest filing's spelling whatever order filings load
    in. Sorted by CIK, so that two loads upserting the same insiders lock
    their rows in the same order and cannot deadlock.
    """
    ordered = sorted(owners, key=lambda owner: owner.cik)
    statement = pg_insert(Insider).values(
        [{"cik": owner.cik, "name": owner.name} for owner in ordered]
    )
    filed_later = exists(
        select(1)
        .select_from(ReportingOwner)
        .join(Filing, Filing.id == ReportingOwner.filing_id)
        .where(
            ReportingOwner.insider_id == Insider.id,
            Filing.id != filing_id,
            tuple_(Filing.filed_at, Filing.accession_no)
            > tuple_(
                literal(filed_at, Filing.filed_at.type),
                literal(accession_no, Filing.accession_no.type),
            ),
        )
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Insider.cik],
            set_={"name": statement.excluded.name},
            where=and_(Insider.name != statement.excluded.name, ~filed_later),
        )
    )
    rows = await session.execute(
        select(Insider.cik, Insider.id).where(Insider.cik.in_([owner.cik for owner in ordered]))
    )
    return {cik: insider_id for cik, insider_id in rows}


def _owner_row(owner: ParsedOwner, filing_id: int, insider_id: int) -> dict[str, Any]:
    return {
        "filing_id": filing_id,
        "insider_id": insider_id,
        "is_director": owner.is_director,
        "is_officer": owner.is_officer,
        "is_ten_percent_owner": owner.is_ten_percent_owner,
        "is_other": owner.is_other,
        "officer_title": owner.officer_title,
        "other_text": owner.other_text,
    }


def _transaction_row(
    line: Form4Transaction,
    *,
    filing_id: int,
    insider_id: int,
    issuer: Issuer,
    security_id: int | None,
) -> dict[str, Any]:
    """One line as an ``insider_transaction`` row.

    Derivative lines keep their own shares and price: see
    :attr:`~app.db.models.insider_transaction.InsiderTransaction.is_derivative`.
    The strike, the underlying, the price range and the ownership nature have
    no column, and the footnotes they come from are kept.
    """
    return {
        "filing_id": filing_id,
        "insider_id": insider_id,
        "line_number": line.line_number,
        "transaction_date": line.transaction_date,
        "issuer_cik": issuer.cik,
        "issuer_name": issuer.name,
        "issuer_ticker": issuer.trading_symbol,
        "security_id": security_id,
        "security_title": line.security_title,
        "is_derivative": line.is_derivative,
        "transaction_code": line.transaction_code,
        "acquired_disposed": line.acquired_disposed,
        "shares": line.shares,
        "price_per_share": line.price_per_share,
        "shares_owned_after": line.shares_owned_after,
        "is_10b5_1": line.is_10b5_1,
        # SQL NULL, not JSON null, which the column's check refuses.
        "footnotes": line.footnotes or null(),
    }


# --- the security ----------------------------------------------------------------


async def link_security(
    session: AsyncSession, issuer: Issuer, *, on: date
) -> tuple[int | None, SecurityLink]:
    """The security the issuer's lines are in, and how that was decided.

    The issuer's tickers from SEC's map; the one matching the filing's own
    symbol, when one does; then the security each names on ``on``. Exactly
    one security links. See :class:`SecurityLink` for the rest.
    """
    tickers = list(
        await session.scalars(
            select(SecCompanyTicker.ticker)
            .where(SecCompanyTicker.cik == issuer.cik)
            .order_by(SecCompanyTicker.ticker)
        )
    )
    if not tickers:
        return None, SecurityLink.UNLISTED
    if issuer.trading_symbol is not None:
        symbol = normalise_ticker(issuer.trading_symbol)
        tickers = [ticker for ticker in tickers if normalise_ticker(ticker) == symbol] or tickers

    found: set[int] = set()
    for ticker in tickers:
        row = (
            await session.execute(lookup_query(normalise_ticker(ticker), cusip=False, on=on))
        ).first()
        if row is not None:
            found.add(row.id)
    if not found:
        return None, SecurityLink.NO_SECURITY
    if len(found) > 1:
        return None, SecurityLink.AMBIGUOUS
    [security_id] = found
    return security_id, SecurityLink.LINKED


# --- amendments ------------------------------------------------------------------


def _filed_on() -> Any:
    """``filing.filed_at`` as EDGAR's filing date."""
    return cast(func.timezone(_EDGAR_TIMEZONE, Filing.filed_at), Date)


async def _link_amendment(session: AsyncSession, amendment_id: int) -> AmendmentLink:
    """Set a 4/A's ``amends_id``, if it has none, to the filing it amends.

    Candidates: the issuer's Forms 4 and 4/A filed on the amendment's
    ``date_of_original_submission``, before it, sharing a reporting owner with
    it. A 4/A among them is how an amendment of an amendment is linked. When
    there are several, the one with exactly the amendment's owners; when that
    is still not one, nothing.
    """
    amendment = (
        await session.execute(
            select(
                Filing.cik,
                Filing.filed_at,
                Filing.amends_id,
                Filing.date_of_original_submission,
            ).where(Filing.id == amendment_id)
        )
    ).one()
    if amendment.amends_id is not None:
        return AmendmentLink.LINKED
    if amendment.date_of_original_submission is None:
        return AmendmentLink.NO_DATE

    owners = await _owner_sets(session, [amendment_id])
    mine = owners[amendment_id]
    candidates = list(
        await session.scalars(
            select(Filing.id)
            .where(
                Filing.id != amendment_id,
                Filing.cik == amendment.cik,
                Filing.form_type.in_(_FORM_TYPES),
                _filed_on() == amendment.date_of_original_submission,
                Filing.filed_at < amendment.filed_at,
                exists(
                    select(1).where(
                        ReportingOwner.filing_id == Filing.id,
                        ReportingOwner.insider_id.in_(mine),
                    )
                ),
            )
            .order_by(Filing.id)
        )
    )
    if len(candidates) > 1:
        theirs = await _owner_sets(session, candidates)
        candidates = [candidate for candidate in candidates if theirs[candidate] == mine]
    if not candidates:
        return AmendmentLink.NOT_LOADED
    if len(candidates) > 1:
        return AmendmentLink.AMBIGUOUS
    await session.execute(
        update(Filing).where(Filing.id == amendment_id).values(amends_id=candidates[0])
    )
    return AmendmentLink.LINKED


async def _owner_sets(session: AsyncSession, filing_ids: list[int]) -> dict[int, frozenset[int]]:
    rows = await session.execute(
        select(ReportingOwner.filing_id, ReportingOwner.insider_id).where(
            ReportingOwner.filing_id.in_(filing_ids)
        )
    )
    grouped: dict[int, set[int]] = {filing_id: set() for filing_id in filing_ids}
    for filing_id, insider_id in rows:
        grouped[filing_id].add(insider_id)
    return {filing_id: frozenset(ids) for filing_id, ids in grouped.items()}


async def _unlinked_amendments_of(session: AsyncSession, filing_id: int) -> list[int]:
    """4/As loaded before this filing that may amend it: unlinked, same issuer,
    filed later, naming this filing's day as their original's."""
    this = (
        await session.execute(
            select(Filing.cik, Filing.filed_at, _filed_on().label("filed_on")).where(
                Filing.id == filing_id
            )
        )
    ).one()
    return list(
        await session.scalars(
            select(Filing.id)
            .where(
                Filing.form_type == "4/A",
                Filing.amends_id.is_(None),
                Filing.cik == this.cik,
                Filing.date_of_original_submission == this.filed_on,
                Filing.filed_at > this.filed_at,
            )
            .order_by(Filing.filed_at, Filing.accession_no)
        )
    )


async def _supersede(session: AsyncSession, filing_id: int) -> tuple[int | None, int]:
    """Keep the lines of the latest-filed filing in this filing's amendment
    chain, and delete everyone else's.

    :returns: The chain's latest filing (``None`` when the filing is alone in
        its chain) and how many lines were deleted.
    """
    root, seen = filing_id, {filing_id}
    while True:
        parent = await session.scalar(select(Filing.amends_id).where(Filing.id == root))
        if parent is None or parent in seen:
            break
        root = parent
        seen.add(parent)

    members, frontier = {root}, [root]
    while frontier:
        children = list(
            await session.scalars(
                select(Filing.id).where(
                    Filing.amends_id.in_(frontier), Filing.form_type.in_(_FORM_TYPES)
                )
            )
        )
        frontier = [child for child in children if child not in members]
        members.update(frontier)
    if len(members) == 1:
        return None, 0

    keeper = await session.scalar(
        select(Filing.id)
        .where(Filing.id.in_(members))
        .order_by(Filing.filed_at.desc(), Filing.accession_no.desc())
        .limit(1)
    )
    deleted = await session.scalars(
        delete(InsiderTransaction)
        .where(InsiderTransaction.filing_id.in_(members - {keeper}))
        .returning(InsiderTransaction.id)
    )
    return keeper, len(deleted.all())


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()
