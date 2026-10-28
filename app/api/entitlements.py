"""What the caller's plan lets a request see and save: one dependency, every gated route.

A route whose answer depends on the caller's plan takes :data:`EntitlementContextDep`,
and nothing else asks: :class:`EntitlementContext` is the plan's limits resolved
for this request (:func:`app.accounts.entitlements.limit`), turned into the
dates its queries filter on. Signed out is Free, and so is a session Redis
cannot read: the answer is then less than the caller may see, never more.

History is cut in SQL
---------------------
``history_quarters`` is how many quarters back a caller sees, counting the one
being filed now (:func:`~app.core.periods.current_quarter`): Free's 4 on
25 October 2026 are 2025Q4 to 2026Q3. :attr:`EntitlementContext.history_from`
is the first of them, and every query of 13F data a gated route runs has
``period_of_report >= history_from`` in its ``WHERE``. Never a response built
from every period and trimmed: a row the plan does not cover is never read,
so no field computed from it (a first period held, a sparkline, a year's
flows) can carry it out. ``insider_months`` is the same for Form 4 trade days,
counted back from today: :attr:`EntitlementContext.insiders_from`.

The window is the calendar's, not the data's: it moves on the day a quarter
ends, whatever has been filed, and needs no query to know. A request that names
a period or a day before it is a 402 ``plan_limit`` (:class:`PlanLimitError`)
naming the limit and the cheapest plan that reaches (:mod:`app.api.schemas.plan_limit`):
a page asking for 2021 should say why it has nothing, not show nothing. Left to
its defaults, a route answers from the window and says nothing.

Writes
------
A save that would go over a limit (``watchlist_items``,
``screener_saved_filters``, ``alert_rules``) is the same 402:
:meth:`EntitlementContext.require_room`. So is one that needs a flag the plan
does not have (``alerts_instant``): :meth:`EntitlementContext.require_flag`.

The cache
---------
Two plans' answers to the same URL differ, so a gated answer is cached under
:attr:`EntitlementContext.tier`, the window it was built for
(:func:`app.api.cache.cached`), and goes out ``Cache-Control: private`` with
``Vary: Cookie, Authorization``: a browser keeps it for the session or API key
that asked, and no shared cache keeps it at all. The dependency also marks the request, and
:class:`GatedResponseMiddleware` makes every response to a marked request
private, so an uncached route or an error cannot go out ``public`` either.
"""

import calendar
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final, get_args, get_origin

from fastapi import Depends, Request, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.accounts import entitlements
from app.accounts.entitlements import Entitled, FlagKey, LimitKey
from app.api.auth import ApiCallerDep, OptionalViewerDep
from app.api.errors import ApiError, Responses
from app.api.schemas.error import ErrorCode
from app.api.schemas.plan_limit import PlanLimitDetail, PlanLimitProblem, PlanOffer
from app.core.periods import (
    current_quarter,
    next_quarter_end,
    quarter_end,
    quarter_label,
    quarters_between,
    quarters_ending,
)

#: What the dependency marks a request with, in ``scope["state"]``.
GATED: Final = "entitlement_gated"

#: What a gated answer varies by: who is asking is the session's or the API key's.
GATED_VARY: Final = ("Cookie", "Authorization")

#: What every gated route documents.
PLAN_LIMITED: Final[Responses] = {
    status.HTTP_402_PAYMENT_REQUIRED: {
        "model": PlanLimitProblem,
        "description": (
            "`plan_limit`: a period or day before the caller's plan's window, or a save "
            "over its limit. `detail` names the limit and the cheapest plan that lifts it."
        ),
    }
}


def utc_today() -> date:
    """Today in UTC, which the windows count back from, as the cache's closing does."""
    return datetime.now(UTC).date()


def months_before(day: date, months: int) -> date:
    """``day``, ``months`` calendar months earlier: the month's last day where
    it has no such day (31 August, 6 months back, is 28 or 29 February)."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def history_from(quarters: int | None, today: date) -> date | None:
    """The first period of a window of ``quarters``, None for no limit.

    Zero quarters is a window that starts after the quarter being filed: nothing.
    """
    if quarters is None:
        return None
    current = current_quarter(today)
    return next_quarter_end(current) if quarters == 0 else quarters_ending(current, quarters)[0]


def insiders_from(months: int | None, today: date) -> date | None:
    """The first trade day of a window of ``months``, None for no limit.
    Zero months is a window that starts tomorrow: nothing."""
    if months is None:
        return None
    return today + timedelta(days=1) if months == 0 else months_before(today, months)


class PlanLimitError(ApiError):
    """A 402 ``plan_limit``: the caller's plan does not allow the request."""


