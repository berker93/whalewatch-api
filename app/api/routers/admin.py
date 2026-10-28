"""The admin console: users, their plans, and the site's numbers.

Every route but signing in needs an admin session (:data:`app.api.admin_auth.AdminDep`),
which a user's session is not. Payments are not here: Paddle's dashboard has
them, and a subscription row links to it by its Paddle ids.

``POST /admin/auth/login``, ``.../logout``, ``GET /admin/auth/session``
    The console's own sign-in: an admin's email and password, for the admin
    cookie. Limited as a user's login is, under its own budget.
``GET /admin/users``
    The user list: email, plan, signup date, last seen; filtered by text,
    plan, status, signup and last-seen days, comped and grandfathered.
``GET /admin/users/{id}``
    One account: its row, every entitlement as it resolves now, its
    subscriptions and overrides, its usage (API calls by quota month, saved
    filters, watchlist, keys, data exports, days active) and the last 50
    admin actions about it.
``POST /admin/users/{id}/override`` and ``.../override/end``
    Give the account a plan, with a reason and an expiry; end it early.
``POST /admin/users/{id}/grandfather`` and ``.../grandfather/end``
    Pin its subscription to a snapshot of its plan's terms today; unpin it.
``GET /admin/plans``
    Every plan, snapshots included: what an override may give.
``GET /admin/actions``
    The admin log, newest first.
``GET /admin/metrics``
    Signups, actives and plan mix by day, week or month.

Every change is written to ``admin_action`` in the transaction that makes it
(:mod:`app.admin.log`), and the account's sessions then read their grant
again, so an override reaches the user on their next request.
"""

from datetime import date, timedelta
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Path, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.accounts import entitlements
from app.accounts.sessions import SessionStoreError
from app.admin import log, metrics, plans, users
from app.admin.auth import authenticate, find_admin
from app.api.admin_auth import (
    AdminDep,
    AdminStoreDep,
    admin_token,
    clear_admin_cookie,
    request_id,
    set_admin_cookie,
)
from app.api.auth import NoStoreDep, SessionStoreDep, limit_attempts
from app.api.deps import SessionDep, SettingsDep
from app.api.errors import INVALID, ApiError, Responses, not_found
from app.api.metering import get_meter, usage_of
from app.api.schemas.admin import (
    AdminActionPage,
    AdminActionRow,
    AdminApiKeys,
    AdminCount,
    AdminExports,
    AdminLoginRequest,
    AdminOverride,
    AdminPlan,
    AdminSession,
    AdminSessionState,
    AdminSubscription,
    AdminUsage,
    AdminUsageMonth,
    AdminUserDetail,
    AdminUserPage,
    AdminUserRow,
    Grandfathered,
    MetricPoint,
    MetricSeries,
    OverrideEnded,
    OverrideRequest,
    OverrideSet,
    ReasonRequest,
    Ungrandfathered,
)
from app.api.schemas.error import ErrorCode, Problem
from app.core.logging import get_logger
from app.db.models import AdminActionKind, AppUser, Plan, PlanEntitlement

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[NoStoreDep])

_UNAUTHORIZED: Final[Responses] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": Problem,
        "description": (
            "Not signed in to the console: `unauthenticated` without the admin cookie, "
            "`session_expired` when its session has ended or the admin was disabled."
        ),
    }
}
_NO_USER: Final[Responses] = not_found("`not_found`: no such account, or it is deleted.")
_CONFLICT: Final[Responses] = {
    status.HTTP_409_CONFLICT: {
        "model": Problem,
        "description": "`no_subscription` or `already_grandfathered`.",
    }
}

_STATUS_OF: Final = {
    "not_found": 404,
    "unknown_plan": 422,
    "validation_error": 422,
    "no_subscription": 409,
    "already_grandfathered": 409,
}

UserId = Annotated[int, Path(ge=1, description="The account's id, from the user list.")]


def _refused(exc: plans.PlanChangeError) -> ApiError:
    return ApiError(_STATUS_OF.get(exc.code, 409), ErrorCode(exc.code), exc.message)


