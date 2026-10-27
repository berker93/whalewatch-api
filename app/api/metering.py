"""Every request made with an API key spends one of its account's monthly calls.

The data routers take :data:`MeteredDep` (app/main.py): a request with an API
key (:data:`app.api.auth.ApiCallerDep`) is counted against its plan's
``api_calls_month`` before the route runs, in Redis (:mod:`app.accounts.usage`).
A request without one is not counted: the website's own calls, signed in or
not, spend nothing. The MCP server's calls are counted the same way against
``mcp_calls_month``, by :func:`spend` with that key.

What a keyed request is told
----------------------------
Every answer to one says where its month stands:

- ``X-RateLimit-Limit``: the month's quota;
- ``X-RateLimit-Remaining``: what is left of it after this request;
- ``X-RateLimit-Reset``: when it resets, in Unix seconds: the subscription's
  next anniversary, at midnight UTC.

These replace the per-address minute's headers of the same names
(:class:`app.api.rate_limit.RateLimitMiddleware`), which still applies: a
request over the minute is a 429 ``rate_limited`` with ``Retry-After``,
whatever its quota. A plan with no limit sends none of the three.

Refusals
--------
- A plan with no calls at all (``api_calls_month`` 0: Free, Pro) is a 402
  ``plan_limit`` naming the cheapest plan with some.
- A month whose quota is spent is a 429 ``quota_exceeded``, with the three
  headers (``Remaining: 0``) and ``Retry-After``, the seconds to the reset.
  A refused request spends nothing.

The request that takes a month to 80% claims the warning email
(:func:`app.accounts.usage.claim_warning`) and sends it after answering.

A Redis that cannot be reached lets keyed requests through uncounted, and
logs: as the minute's limit does, an outage takes the meter away for a while
rather than taking the API down with it. An app with no meter, as in a test
that does not run the lifespan, counts nothing.
"""

from contextlib import suppress
from typing import Final, Protocol

from fastapi import BackgroundTasks, Depends, Request, status
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.accounts import entitlements
from app.accounts.api_keys import KeyHolder
from app.accounts.delivery import Courier
from app.accounts.entitlements import Grant
from app.accounts.mail import quota_warning
from app.accounts.sessions import now
from app.accounts.usage import (
    QUOTA_NAMES,
    MeteredKey,
    Spent,
    UsageMeter,
    anchor_of,
    claim_warning,
    quota_period,
    stored_count,
)
from app.api.auth import INVALID_API_KEY, ApiCallerDep
from app.api.deps import CourierDep, SessionDep, SettingsDep
from app.api.entitlements import GATED, PLAN_LIMITED, offer, offer_sentence, plan_limit_error
from app.api.errors import ApiError, Responses
from app.api.schemas.error import ErrorCode
from app.api.schemas.usage import QuotaDetail, QuotaProblem, QuotaUsage, Usage
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import AppUser

logger = get_logger(__name__)

#: Where the dependency leaves the headers for :class:`QuotaHeadersMiddleware`.
QUOTA_HEADERS: Final = "quota_headers"

#: What every metered route documents, beside the minute's 429.
METERED: Final[Responses] = {
    **INVALID_API_KEY,
    status.HTTP_402_PAYMENT_REQUIRED: PLAN_LIMITED[status.HTTP_402_PAYMENT_REQUIRED],
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": QuotaProblem,
        "description": (
            "`rate_limited`: too many requests from this address; wait `Retry-After` "
            "seconds. `quota_exceeded`: the API key's month of calls is spent; "
            "`X-RateLimit-Reset` says when it resets."
        ),
        "headers": {
            "Retry-After": {
                "description": "Seconds until a request will be answered.",
                "schema": {"type": "integer"},
            },
            "X-RateLimit-Limit": {
                "description": "With an API key: the month's quota of calls.",
                "schema": {"type": "integer"},
            },
            "X-RateLimit-Remaining": {
                "description": "With an API key: the calls left this month.",
                "schema": {"type": "integer"},
            },
            "X-RateLimit-Reset": {
                "description": "With an API key: when the month's quota resets, in Unix seconds.",
                "schema": {"type": "integer"},
            },
        },
    },
}


def get_meter(request: Request) -> UsageMeter | None:
    """The quota counters, on the app's Redis: built by the lifespan."""
    meter: UsageMeter | None = getattr(request.app.state, "meter", None)
    return meter


def quota_headers(spent: Spent) -> dict[str, str]:
    """The three ``X-RateLimit-*`` headers for where ``spent`` left the month;
    none for no limit."""
    if spent.limit is None or spent.remaining is None:
        return {}
    return {
        "x-ratelimit-limit": str(spent.limit),
        "x-ratelimit-remaining": str(spent.remaining),
        "x-ratelimit-reset": str(int(spent.period.resets_at.timestamp())),
    }


