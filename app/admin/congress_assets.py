"""The asset queue: PTR lines no run resolved to a security, and a person's override.

A run resolves a line by its ticker, then its issuer's name
(:mod:`app.ingestion.congress.asset_resolution`). A stock's line neither finds
is ``equity`` with no security; a scan's line nothing places is ``unknown``.
Here an admin sees them grouped by asset key — the code, ticker and name as
filed — with how many lines and filings name the asset that way and the best
security a name search finds, and the resolved lines a run was not sure of
(``low_confidence``). They then set an override for the key:

- a **stock**, or an option on one: ``equity`` or ``stock_option``, with the
  security, by CUSIP or by ticker;
- any **other class**: a municipal bond, a fund, a private company. No security.

An override covers every line under the key, past and future, and no run
changes it (``congress_asset_override``). Removing it hands the lines back to
the runs. Either way the key's lines are resolved again at once, and the
change is logged in ``admin_action`` in the same transaction: from the console
under the admin, from ``congress-asset-override`` with none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import log
from app.admin.congress import ReviewError
from app.core.tickers import normalise_ticker
from app.db.models import AdminActionKind
from app.db.models.congress_asset_override import CongressAssetOverride
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_transaction import CongressTransaction
from app.db.models.security import Security
from app.db.queries.tickers import lookup_query
from app.ingestion.congress.asset_resolution import (
    LOW_CONFIDENCE,
    Candidate,
    Resolver,
    resolve_assets,
)
from app.ingestion.congress.assets import RESOLVABLE, WITH_SECURITY, AssetClass
from app.ingestion.congress.ptr import RowStatus

#: The lines the queue is about: trades, or maybe trades.
_LINES = (RowStatus.PARSED.value, RowStatus.NEEDS_REVIEW.value)


@dataclass(frozen=True, slots=True)
class QueuedAsset:
    """One asset key, with the lines under it."""

    asset_key: str
    asset_type: str | None
    ticker: str | None
    asset_name: str | None
    """As filed, on the newest line."""
    asset_class: str
    lines: int
    filings: int
    last_traded: date | None
    document_url: str | None
    """The newest filing's original, to read the asset off."""
    security: Security | None
    """The security its lines have, on the ``low_confidence`` queue."""
    confidence: Decimal | None
    """Theirs, the lowest of them."""
    candidate: Candidate | None
    """The best security a name search finds, accepted or not."""


async def asset_queue(
    session: AsyncSession,
    *,
    status: Literal["unresolved", "low_confidence"] = "unresolved",
    chamber: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QueuedAsset], int]:
    """Asset keys whose lines are unresolved, or resolved under
    :data:`~app.ingestion.congress.asset_resolution.LOW_CONFIDENCE`; most lines
    first. A person's overrides are never in it."""
    line, filing = CongressTransaction, CongressFiling
    where: list[ColumnElement[bool]] = [line.parse_status.in_(_LINES), line.asset_key.is_not(None)]
    if status == "unresolved":
        where += [
            line.security_id.is_(None),
            line.asset_class.in_([kind.value for kind in RESOLVABLE]),
        ]
    else:
        where += [line.resolution_confidence < LOW_CONFIDENCE]
    where.append(line.resolution_method.is_distinct_from("manual"))
    if chamber is not None:
        where.append(filing.chamber == chamber)

    newest = func.max(func.coalesce(line.transaction_date, filing.filing_date))
    grouped = (
        select(
            line.asset_key,
            func.count().label("lines"),
            func.count(func.distinct(line.filing_id)).label("filings"),
            newest.label("last_traded"),
            func.max(line.id).label("newest_id"),
            func.min(line.resolution_confidence).label("confidence"),
        )
        .join(filing, filing.id == line.filing_id)
        .where(*where)
        .group_by(line.asset_key)
    )
    total = await session.scalar(select(func.count()).select_from(grouped.subquery()))
    rows = (
        await session.execute(
            grouped.order_by(func.count().desc(), line.asset_key).limit(limit).offset(offset)
        )
    ).all()

    resolver = await Resolver.load(session)
    queued: list[QueuedAsset] = []
    for row in rows:
        newest_line, newest_filing = (
            (
                await session.execute(
                    select(line, filing)
                    .join(filing, filing.id == line.filing_id)
                    .where(line.id == row.newest_id)
                )
            )
            .one()
            ._tuple()
        )
        security = (
            None
            if newest_line.security_id is None
            else await session.get(Security, newest_line.security_id)
        )
        on = row.last_traded or newest_filing.filing_date
        candidate = (
            None
            if newest_line.asset_name is None
            else await resolver.best_candidate(newest_line.asset_name, on)
        )
        queued.append(
            QueuedAsset(
                asset_key=row.asset_key,
                asset_type=newest_line.asset_type,
                ticker=newest_line.ticker,
                asset_name=newest_line.asset_name,
                asset_class=newest_line.asset_class or AssetClass.UNKNOWN.value,
                lines=row.lines,
                filings=row.filings,
                last_traded=row.last_traded,
                document_url=newest_filing.document_url,
                security=security,
                confidence=row.confidence,
                candidate=candidate,
            )
        )
    return queued, total or 0