# --- signing in ------------------------------------------------------------------------


@router.post(
    "/auth/login",
    operation_id="adminLogin",
    response_model=AdminSession,
    summary="Sign in to the admin console",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": Problem,
            "description": "`invalid_credentials`, or `forbidden_origin`.",
        },
        **INVALID,
    },
)
async def admin_login(
    body: AdminLoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    store: AdminStoreDep,
    settings: SettingsDep,
) -> AdminSession:
    """Sets the admin cookie, for `ADMIN_SESSION_HOURS`. A wrong email and a wrong
    password are the same 403 `invalid_credentials`, and take as long. At most 5
    attempts in 15 minutes from one address, and for one email. A user's
    account does not sign in here, whatever its email."""
    await limit_attempts(request, "admin_login", body.email)
    admin = await authenticate(session, body.email, body.password)
    if admin is None:
        logger.warning("admin.login_refused")
        raise ApiError(403, ErrorCode.INVALID_CREDENTIALS, "Wrong email or password.")
    started = await store.start(admin.id, remember=False)
    await log.record(
        session, AdminActionKind.LOGIN, admin_id=admin.id, request_id=request_id(request)
    )
    await session.commit()
    set_admin_cookie(response, settings, started)
    return AdminSession(email=admin.email, name=admin.name, expires_at=started.expires_at)


@router.post(
    "/auth/logout",
    operation_id="adminLogout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of the admin console",
)
async def admin_logout(
    request: Request,
    response: Response,
    session: SessionDep,
    store: AdminStoreDep,
    settings: SettingsDep,
) -> None:
    """Ends the session and clears the cookie. A 204 without one, too."""
    if (token := admin_token(request, settings)) is not None:
        if (admin := await find_admin(session, store, token)) is not None:
            await log.record(
                session,
                AdminActionKind.LOGOUT,
                admin_id=admin.admin.id,
                request_id=request_id(request),
            )
            await session.commit()
        await store.end(token)
    clear_admin_cookie(response, settings)


@router.get(
    "/auth/session",
    operation_id="adminSession",
    response_model=AdminSessionState,
    summary="Who the admin cookie is for",
    responses=_UNAUTHORIZED,
)
async def admin_session(admin: AdminDep) -> AdminSessionState:
    """A 401 without a live admin session, so the console can show its sign-in."""
    return AdminSessionState(
        session=AdminSession(
            email=admin.admin.email, name=admin.admin.name, expires_at=admin.expires_at
        )
    )


# --- users ----------------------------------------------------------------------------