@dataclass(frozen=True, slots=True)
class EntitlementContext:
    """The caller's plan, as far as this request needs it: its limits, and the
    first period and trade day they let it see.

    Two contexts with the same :attr:`tier` answer every read alike; the viewer
    rides along for the writes' limits and is no part of it.
    """

    history_quarters: int | None
    insider_months: int | None
    today: date
    viewer: Entitled | None = field(default=None, compare=False, repr=False)
    """Who is asking: the signed-in viewer, or the account of the API key the
    request was made with."""
    history_from: date | None = field(init=False)
    """The first period the caller sees; None for every period."""
    insiders_from: date | None = field(init=False)
    """The first trade day the caller sees; None for every day."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "history_from", history_from(self.history_quarters, self.today))
        object.__setattr__(self, "insiders_from", insiders_from(self.insider_months, self.today))

    @classmethod
    def of(cls, viewer: Entitled | None, *, today: date | None = None) -> "EntitlementContext":
        """``viewer``'s, or Free's for None.

        :raises EntitlementsUnavailableError: before a catalog has loaded.
        """
        return cls(
            history_quarters=entitlements.limit(viewer, "history_quarters"),
            insider_months=entitlements.limit(viewer, "insider_months"),
            today=today or utc_today(),
            viewer=viewer,
        )

    @classmethod
    def unlimited(
        cls, *, viewer: Entitled | None = None, today: date | None = None
    ) -> "EntitlementContext":
        """Every period and day, and ``viewer``'s write limits: what a test
        that is not about plans runs as, and ``make explain`` for a paid plan."""
        return cls(
            history_quarters=None, insider_months=None, today=today or utc_today(), viewer=viewer
        )

    @property
    def tier(self) -> str:
        """What a cached answer is keyed on: the window, ``h2025-12-31.i2026-04-25``,
        ``all`` for no limit. The dates rather than the plan, so a window that
        moves at a quarter's end, or each day for trades, is a new key."""
        return f"h{self.history_from or 'all'}.i{self.insiders_from or 'all'}"

    # --- reads ------------------------------------------------------------------

    def first_period(self, quarters: int = 1) -> date | None:
        """The first period whose ``quarters`` quarters ending at it are all in
        the window, such as a year's flows: what a query of periods filters
        ``>=`` on. None for every period."""
        if self.history_from is None:
            return None
        first = self.history_from
        for _ in range(quarters - 1):
            first = next_quarter_end(first)
        return first

    def require_period(self, period: date | None, *, quarters: int = 1) -> None:
        """Refuse ``period`` when it, or with ``quarters`` the first of the
        quarters ending at it, is before the window: 402.

        None, the route's default, is never refused: it is the window's to choose.
        A day that is not a quarter end, as a 13F's period on EDGAR can be, is
        its quarter's.
        """
        if period is None or self.history_from is None:
            return
        quarter = quarter_end(period.year, (period.month - 1) // 3 + 1)
        first = quarters_ending(quarter, quarters)[0]
        if first >= self.history_from:
            return
        assert self.history_quarters is not None
        needed = len(quarters_between(first, current_quarter(self.today)))
        what = (
            quarter_label(quarter)
            if quarters == 1
            else f"the {quarters} quarters to {quarter_label(quarter)}, from {quarter_label(first)}"
        )
        raise self._refused(
            "history_quarters",
            needed,
            f"Your plan shows {_count(self.history_quarters, 'quarter')} of history, from "
            f"{quarter_label(self.history_from)}: {what} is before that.",
        )

    def require_day(self, day: date | None) -> None:
        """Refuse a trade day before the window: 402. None is never refused."""
        if day is None or self.insiders_from is None or day >= self.insiders_from:
            return
        assert self.insider_months is not None
        needed = (self.today.year - day.year) * 12 + self.today.month - day.month
        if months_before(self.today, needed) > day:
            needed += 1
        raise self._refused(
            "insider_months",
            needed,
            f"Your plan shows {_count(self.insider_months, 'month')} of insider trades, from "
            f"{self.insiders_from}: {day} is before that.",
        )

    # --- writes -----------------------------------------------------------------

    def limit(self, key: LimitKey) -> int | None:
        """The caller's ``key``: None for no limit."""
        return entitlements.limit(self.viewer, key)

    def require_room(self, key: LimitKey, saved: int, *, what: str) -> None:
        """Refuse one more when ``saved`` of ``key`` are as many as the plan
        allows: 402.

        :param what: The things, plural, for the message: "watchlist items".
        """
        allowed = self.limit(key)
        if allowed is None or saved < allowed:
            return
        raise self._refused(
            key, saved + 1, f"Your plan keeps {allowed} {what}, and {saved} are saved."
        )

    def can(self, key: FlagKey) -> bool:
        """Whether the caller's plan has ``key`` on."""
        return entitlements.can(self.viewer, key)

    def require_flag(self, key: FlagKey, *, message: str) -> None:
        """Refuse what needs ``key`` when the caller's plan has it off: 402,
        ``allowed`` 0 and ``needed`` 1.

        :param message: What was refused and why, for a person: the plan that
            has it is added.
        """
        if self.can(key):
            return
        plan = flag_offer(key)
        raise PlanLimitError(
            status.HTTP_402_PAYMENT_REQUIRED,
            ErrorCode.PLAN_LIMIT,
            f"{message} {offer_sentence(plan, flag=True)}",
            detail=PlanLimitDetail(limit=key, allowed=0, needed=1, plan=plan),
        )

    def _refused(self, key: LimitKey, needed: int, message: str) -> PlanLimitError:
        allowed = self.limit(key)
        assert allowed is not None  # no limit is never refused
        return plan_limit_error(key, allowed=allowed, needed=needed, message=message)


def offer(key: LimitKey, needed: int) -> PlanOffer | None:
    """The cheapest public plan whose ``key`` reaches ``needed``, as a refusal names it."""
    plan = entitlements.lifting_plan(key, needed)
    if plan is None:
        return None
    return PlanOffer(
        slug=plan.slug,
        name=plan.name,
        price_cents=plan.price_cents,
        currency=plan.currency,
        interval=plan.interval,
        value=plan.values[key],
    )


def flag_offer(key: FlagKey) -> PlanOffer | None:
    """The cheapest public plan with ``key`` on, as a refusal names it: its
    ``value`` is 1, on."""
    plan = entitlements.enabling_plan(key)
    if plan is None:
        return None
    return PlanOffer(
        slug=plan.slug,
        name=plan.name,
        price_cents=plan.price_cents,
        currency=plan.currency,
        interval=plan.interval,
        value=1,
    )


def offer_sentence(plan: PlanOffer | None, *, flag: bool = False) -> str:
    """What a refusal's message ends with: who goes further, or that nobody does."""
    if plan is None:
        return "No plan goes further."
    if flag:
        return f"{plan.name} does."
    return f"{plan.name} " + ("has no limit." if plan.value is None else f"allows {plan.value}.")


def plan_limit_error(key: LimitKey, *, allowed: int, needed: int, message: str) -> PlanLimitError:
    """The 402 for ``key``: ``allowed`` is the caller's, ``needed`` what the
    request needed, and the message gains the plan that lifts it."""
    plan = offer(key, needed)
    return PlanLimitError(
        status.HTTP_402_PAYMENT_REQUIRED,
        ErrorCode.PLAN_LIMIT,
        f"{message} {offer_sentence(plan)}",
        detail=PlanLimitDetail(limit=key, allowed=allowed, needed=needed, plan=plan),
    )


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


async def entitlement_context(
    request: Request, viewer: OptionalViewerDep, caller: ApiCallerDep
) -> EntitlementContext:
    """The caller's :class:`EntitlementContext`, and the request marked gated
    (:class:`GatedResponseMiddleware`). A request made with an API key is its
    account's, whatever cookie came with it."""
    setattr(request.state, GATED, True)
    return EntitlementContext.of(caller or viewer, today=utc_today())


EntitlementContextDep = Annotated[EntitlementContext, Depends(entitlement_context)]


def takes_entitlements(endpoint: Callable[..., Any]) -> bool:
    """Whether ``endpoint`` declares an :class:`EntitlementContext` parameter."""
    for parameter in inspect.signature(endpoint).parameters.values():
        annotation = parameter.annotation
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        if annotation is EntitlementContext:
            return True
    return False


# --- every gated response private ---------------------------------------------------

_PUBLIC: Final = re.compile(r"\bpublic\b", re.IGNORECASE)


def private_headers(headers: MutableHeaders) -> None:
    """Make a response one only the caller's browser may keep: ``public`` becomes
    ``private``, no ``Cache-Control`` becomes ``private, no-cache``, and ``Vary``
    gains ``Cookie`` and ``Authorization``, the session and the API key.
    ``no-store`` is left alone: it is stricter."""
    cache_control = headers.get("cache-control")
    if cache_control is None:
        headers["cache-control"] = "private, no-cache"
    elif "no-store" not in cache_control and "private" not in cache_control:
        replaced = _PUBLIC.sub("private", cache_control)
        if replaced == cache_control:
            replaced = f"private, {cache_control}"
        headers["cache-control"] = replaced
    varies = {name.strip().lower() for name in headers.get("vary", "").split(",")}
    if "*" not in varies:
        for name in GATED_VARY:
            if name.lower() not in varies:
                headers.add_vary_header(name)


class GatedResponseMiddleware:
    """Every response to a request :func:`entitlement_context` marked goes out
    :func:`private_headers`: the cached ones, the uncached ones, and the errors.

    :func:`app.api.cache.cached` writes a gated answer's headers private itself;
    this is so that nothing else on a gated route can go out ``public`` by
    forgetting to.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start" and scope.get("state", {}).get(GATED):
                private_headers(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_private)
