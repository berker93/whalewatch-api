"""``/v1/me``: the signed-in user, and what WhaleWatch holds on them.

``GET /v1/me``
    Who is signed in, their plan and their entitlements, in one call: what an
    app page needs to decide what to show before it shows anything. One
    indexed read of the user; the session is already in Redis, and the plan
    and entitlements are worked out from the user (:mod:`app.accounts.plans`).
``GET`` and ``PATCH /v1/me/privacy``
    What the account's data is used for, on which lawful basis, and the two
    choices the user has: product email (objectable) and marketing email
    (opt-in). Every change is a row in the ledger (:mod:`app.accounts.privacy`).
``GET /v1/me/export``
    Everything held on the user, as one JSON document to download: the
    right of access and of portability. Works unverified: it saves nothing.

Deleting the account is ``DELETE /v1/account`` (app/api/routers/account.py).
"""

from typing import Final

from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import privacy
from app.accounts.plans import entitlements_for, plan_for
from app.accounts.sessions import now
from app.api.auth import UNAUTHORIZED, NoStoreDep, SessionStoreDep, ViewerDep
from app.api.deps import SessionDep
from app.api.errors import INVALID
from app.api.schemas.me import Me, MeEntitlements, MePlan, MeUser
from app.api.schemas.privacy import (
    Export,
    ExportAccount,
    ExportConsent,
    ExportInsider,
    ExportInvestor,
    ExportLink,
    ExportSession,
    ExportStock,
    ExportWatchlist,
    Privacy,
    PrivacyPatch,
    PurposeStanding,
    TermsStanding,
)
from app.db.models import (
    BASES,
    AccountToken,
    AppUser,
    ConsentRecord,
    Filer,
    Insider,
    Purpose,
    Security,
    WatchlistItem,
)

router = APIRouter(prefix="/me", tags=["account"], dependencies=[NoStoreDep])


@router.get(
    "",
    operation_id="getMe",
    response_model=Me,
    summary="The signed-in user, their plan and what it lets them do",
    responses=UNAUTHORIZED,
)
async def read_me(viewer: ViewerDep) -> Me:
    """A 401 `unauthenticated` signed out, `session_expired` when the session
    has ended. `entitlements.save` is false until the email is verified: the
    account reads everything and saves nothing."""
    user = viewer.user
    plan = plan_for(user)
    entitlements = entitlements_for(user)
    return Me(
        user=MeUser(
            email=user.email,
            name=user.name,
            time_zone=user.time_zone,
            email_verified=user.email_verified_at is not None,
            email_verified_at=user.email_verified_at,
            created_at=user.created_at,
            last_seen_at=user.last_seen_at,
        ),
        plan=MePlan(id=plan.id, name=plan.name),
        entitlements=MeEntitlements(
            save=entitlements.save, watchlist_max_items=entitlements.watchlist_max_items
        ),
    )


# --- privacy --------------------------------------------------------------------


def _privacy(standing: dict[Purpose, ConsentRecord]) -> Privacy:
    account = standing.get(Purpose.ACCOUNT)
    return Privacy(
        terms=TermsStanding(
            accepted_version=account.terms_version if account else None,
            accepted_at=account.recorded_at if account and account.terms_version else None,
            current_version=privacy.TERMS_VERSION,
            privacy_version=privacy.PRIVACY_VERSION,
        ),
        purposes=[
            PurposeStanding(
                purpose=purpose,
                basis=BASES[purpose],
                # The account's own purpose holds while the account does.
                granted=purpose is Purpose.ACCOUNT or (record is not None and record.granted),
                changeable=purpose in privacy.CHOOSABLE,
                since=record.recorded_at if record else None,
            )
            for purpose in Purpose
            for record in [standing.get(purpose)]
        ],
    )


@router.get(
    "/privacy",
    operation_id="getPrivacy",
    response_model=Privacy,
    summary="What the account's data is used for, on what basis, and the user's choices",
    responses=UNAUTHORIZED,
)
async def read_privacy(viewer: ViewerDep, session: SessionDep) -> Privacy:
    """Each purpose with its lawful basis and whether it applies, and the
    terms accepted against those in force. A `terms.accepted_version` behind
    `terms.current_version` is an account to ask to accept the new terms."""
    return _privacy(await privacy.standing(session, viewer.user.id))