@router.get(
    "/users",
    operation_id="adminListUsers",
    response_model=AdminUserPage,
    summary="The user list, filtered",
    responses={**_UNAUTHORIZED, **INVALID},
)
async def list_users(
    _: AdminDep,
    session: SessionDep,
    q: Annotated[
        str | None, Query(max_length=200, description="Part of the email or name.")
    ] = None,
    plan: Annotated[
        str | None,
        Query(description="The plan the account resolves to, by slug: `free` included."),
    ] = None,
    account_status: Annotated[
        Literal["active", "unverified", "deleted", "all"],
        Query(
            alias="status",
            description="`active`: not deleted. `unverified`: not deleted, email unverified.",
        ),
    ] = "active",
    signed_up_from: Annotated[date | None, Query(description="UTC day, inclusive.")] = None,
    signed_up_to: Annotated[date | None, Query(description="UTC day, inclusive.")] = None,
    seen_from: Annotated[date | None, Query(description="UTC day, inclusive.")] = None,
    seen_to: Annotated[date | None, Query(description="UTC day, inclusive.")] = None,
    comped: Annotated[
        bool | None, Query(description="Whether the plan comes from an override.")
    ] = None,
    grandfathered: Annotated[
        bool | None, Query(description="Whether the live subscription is grandfathered.")
    ] = None,
    sort: Annotated[
        Literal["signed_up", "last_seen", "email"], Query(description="What to order by.")
    ] = "signed_up",
    order: Annotated[Literal["asc", "desc"], Query(description="Which way.")] = "desc",
    limit: Annotated[int, Query(ge=1, le=200, description="Accounts per page.")] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000, description="How many matches to skip.")] = 0,
) -> AdminUserPage:
    """Newest signup first by default; never-seen accounts last on `sort=last_seen`."""
    rows, total = await users.list_users(
        session,
        users.UserFilter(
            q=q.strip() if q else None,
            plan=plan,
            status=account_status,
            signed_up_from=signed_up_from,
            signed_up_to=signed_up_to,
            seen_from=seen_from,
            seen_to=seen_to,
            comped=comped,
            grandfathered=grandfathered,
        ),
        sort=sort,
        descending=order == "desc",
        limit=limit,
        offset=offset,
    )
    return AdminUserPage(
        items=[AdminUserRow.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/users/{user_id}",
    operation_id="adminGetUser",
    response_model=AdminUserDetail,
    summary="One account: plan, subscriptions, overrides, usage, admin actions",
    responses={**_UNAUTHORIZED, **_NO_USER, **INVALID},
)
async def get_user(
    user_id: UserId, request: Request, _: AdminDep, session: SessionDep
) -> AdminUserDetail:
    """A deleted account too, until the purge erases it."""
    row = await users.account(session, user_id)
    user = await session.get(AppUser, user_id)
    if row is None or user is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, f"No account {user_id}.")
    subject = users.Subject(user=user, grant=await entitlements.grant_for(session, user_id))
    counts = await users.counts(session, user_id, metrics.utc_today())
    usage = AdminUsage(
        quotas=await usage_of(get_meter(request), session, subject),
        months=[
            AdminUsageMonth.model_validate(month, from_attributes=True)
            for month in await users.usage_months(session, user_id)
        ],
        saved_filters=AdminCount(
            count=counts.saved_filters,
            limit=entitlements.limit(subject, "screener_saved_filters"),
        ),
        watchlist_items=AdminCount(
            count=counts.watchlist_items, limit=entitlements.limit(subject, "watchlist_items")
        ),
        alert_rules=AdminCount(
            count=counts.alert_rules_on, limit=entitlements.limit(subject, "alert_rules")
        ),
        api_keys=AdminApiKeys(live=counts.live_api_keys, last_used_at=counts.api_key_last_used_at),
        data_exports=AdminExports(total=counts.data_exports, last_on=counts.last_data_export),
        active_days_30=counts.active_days_30,
    )
    return AdminUserDetail(
        account=AdminUserRow.model_validate(row, from_attributes=True),
        entitlements=entitlements.entitlements(subject),
        subscriptions=[
            AdminSubscription.model_validate(sub, from_attributes=True)
            for sub in await users.subscriptions(session, user_id)
        ],
        overrides=[
            AdminOverride.model_validate(override, from_attributes=True)
            for override in await users.overrides(session, user_id)
        ],
        usage=usage,
        actions=[
            AdminActionRow.model_validate(action, from_attributes=True)
            for action in await log.recent(session, user_id=user_id)
        ],
    )


async def _regrant(store: SessionStoreDep, user_id: int) -> None:
    """Have the account's sessions read their grant again. Already committed,
    so a Redis that cannot be reached only delays it: sessions re-read on
    their own within five minutes."""
    try:
        await store.forget_grants(user_id)
    except SessionStoreError as exc:
        logger.warning("admin.regrant_failed", user_id=user_id, error=repr(exc.__cause__))


