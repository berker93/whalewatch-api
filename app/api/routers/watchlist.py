"""The signed-in user's watchlist: stocks, investors and insiders they follow.

``GET /v1/watchlist``
    All three lists, each in the order its items were added, oldest first, and
    each present, ``[]`` when empty. On the path of every app entry (the ``/``
    redirect waits for it), so it is three indexed reads and nothing the client
    waits on per item.
``PUT /v1/watchlist/items`` and ``DELETE /v1/watchlist/items``
    Follow and unfollow one thing, by a :class:`~app.api.schemas.watchlist.WatchRef`.
    Both idempotent, and both answer with the whole watchlist. A ref that names
    nothing (no such CUSIP, slug or CIK) is a 404, either way.

An insider is shown as their latest Form 4 gives them: name, roles and title
from their latest filing, its issuer, and the ticker of the security its lines
are linked to.
"""

from typing import Any, Final

from fastapi import APIRouter, HTTPException, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import Select, delete, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.plans import entitlements_for
from app.api.auth import UNAUTHORIZED, UNVERIFIED, NoStoreDep, VerifiedViewerDep, ViewerDep
from app.api.deps import SessionDep
from app.api.errors import INVALID, invalid, not_found
from app.api.schemas.insider import Role
from app.api.schemas.search import InvestorMatch, StockMatch
from app.api.schemas.watchlist import (
    InsiderMatch,
    WatchInsider,
    WatchInvestor,
    Watchlist,
    WatchlistItemRequest,
    WatchStock,
)
from app.db.models import (
    Filer,
    Filing,
    Insider,
    InsiderTransaction,
    ReportingOwner,
    Security,
    WatchlistItem,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"], dependencies=[NoStoreDep])

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)

_NOT_FOUND = not_found(
    "No stock with that CUSIP, investor with that slug, or insider with that CIK."
)


# --- refs -----------------------------------------------------------------------


def normalise_cusip(cusip: str) -> str | None:
    """Nine letters and digits, uppercased; None for anything else."""
    cusip = cusip.strip().upper()
    return cusip if len(cusip) == 9 and cusip.isalnum() else None


def normalise_cik(cik: str) -> str | None:
    """Ten digits, zero-padded; None for anything else."""
    cik = cik.strip()
    return cik.zfill(10) if cik.isdigit() and len(cik) <= 10 else None


async def resolve_ref(
    session: AsyncSession, ref: WatchStock | WatchInvestor | WatchInsider
) -> dict[str, int]:
    """The ``watchlist_item`` column ``ref`` sets, and the id it names.

    :raises RequestValidationError: A CUSIP or CIK that cannot be one.
    :raises HTTPException: 404, a well-formed ref that names nothing.
    """
    found: int | None
    if isinstance(ref, WatchStock):
        cusip = normalise_cusip(ref.cusip)
        if cusip is None:
            raise _refused("cusip", "a CUSIP is nine letters and digits")
        found = await session.scalar(select(Security.id).where(Security.cusip == cusip))
        column, missing = "security_id", f"No stock with CUSIP {cusip}."
    elif isinstance(ref, WatchInvestor):
        found = await session.scalar(select(Filer.id).where(Filer.slug == ref.slug))
        column, missing = "filer_id", f"No investor '{ref.slug}'. GET /v1/investors lists them."
    else:
        cik = normalise_cik(ref.insider_cik)
        if cik is None:
            raise _refused("insider_cik", "a CIK is up to ten digits")
        found = await session.scalar(select(Insider.id).where(Insider.cik == cik))
        column, missing = "insider_id", f"No insider with CIK {cik} has filed a Form 4 we hold."
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    return {column: found}


def _refused(field: str, message: str) -> RequestValidationError:
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", "ref", field), "msg": message}]
    )


# --- the lists ------------------------------------------------------------------