# --- overrides -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverrideSet:
    override: CongressAssetOverride
    security: Security | None
    lines: int
    """Lines resolved again under it."""


async def find_security(
    session: AsyncSession, *, cusip: str | None, ticker: str | None
) -> Security | None:
    """The security a CUSIP names, or a ticker does today; ``None`` for neither given.

    :raises ReviewError: ``validation_error`` for both; ``not_found`` for one
        that names none.
    """
    if cusip is not None and ticker is not None:
        raise ReviewError("validation_error", "Give the security by cusip or by ticker, not both.")
    if cusip is not None:
        found = await session.scalar(select(Security).where(Security.cusip == cusip.upper()))
        if found is None:
            raise ReviewError("not_found", f"No security with CUSIP {cusip}.")
        return found
    if ticker is not None:
        key = normalise_ticker(ticker)
        row = (await session.execute(lookup_query(key, cusip=False))).first()
        if row is None:
            raise ReviewError("not_found", f"No security trades as {key}.")
        return await session.get(Security, row.id)
    return None


async def asset_key_of(session: AsyncSession, line_id: int) -> str:
    """The asset key a line is under, for an override made from one line.

    :raises ReviewError: ``not_found`` for no such line, or one no run has resolved.
    """
    key = await session.scalar(
        select(CongressTransaction.asset_key).where(CongressTransaction.id == line_id)
    )
    if key is None:
        raise ReviewError("not_found", f"No resolved PTR line {line_id}.")
    return key


async def set_override(
    session: AsyncSession,
    asset_key: str,
    *,
    asset_class: AssetClass,
    cusip: str | None,
    ticker: str | None,
    note: str | None,
    admin_id: int | None,
    request_id: str | None = None,
) -> OverrideSet:
    """Say what every line under ``asset_key`` is, and resolve them so. Replaces
    any override the key had. Does not commit.

    :raises ReviewError: ``not_found`` for a key no line has, or a security
        that is not there; ``validation_error`` for a stock without a
        security, or another class with one, or ``unknown``.
    """
    if asset_class is AssetClass.UNKNOWN:
        raise ReviewError("validation_error", "unknown is a run's word, not an override.")
    security = await find_security(session, cusip=cusip, ticker=ticker)
    needs_security = asset_class in WITH_SECURITY
    if needs_security and security is None:
        raise ReviewError("validation_error", f"{asset_class.value} needs a cusip or a ticker.")
    if not needs_security and security is not None:
        raise ReviewError("validation_error", f"{asset_class.value} has no security.")
    lines = await session.scalar(
        select(func.count())
        .select_from(CongressTransaction)
        .where(CongressTransaction.asset_key == asset_key)
    )
    if not lines:
        raise ReviewError("not_found", f"No PTR line has the asset key {asset_key!r}.")

    values = {
        "asset_class": asset_class.value,
        "security_id": None if security is None else security.id,
        "note": note,
        "created_by": admin_id,
    }
    statement = insert(CongressAssetOverride).values(asset_key=asset_key, **values)
    override_id = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[CongressAssetOverride.asset_key],
            set_={**values, "updated_at": datetime.now(UTC)},
        ).returning(CongressAssetOverride.id)
    )
    passed = await resolve_assets(session, keys=[asset_key])
    await log.record(
        session,
        AdminActionKind.ASSET_OVERRIDDEN,
        admin_id=admin_id,
        request_id=request_id,
        detail={
            "asset_key": asset_key,
            "asset_class": asset_class.value,
            "cusip": None if security is None else security.cusip,
            "lines": passed.lines,
        },
    )
    override = await session.get(CongressAssetOverride, override_id, populate_existing=True)
    assert override is not None
    return OverrideSet(override=override, security=security, lines=passed.lines)


async def remove_override(
    session: AsyncSession, override_id: int, *, admin_id: int | None, request_id: str | None = None
) -> int:
    """Take an override away, and resolve its lines by a run's rules again.
    Returns how many. Does not commit.

    :raises ReviewError: ``not_found`` for no such override.
    """
    key = await session.scalar(
        delete(CongressAssetOverride)
        .where(CongressAssetOverride.id == override_id)
        .returning(CongressAssetOverride.asset_key)
    )
    if key is None:
        raise ReviewError("not_found", f"No asset override {override_id}.")
    passed = await resolve_assets(session, keys=[key])
    await log.record(
        session,
        AdminActionKind.ASSET_OVERRIDE_REMOVED,
        admin_id=admin_id,
        request_id=request_id,
        detail={"asset_key": key, "lines": passed.lines},
    )
    return passed.lines


async def overrides(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> tuple[list[tuple[CongressAssetOverride, Security | None]], int]:
    """Every override, newest change first, with its security."""
    total = await session.scalar(select(func.count()).select_from(CongressAssetOverride))
    rows = await session.execute(
        select(CongressAssetOverride, Security)
        .outerjoin(Security, Security.id == CongressAssetOverride.security_id)
        .order_by(CongressAssetOverride.updated_at.desc(), CongressAssetOverride.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(override, security) for override, security in rows.tuples()], total or 0