@router.post(
    "/users/{user_id}/override",
    operation_id="adminSetOverride",
    status_code=status.HTTP_201_CREATED,
    response_model=OverrideSet,
    summary="Give the account a plan, with a reason and an expiry",
    responses={**_UNAUTHORIZED, **_NO_USER, **INVALID},
)
async def set_override(
    user_id: UserId,
    body: OverrideRequest,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    store: SessionStoreDep,
) -> OverrideSet:
    """The plan's terms, whole, above the account's subscription, until `expires_at`;
    any override it had ends. Paddle is not told: the subscription, if any, runs on,
    and its terms are back when this ends. `unknown_plan` (422) for a slug no plan has;
    `validation_error` (422) for a reason under 3 characters or an expiry not ahead."""
    try:
        override, replaced = await plans.set_override(
            session,
            user_id=user_id,
            plan=body.plan,
            reason=body.reason,
            expires_at=body.expires_at,
            admin_id=admin.admin.id,
            request_id=request_id(request),
        )
    except plans.PlanChangeError as exc:
        raise _refused(exc) from None
    await session.commit()
    await _regrant(store, user_id)
    [shown] = [row for row in await users.overrides(session, user_id) if row.id == override.id]
    return OverrideSet(
        override=AdminOverride.model_validate(shown, from_attributes=True), replaced=replaced
    )


@router.post(
    "/users/{user_id}/override/end",
    operation_id="adminEndOverride",
    response_model=OverrideEnded,
    summary="End the account's override before its expiry",
    responses={**_UNAUTHORIZED, **_NO_USER, **INVALID},
)
async def end_override(
    user_id: UserId,
    body: ReasonRequest,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    store: SessionStoreDep,
) -> OverrideEnded:
    """A 404 `not_found` when none is running."""
    try:
        ended = await plans.end_override(
            session,
            user_id=user_id,
            admin_id=admin.admin.id,
            reason=body.reason,
            request_id=request_id(request),
        )
    except plans.PlanChangeError as exc:
        raise _refused(exc) from None
    await session.commit()
    await _regrant(store, user_id)
    return OverrideEnded(ended=ended)


async def _reload_catalog(session: AsyncSession) -> None:
    """This process's catalog, with a snapshot just made: the others load it
    within ``ENTITLEMENTS_REFRESH_SECONDS``, and meanwhile resolve the plan it
    copies, which has the same terms."""
    try:
        entitlements.install(await entitlements.load_catalog(session))
    except entitlements.CatalogError as exc:
        logger.error("entitlements.catalog_refused", error=str(exc))


@router.post(
    "/users/{user_id}/grandfather",
    operation_id="adminGrandfather",
    response_model=Grandfathered,
    summary="Pin the account's subscription to its plan's terms today",
    responses={**_UNAUTHORIZED, **_NO_USER, **_CONFLICT, **INVALID},
)
async def grandfather(
    user_id: UserId,
    body: ReasonRequest,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    store: SessionStoreDep,
) -> Grandfathered:
    """The subscription's plan is copied to a private snapshot, or one with the same
    terms reused, and the subscription keeps those terms whatever the plan's become.
    Its price is Paddle's, per subscription, and unchanged. A 409 `no_subscription`
    without a live one; `already_grandfathered` when pinned already."""
    try:
        source, snapshot = await plans.grandfather(
            session,
            user_id=user_id,
            admin_id=admin.admin.id,
            reason=body.reason,
            request_id=request_id(request),
        )
    except plans.PlanChangeError as exc:
        raise _refused(exc) from None
    await session.commit()
    if snapshot.created:
        await _reload_catalog(session)
    await _regrant(store, user_id)
    return Grandfathered(
        plan=source.slug, snapshot=snapshot.plan.slug, snapshot_created=snapshot.created
    )


@router.post(
    "/users/{user_id}/grandfather/end",
    operation_id="adminUngrandfather",
    response_model=Ungrandfathered,
    summary="Put the account's subscription back on its plan's current terms",
    responses={**_UNAUTHORIZED, **_NO_USER, **_CONFLICT, **INVALID},
)
async def ungrandfather(
    user_id: UserId,
    body: ReasonRequest,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
    store: SessionStoreDep,
) -> Ungrandfathered:
    """A 404 `not_found` when it is not grandfathered."""
    try:
        held = await plans.ungrandfather(
            session,
            user_id=user_id,
            admin_id=admin.admin.id,
            reason=body.reason,
            request_id=request_id(request),
        )
    except plans.PlanChangeError as exc:
        raise _refused(exc) from None
    await session.commit()
    await _regrant(store, user_id)
    return Ungrandfathered(snapshot=held.slug)