def securities_query(user_id: int) -> Select[Any]:
    return (
        select(Security.cusip, Security.ticker, Security.name, Security.delisted_by)
        .join(WatchlistItem, WatchlistItem.security_id == Security.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )


def investors_query(user_id: int) -> Select[Any]:
    return (
        select(Filer.slug, _DISPLAY_NAME.label("display_name"), Filer.manager_name, Filer.category)
        .join(WatchlistItem, WatchlistItem.filer_id == Filer.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )


def insiders_query(user_id: int) -> Select[Any]:
    """Each followed insider with their latest filing's roles, issuer and linked
    security: a LATERAL probe of ``ix_reporting_owner_insider_id`` per insider,
    then one of the filing's lines."""
    latest = (
        select(
            ReportingOwner.filing_id,
            ReportingOwner.is_director,
            ReportingOwner.is_officer,
            ReportingOwner.is_ten_percent_owner,
            ReportingOwner.is_other,
            ReportingOwner.officer_title,
        )
        .join(Filing, Filing.id == ReportingOwner.filing_id)
        .where(ReportingOwner.insider_id == Insider.id)
        .order_by(Filing.filed_at.desc(), Filing.id.desc())
        .limit(1)
        .lateral("latest")
    )
    line = (
        select(InsiderTransaction.issuer_name, InsiderTransaction.security_id)
        .where(InsiderTransaction.filing_id == latest.c.filing_id)
        # A linked line first, so a filing whose first line is unlinked still
        # gives the ticker of the one that is.
        .order_by(InsiderTransaction.security_id.is_(None), InsiderTransaction.line_number)
        .limit(1)
        .lateral("line")
    )
    return (
        select(
            Insider.cik,
            Insider.name,
            latest.c.is_director,
            latest.c.is_officer,
            latest.c.is_ten_percent_owner,
            latest.c.is_other,
            latest.c.officer_title,
            line.c.issuer_name,
            Security.ticker,
        )
        .select_from(WatchlistItem)
        .join(Insider, Insider.id == WatchlistItem.insider_id)
        .outerjoin(latest, true())
        .outerjoin(line, true())
        .outerjoin(Security, Security.id == line.c.security_id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )


def _roles(row: Any) -> list[Role]:
    return [
        role
        for role, checked in (
            (Role.DIRECTOR, row.is_director),
            (Role.OFFICER, row.is_officer),
            (Role.TEN_PERCENT_OWNER, row.is_ten_percent_owner),
            (Role.OTHER, row.is_other),
        )
        if checked
    ]


async def build_watchlist(session: AsyncSession, user_id: int) -> Watchlist:
    securities = await session.execute(securities_query(user_id))
    investors = await session.execute(investors_query(user_id))
    insiders = await session.execute(insiders_query(user_id))
    return Watchlist(
        securities=[
            StockMatch(
                cusip=row.cusip,
                ticker=row.ticker,
                current_ticker=row.ticker,
                issuer_name=row.name,
                delisted_by=row.delisted_by,
            )
            for row in securities
        ],
        investors=[
            InvestorMatch(
                slug=row.slug,
                display_name=row.display_name,
                manager_name=row.manager_name,
                category=row.category,
            )
            for row in investors
        ],
        insiders=[
            InsiderMatch(
                insider_cik=row.cik,
                insider_name=row.name,
                roles=_roles(row),
                officer_title=row.officer_title,
                issuer_name=row.issuer_name or "",
                current_ticker=row.ticker,
            )
            for row in insiders
        ],
    )


# --- the routes -----------------------------------------------------------------


@router.get(
    "",
    operation_id="getWatchlist",
    response_model=Watchlist,
    summary="What the signed-in user follows",
    responses=UNAUTHORIZED,
)
async def read_watchlist(viewer: ViewerDep, session: SessionDep) -> Watchlist:
    """Stocks, investors and insiders, each list in the order its items were
    added, oldest first. Nothing followed is three empty lists, never a 404."""
    return await build_watchlist(session, viewer.user.id)


@router.put(
    "/items",
    operation_id="addWatchlistItem",
    response_model=Watchlist,
    summary="Follow a stock, an investor or an insider",
    responses={**UNAUTHORIZED, **UNVERIFIED, **_NOT_FOUND, **INVALID},
)
async def add_watchlist_item(
    body: WatchlistItemRequest, viewer: VerifiedViewerDep, session: SessionDep
) -> Watchlist:
    """Adds `ref` at the end of its list. Already followed is no change. A ref
    that names nothing is a 404; a full watchlist (the plan's
    `watchlist_max_items`, 500 on Free) a 422; an account whose email is not
    verified a 403 `email_unverified`."""
    target = await resolve_ref(session, body.ref.root)
    user_id = viewer.user.id
    most = entitlements_for(viewer.user).watchlist_max_items
    count = await session.scalar(
        select(func.count()).select_from(WatchlistItem).where(WatchlistItem.user_id == user_id)
    )
    if (count or 0) >= most:
        raise invalid("ref", f"the watchlist is full: at most {most} items", where="body")
    # Any of the three partial unique indexes: already followed is no change.
    await session.execute(
        insert(WatchlistItem).values(user_id=user_id, **target).on_conflict_do_nothing()
    )
    await session.commit()
    return await build_watchlist(session, user_id)


@router.delete(
    "/items",
    operation_id="removeWatchlistItem",
    response_model=Watchlist,
    summary="Unfollow a stock, an investor or an insider",
    responses={**UNAUTHORIZED, **UNVERIFIED, **_NOT_FOUND, **INVALID},
)
async def remove_watchlist_item(
    body: WatchlistItemRequest, viewer: VerifiedViewerDep, session: SessionDep
) -> Watchlist:
    """Removes `ref`. Not followed is no change. A ref that names nothing is a
    404; an account whose email is not verified a 403 `email_unverified`."""
    target = await resolve_ref(session, body.ref.root)
    [(column, target_id)] = target.items()
    user_id = viewer.user.id
    await session.execute(
        delete(WatchlistItem).where(
            WatchlistItem.user_id == user_id, getattr(WatchlistItem, column) == target_id
        )
    )
    await session.commit()
    return await build_watchlist(session, user_id)