async def spend(
    request: Request,
    background: BackgroundTasks,
    courier: Courier,
    caller: KeyHolder,
    key: MeteredKey,
    session: AsyncSession,
    settings: Settings,
) -> Spent | None:
    """Count one ``key`` for ``caller``'s account, or refuse the request.
    None when nothing could count it.

    :raises ApiError: 402 ``plan_limit`` for a plan with none of ``key``,
        429 ``quota_exceeded`` for a month with none left.
    """
    # Its answer is its account's, whatever the route: nobody else's cache keeps it.
    setattr(request.state, GATED, True)
    limit = entitlements.limit(caller, key)
    if limit == 0:
        raise plan_limit_error(
            key,
            allowed=0,
            needed=1,
            message=f"Your plan has no {QUOTA_NAMES[key]}.",
        )
    meter = get_meter(request)
    if meter is None:
        return None
    at = now()
    period = quota_period(anchor_of(caller.user, entitlements.live_grant(caller)), at)
    try:
        spent = await meter.spend(session, caller.user.id, key, limit=limit, period=period)
    except (RedisError, OSError) as exc:
        logger.warning("usage.unavailable", error=repr(exc))
        return None
    headers = quota_headers(spent)
    if not spent.admitted:
        assert spent.limit is not None  # no limit is never refused
        logger.info("usage.refused", key=key, used=spent.used, limit=spent.limit)
        plan = offer(key, spent.limit + 1)
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            ErrorCode.QUOTA_EXCEEDED,
            f"This month's {spent.limit:,} {QUOTA_NAMES[key]} are spent: the quota resets on "
            f"{period.end:%d %B %Y}. {offer_sentence(plan)}",
            detail=QuotaDetail(
                limit=key,
                allowed=spent.limit,
                used=spent.used,
                resets_at=period.resets_at,
                plan=plan,
            ),
            headers={**headers, "retry-after": str(period.seconds_left(at))},
        )
    setattr(request.state, QUOTA_HEADERS, headers)
    if spent.warn:
        await _warn(meter, background, courier, caller, key, spent, session, settings)
    return spent


async def _warn(
    meter: UsageMeter,
    background: BackgroundTasks,
    courier: Courier,
    caller: KeyHolder,
    key: MeteredKey,
    spent: Spent,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Claim the month's 80% email and send it after the answer, if this
    request's claim is the one that wrote it."""
    user = caller.user
    try:
        claimed = await claim_warning(session, user.id, key, spent.period, spent.used)
        await session.commit()
    except SQLAlchemyError as exc:
        logger.warning("usage.warning_unclaimed", error=repr(exc))
        await session.rollback()
        with suppress(RedisError, OSError):
            await meter.unwarn(user.id, key, spent.period)
        return
    if not claimed:
        return
    assert spent.limit is not None  # no limit never warns
    logger.info("usage.warned", key=key, used=spent.used, limit=spent.limit)
    background.add_task(
        courier.send,
        quota_warning(
            settings,
            to=user.email,
            user_id=user.id,
            what=QUOTA_NAMES[key],
            used=spent.used,
            limit=spent.limit,
            resets_on=spent.period.end,
        ),
    )


class Holder(Protocol):
    """An account and its grant: a viewer, an API key's caller, or an account
    an admin is looking at (:class:`app.admin.users.Subject`)."""

    @property
    def user(self) -> AppUser: ...

    @property
    def grant(self) -> Grant | None: ...


async def usage_of(meter: UsageMeter | None, session: AsyncSession, who: Holder) -> Usage:
    """What ``who``'s account has spent of each quota this month: what the
    account page shows before the 429 does."""
    period = quota_period(anchor_of(who.user, entitlements.live_grant(who)), now())
    keys: list[MeteredKey] = ["api_calls_month", "mcp_calls_month"]
    if meter is None:
        used = {key: await stored_count(session, who.user.id, key, period.start) for key in keys}
    else:
        used = await meter.used(session, who.user.id, keys, period)

    def quota(key: MeteredKey) -> QuotaUsage:
        limit = entitlements.limit(who, key)
        return QuotaUsage(
            used=used[key],
            limit=limit,
            remaining=None if limit is None else max(0, limit - used[key]),
            period_start=period.start,
            resets_at=period.resets_at,
        )

    return Usage(api_calls_month=quota("api_calls_month"), mcp_calls_month=quota("mcp_calls_month"))


async def spend_api_call(
    request: Request,
    background: BackgroundTasks,
    caller: ApiCallerDep,
    session: SessionDep,
    settings: SettingsDep,
    courier: CourierDep,
) -> None:
    """One of the API key's ``api_calls_month``, for every data endpoint."""
    if caller is not None:
        await spend(request, background, courier, caller, "api_calls_month", session, settings)


MeteredDep = Depends(spend_api_call)


class QuotaHeadersMiddleware:
    """Put the quota headers :func:`spend` left on the request on its answer.

    Not the route's ``Response``: a cached answer is a response of its own,
    which FastAPI does not copy a dependency's headers onto. A refusal carries
    its own.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_quota(message: Message) -> None:
            if message["type"] == "http.response.start":
                quota = scope.get("state", {}).get(QUOTA_HEADERS)
                if quota:
                    headers = MutableHeaders(scope=message)
                    for name, value in quota.items():
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_quota)