# --- plans, the log, metrics ------------------------------------------------------------


@router.get(
    "/plans",
    operation_id="adminListPlans",
    response_model=list[AdminPlan],
    summary="Every plan, private ones and snapshots included",
    responses=_UNAUTHORIZED,
)
async def list_plans(_: AdminDep, session: SessionDep) -> list[AdminPlan]:
    """In the order they were made, from the tables, not the catalog: a plan made a
    moment ago is here."""
    source = aliased(Plan)
    rows = (
        await session.execute(
            select(Plan, source.slug)
            .outerjoin(source, source.id == Plan.snapshot_of_plan_id)
            .order_by(Plan.id)
        )
    ).all()
    values: dict[int, dict[str, object]] = {}
    for plan_id, key, value in await session.execute(
        select(PlanEntitlement.plan_id, PlanEntitlement.key, PlanEntitlement.value)
    ):
        values.setdefault(plan_id, {})[key] = value
    return [
        AdminPlan(
            slug=plan.slug,
            name=plan.name,
            price_cents=plan.price_cents,
            currency=plan.currency,
            interval=plan.interval,
            is_public=plan.is_public,
            snapshot_of=snapshot_of,
            snapshot_at=plan.snapshot_at,
            values=values.get(plan.id, {}),
        )
        for plan, snapshot_of in rows
    ]


@router.get(
    "/actions",
    operation_id="adminListActions",
    response_model=AdminActionPage,
    summary="The admin log, newest first",
    responses={**_UNAUTHORIZED, **INVALID},
)
async def list_actions(
    _: AdminDep,
    session: SessionDep,
    user_id: Annotated[int | None, Query(ge=1, description="About this account.")] = None,
    admin_id: Annotated[int | None, Query(ge=1, description="By this admin.")] = None,
    before: Annotated[int | None, Query(ge=1, description="Older than this action id.")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Actions per page.")] = 50,
) -> AdminActionPage:
    """Every sign-in and every change, console and CLI. Append-only: nothing here
    is ever edited or removed."""
    rows = await log.recent(session, user_id=user_id, admin_id=admin_id, before=before, limit=limit)
    return AdminActionPage(
        items=[AdminActionRow.model_validate(row, from_attributes=True) for row in rows],
        next_before=rows[-1].id if len(rows) == limit else None,
    )


def _default_start(end: date, every: str) -> date:
    """The last 90 days, 26 weeks or 12 months to ``end``."""
    if every == "day":
        return end - timedelta(days=89)
    if every == "week":
        return end - timedelta(weeks=25)
    months = end.year * 12 + end.month - 1 - 11
    return date(months // 12, months % 12 + 1, 1)


@router.get(
    "/metrics",
    operation_id="adminMetrics",
    response_model=MetricSeries,
    summary="Signups, actives and plan mix over time",
    responses={**_UNAUTHORIZED, **INVALID},
)
async def get_metrics(
    _: AdminDep,
    session: SessionDep,
    every: Annotated[
        Literal["day", "week", "month"],
        Query(description="A point per UTC day, ISO week or calendar month."),
    ] = "day",
    start: Annotated[date | None, Query(description="First UTC day, inclusive.")] = None,
    end: Annotated[date | None, Query(description="Last UTC day, inclusive: today.")] = None,
) -> MetricSeries:
    """From the daily snapshots, with today as it stands so far. By default the last
    90 days, 26 weeks or 12 months. At most three years at once."""
    today = metrics.utc_today()
    end = min(end or today, today)
    start = start or _default_start(end, every)
    if start > end:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, "start is after end.")
    if (end - start).days > metrics.MAX_SERIES_DAYS:
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR, "At most three years at once: narrow start and end."
        )
    points = await metrics.series(session, start=start, end=end, every=every, today=today)
    return MetricSeries(
        every=every,
        start=start,
        end=end,
        points=[MetricPoint.model_validate(point, from_attributes=True) for point in points],
    )