@router.patch(
    "/privacy",
    operation_id="updatePrivacy",
    response_model=Privacy,
    summary="Consent to or withdraw marketing email; object to product email or stop objecting",
    responses={**UNAUTHORIZED, **INVALID},
)
async def update_privacy(body: PrivacyPatch, viewer: ViewerDep, session: SessionDep) -> Privacy:
    """Fields left out or null stay as they are. Takes effect at once and is
    recorded with its time, as withdrawing must be as easy as consenting.
    Works unverified: withdrawing is not saving. The same answer twice is one
    record."""
    choices = {
        purpose: granted
        for purpose, granted in (
            (Purpose.PRODUCT_EMAIL, body.product_email),
            (Purpose.MARKETING_EMAIL, body.marketing_email),
        )
        if granted is not None
    }
    standing = await privacy.choose(session, viewer.user.id, choices)
    await session.commit()
    return _privacy(standing)


# --- export ---------------------------------------------------------------------

#: What is held on the user but not in the export, and why.
NOT_INCLUDED: Final = [
    "password: only a one-way hash of it is kept, which is no use to anyone but an attacker",
    "session cookies and emailed link tokens: only their hashes are kept, for the same reason",
    "server logs: request lines (time, path, IP address) and email delivery failures, kept "
    "for operations; ask for them by email",
    "sign-in attempt counters: a hash of the email and the IP address, gone after 15 minutes",
]


async def _watchlist(session: AsyncSession, user_id: int) -> ExportWatchlist:
    securities = await session.execute(
        select(Security.cusip, Security.ticker, Security.name, WatchlistItem.added_at)
        .join(WatchlistItem, WatchlistItem.security_id == Security.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )
    investors = await session.execute(
        select(
            Filer.slug,
            func.coalesce(Filer.display_name, Filer.name).label("name"),
            WatchlistItem.added_at,
        )
        .join(WatchlistItem, WatchlistItem.filer_id == Filer.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )
    insiders = await session.execute(
        select(Insider.cik, Insider.name, WatchlistItem.added_at)
        .join(WatchlistItem, WatchlistItem.insider_id == Insider.id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.id)
    )
    return ExportWatchlist(
        securities=[ExportStock.model_validate(row, from_attributes=True) for row in securities],
        investors=[ExportInvestor.model_validate(row, from_attributes=True) for row in investors],
        insiders=[ExportInsider.model_validate(row, from_attributes=True) for row in insiders],
    )


def _account(user: AppUser) -> ExportAccount:
    return ExportAccount(
        id=user.id,
        email=user.email,
        name=user.name,
        time_zone=user.time_zone,
        created_at=user.created_at,
        password_changed_at=user.password_changed_at,
        email_verified_at=user.email_verified_at,
        last_seen_at=user.last_seen_at,
    )


@router.get(
    "/export",
    operation_id="exportMe",
    response_model=Export,
    summary="Everything WhaleWatch holds on the signed-in user, as a JSON download",
    responses=UNAUTHORIZED,
)
async def export_me(
    viewer: ViewerDep, session: SessionDep, store: SessionStoreDep, response: Response
) -> Export:
    """The account, the watchlist, the whole consent ledger, the links and
    sessions still live, and a list of what is held but left out (hashes,
    logs) and why. `Content-Disposition: attachment`, so a browser saves it.
    The format is versioned by `format`."""
    user = viewer.user
    links = await session.scalars(
        select(AccountToken)
        .where(AccountToken.user_id == user.id, AccountToken.expires_at > now())
        .order_by(AccountToken.id)
    )
    exported_at = now()
    response.headers["content-disposition"] = (
        f'attachment; filename="whalewatch-export-{exported_at:%Y-%m-%d}.json"'
    )
    return Export(
        format="whalewatch-export/1",
        exported_at=exported_at,
        account=_account(user),
        watchlist=await _watchlist(session, user.id),
        consents=[
            ExportConsent.model_validate(row, from_attributes=True)
            for row in await privacy.history(session, user.id)
        ],
        links=[ExportLink.model_validate(row, from_attributes=True) for row in links],
        sessions=[
            ExportSession(remember=record.remember, expires_at=record.expires_at)
            for record in await store.list_for(user.id)
        ],
        not_included=NOT_INCLUDED,
    )
