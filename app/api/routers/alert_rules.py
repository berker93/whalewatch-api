"""The signed-in user's alert rules: what they asked to be told about, and how.

``GET /v1/me/alert-rules``
    Every rule, on and off, oldest first, each with its target named and
    whether the plan sends its kind.
``POST /v1/me/alert-rules``
    Make a rule, or set every setting of the one there is for that kind and
    target (there is one per kind and target): 201 made, 200 set. It is on
    after either.
``PUT /v1/me/alert-rules/presets``
    One click from the watchlist: "alert me about this investor" turns on its
    filing and its large changes, "this stock" its large ownership moves and
    its insiders buying together (:mod:`app.accounts.alert_rules`). A rule
    there already is turned on with the settings it has. Idempotent.
``PATCH /v1/me/alert-rules/{id}``
    Change its threshold or channels, or turn it on or off.
``DELETE /v1/me/alert-rules/{id}``
    Delete it. Idempotent. Its notifications stay.
``POST /v1/me/alert-rules/{id}/test``
    Send a test alert as the rule would send one: a notification in the bell,
    and an email if the rule emails. One a minute per rule.

What a plan allows
------------------
Free is sent the season digest only: making, turning on or testing a rule of
any other kind is a 402 ``plan_limit`` on ``alerts_instant``. ``alert_rules``
is how many rules a plan keeps on at once; turning on one more is a 402 on
it. Past :data:`~app.accounts.alert_rules.MAX_ALERT_RULES` rules, on or off,
which no plan lifts, a 422. Turning a rule off, changing its settings and
deleting it are never refused.

Every write needs a verified email, as the watchlist's do; reading does not.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Path, Response, status
from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import alert_rules as rules
from app.accounts import mail
from app.accounts.alert_rules import INVESTOR_PRESETS, MAX_ALERT_RULES, STOCK_PRESETS, Target
from app.accounts.entitlements import Entitled
from app.accounts.unsubscribe import alert_label
from app.api.auth import UNAUTHORIZED, UNVERIFIED, NoStoreDep, VerifiedViewerDep, ViewerDep
from app.api.deps import CourierDep, RedisDep, SessionDep, SettingsDep
from app.api.entitlements import (
    PLAN_LIMITED,
    EntitlementContext,
    EntitlementContextDep,
    plan_limit_error,
)
from app.api.errors import INVALID, RATE_LIMITED, ApiError, Responses, invalid, not_found
from app.api.routers.notifications import forget_count
from app.api.routers.watchlist import resolve_ref
from app.api.schemas.alert_rule import (
    AlertPresetRequest,
    AlertRule,
    AlertRuleChange,
    AlertRules,
    AlertTest,
    NewAlertRule,
    RuleInvestor,
    RuleStock,
)
from app.api.schemas.error import ErrorCode
from app.api.schemas.notification import Notification as NotificationOut
from app.api.schemas.watchlist import WatchInsider, WatchInvestor, WatchRef, WatchStock
from app.db.models import (
    FILER_KINDS,
    PAYLOAD_KEYS,
    SECURITY_KINDS,
    THRESHOLD_KINDS,
    AlertKind,
    Channel,
    Filer,
    Notification,
    Security,
)
from app.db.models import AlertRule as AlertRuleRow

router = APIRouter(prefix="/me/alert-rules", tags=["alert-rules"], dependencies=[NoStoreDep])

_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)

#: How long one rule's test lasts: a second within it is refused.
TEST_EVERY: Final = timedelta(minutes=1)


def utc_now() -> datetime:
    """Now, which a test's minute is counted from."""
    return datetime.now(UTC)

_NO_RULE: Final = not_found("No alert rule with that id is the signed-in user's.")

_TESTED: Final[Responses] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        **RATE_LIMITED[status.HTTP_429_TOO_MANY_REQUESTS],
        "description": (
            "Too many requests from this address, or this rule was tested less than a "
            "minute ago. Wait `Retry-After` seconds."
        ),
    }
}


# --- reading ----------------------------------------------------------------------


def _rules_query(user_id: int) -> Select[Any]:
    return (
        select(
            AlertRuleRow,
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            Security.cusip,
            Security.ticker,
            Security.name.label("issuer_name"),
        )
        .outerjoin(Filer, Filer.id == AlertRuleRow.filer_id)
        .outerjoin(Security, Security.id == AlertRuleRow.security_id)
        .where(AlertRuleRow.user_id == user_id)
        .order_by(AlertRuleRow.id)
        # A rule this session loaded before an upsert changed it is read
        # again, not answered from the session's copy.
        .execution_options(populate_existing=True)
    )


def _rule(row: Any, sent: frozenset[AlertKind]) -> AlertRule:
    rule: AlertRuleRow = row[0]
    return AlertRule(
        id=str(rule.id),
        kind=AlertKind(rule.kind),
        investor=RuleInvestor(slug=row.slug, display_name=row.display_name)
        if rule.filer_id is not None
        else None,
        stock=RuleStock(cusip=row.cusip, ticker=row.ticker, issuer_name=row.issuer_name)
        if rule.security_id is not None
        else None,
        min_value=rule.min_value,
        channels=[Channel(channel) for channel in rule.channels],
        is_active=rule.is_active,
        sent=AlertKind(rule.kind) in sent,
        created_at=rule.created_at,
    )


async def _list(
    session: AsyncSession, who: Entitled | None, user_id: int, target: Target | None = None
) -> AlertRules:
    statement = _rules_query(user_id)
    if target is not None:
        statement = statement.where(*rules.target_is(target))
    sent = rules.sent_kinds(who)
    return AlertRules(data=[_rule(row, sent) for row in await session.execute(statement)])


async def _one(
    session: AsyncSession, who: Entitled | None, user_id: int, rule_id: int
) -> AlertRule:
    row = (await session.execute(_rules_query(user_id).where(AlertRuleRow.id == rule_id))).one()
    return _rule(row, rules.sent_kinds(who))


# --- refusals -----------------------------------------------------------------------


def _require_sent(entitled: EntitlementContext, kind: AlertKind) -> None:
    """402 on ``alerts_instant`` for a kind the plan does not send."""
    if kind in rules.sent_kinds(entitled.viewer):
        return
    entitled.require_flag(
        "alerts_instant",
        message=(
            f"Your plan sends the season digest only: alerts of {alert_label(kind, None)} "
            "are sent as they happen, on a plan with instant alerts."
        ),
    )


async def _require_room(
    session: AsyncSession, entitled: EntitlementContext, user_id: int, more: int
) -> None:
    """402 on ``alert_rules`` when ``more`` rules turned on would be past the plan's."""
    allowed = entitled.limit("alert_rules")
    if allowed is None or more == 0:
        return
    on = await rules.rules_on(session, user_id, rules.sent_kinds(entitled.viewer))
    if on + more <= allowed:
        return
    raise plan_limit_error(
        "alert_rules",
        allowed=allowed,
        needed=on + more,
        message=(
            f"Your plan keeps {allowed} alert {'rule' if allowed == 1 else 'rules'} on at "
            f"once, and {on} {'is' if on == 1 else 'are'} on."
        ),
    )


async def _require_kept(session: AsyncSession, user_id: int, more: int) -> None:
    """422 when ``more`` new rules would be past what any account keeps."""
    if more and await rules.rules_kept(session, user_id) + more > MAX_ALERT_RULES:
        raise invalid(
            "ref", f"at most {MAX_ALERT_RULES} alert rules are kept: delete some", where="body"
        )


def _require_threshold_kind(kind: AlertKind, min_value: object) -> None:
    if min_value is not None and kind not in THRESHOLD_KINDS:
        raise invalid(
            "min_value",
            f"{kind} is not about dollars: only {', '.join(THRESHOLD_KINDS)} take a threshold",
            where="body",
        )


async def _target(session: AsyncSession, kind: AlertKind, ref: WatchRef | None) -> Target:
    """The rule's target for ``kind``, from ``ref``.

    :raises RequestValidationError: A ref of the wrong kind, or one missing.
    :raises HTTPException: 404, a ref that names nothing.
    """
    named = None if ref is None else ref.root
    if kind in FILER_KINDS:
        if not isinstance(named, WatchInvestor):
            raise invalid("ref", f"{kind} is about an investor: send one", where="body")
    elif kind in SECURITY_KINDS:
        if not isinstance(named, WatchStock):
            raise invalid("ref", f"{kind} is about a stock: send one", where="body")
    elif named is not None:
        raise invalid("ref", f"{kind} is about no one thing: send no ref", where="body")
    return {} if named is None else await resolve_ref(session, named)


# --- the routes -------------------------------------------------------------------


@router.get(
    "",
    operation_id="getAlertRules",
    response_model=AlertRules,
    summary="The signed-in user's alert rules",
    responses=UNAUTHORIZED,
)
async def read_alert_rules(viewer: ViewerDep, session: SessionDep) -> AlertRules:
    """Every rule, on and off, oldest first. None is an empty list, never a 404."""
    return await _list(session, viewer, viewer.user.id)


@router.post(
    "",
    operation_id="saveAlertRule",
    response_model=AlertRule,
    status_code=status.HTTP_201_CREATED,
    summary="Make an alert rule, or set the one there is",
    responses={
        status.HTTP_200_OK: {
            "model": AlertRule,
            "description": "The rule for that kind and target was there: its settings are set.",
        },
        **UNAUTHORIZED,
        **PLAN_LIMITED,
        **UNVERIFIED,
        **not_found("No stock with that CUSIP, or investor with that slug."),
        **INVALID,
    },
)
async def save_alert_rule(
    body: NewAlertRule,
    response: Response,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
) -> AlertRule:
    """One rule per kind and target: the first is made (201), and a second
    sets every setting of the first (200), `min_value` included, so one left
    out is cleared. On after either. A kind the plan does not send (every one
    but `season_digest` on Free) is a 402 `plan_limit` on `alerts_instant`;
    one rule on past the plan's `alert_rules`, a 402 on that; a ref of the
    wrong kind, or a threshold on a kind not about dollars, a 422."""
    kind = body.kind
    _require_threshold_kind(kind, body.min_value)
    target = await _target(session, kind, body.ref)
    _require_sent(entitled, kind)
    user_id = viewer.user.id
    await rules.lock(session, user_id)
    existing = await session.scalar(
        select(AlertRuleRow).where(
            AlertRuleRow.user_id == user_id,
            AlertRuleRow.kind == kind,
            *rules.target_is(target),
        )
    )
    await _require_kept(session, user_id, 0 if existing else 1)
    await _require_room(session, entitled, user_id, 0 if existing and existing.is_active else 1)
    settings = {
        "min_value": body.min_value,
        "channels": [str(channel) for channel in body.channels],
        "is_active": True,
    }
    rule_id = await session.scalar(
        insert(AlertRuleRow)
        .values(
            user_id=user_id,
            kind=str(kind),
            filer_id=target.get("filer_id"),
            security_id=target.get("security_id"),
            **settings,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "kind", "filer_id", "security_id"], set_=settings
        )
        .returning(AlertRuleRow.id)
    )
    assert rule_id is not None
    await session.commit()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
    return await _one(session, entitled.viewer, user_id, rule_id)


@router.put(
    "/presets",
    operation_id="applyAlertPresets",
    response_model=AlertRules,
    summary="Alert me about this investor, or this stock",
    responses={
        **UNAUTHORIZED,
        **PLAN_LIMITED,
        **UNVERIFIED,
        **not_found("No stock with that CUSIP, or investor with that slug."),
        **INVALID,
    },
)
async def apply_alert_presets(
    body: AlertPresetRequest,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
) -> AlertRules:
    """An investor: its filings, by email too, and its position changes of $1M
    or more. A stock: moves of $1M or more in who owns it, and its insiders
    buying together. A rule of these there is already is turned on, with the
    threshold and channels it has. Answers with every rule about `ref`.

    On a plan that sends the season digest only, a 402 `plan_limit` on
    `alerts_instant`; past the plan's `alert_rules`, a 402 on that, and
    nothing is turned on. An insider is a 422: no alert is about one yet."""
    named = body.ref.root
    if isinstance(named, WatchInsider):
        raise invalid("ref", "no alert is about an insider yet", where="body")
    presets = INVESTOR_PRESETS if isinstance(named, WatchInvestor) else STOCK_PRESETS
    target = await resolve_ref(session, named)
    _require_sent(entitled, presets[0].kind)
    user_id = viewer.user.id
    await rules.lock(session, user_id)
    todo = await rules.pending(session, user_id, presets, target)
    await _require_kept(session, user_id, todo.new)
    await _require_room(session, entitled, user_id, len(todo.presets))
    await rules.write(session, user_id, todo.presets, target)
    await session.commit()
    return await _list(session, entitled.viewer, user_id, target)


RuleIdPath = Annotated[int, Path(ge=1, le=10**18 - 1, description="The rule's `id`.")]


async def _mine(session: AsyncSession, user_id: int, rule_id: int) -> AlertRuleRow:
    rule = await session.scalar(
        select(AlertRuleRow)
        .where(AlertRuleRow.id == rule_id, AlertRuleRow.user_id == user_id)
        .with_for_update()
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No alert rule with that id is yours.")
    return rule


@router.patch(
    "/{rule_id}",
    operation_id="changeAlertRule",
    response_model=AlertRule,
    summary="Change an alert rule, or turn it on or off",
    responses={**UNAUTHORIZED, **PLAN_LIMITED, **UNVERIFIED, **_NO_RULE, **INVALID},
)
async def change_alert_rule(
    rule_id: RuleIdPath,
    body: AlertRuleChange,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
) -> AlertRule:
    """Sets what is sent and leaves the rest. `min_value: null` clears the
    threshold. Turning on a kind the plan does not send, or one rule past the
    plan's `alert_rules`, is a 402 `plan_limit`; turning off, and changing a
    setting, never is. Another user's rule is a 404."""
    user_id = viewer.user.id
    await rules.lock(session, user_id)
    rule = await _mine(session, user_id, rule_id)
    kind = AlertKind(rule.kind)
    if "min_value" in body.model_fields_set:
        _require_threshold_kind(kind, body.min_value)
        rule.min_value = body.min_value
    if body.channels is not None:
        rule.channels = [str(channel) for channel in body.channels]
    if body.is_active is not None:
        if body.is_active and not rule.is_active:
            _require_sent(entitled, kind)
            await _require_room(session, entitled, user_id, 1)
        rule.is_active = body.is_active
    await session.commit()
    return await _one(session, entitled.viewer, user_id, rule_id)


@router.delete(
    "/{rule_id}",
    operation_id="deleteAlertRule",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an alert rule",
    responses={**UNAUTHORIZED, **UNVERIFIED, **INVALID},
)
async def delete_alert_rule(
    rule_id: RuleIdPath, viewer: VerifiedViewerDep, session: SessionDep
) -> None:
    """Deletes it; the notifications it sent stay. One already deleted, or
    another user's, is no change: 204 either way."""
    await session.execute(
        delete(AlertRuleRow).where(
            AlertRuleRow.id == rule_id, AlertRuleRow.user_id == viewer.user.id
        )
    )
    await session.commit()


@router.post(
    "/{rule_id}/test",
    operation_id="testAlertRule",
    response_model=AlertTest,
    summary="Send a test alert",
    responses={**UNAUTHORIZED, **PLAN_LIMITED, **UNVERIFIED, **_NO_RULE, **_TESTED, **INVALID},
)
async def send_test_alert(
    rule_id: RuleIdPath,
    viewer: VerifiedViewerDep,
    entitled: EntitlementContextDep,
    session: SessionDep,
    redis: RedisDep,
    courier: CourierDep,
    settings: SettingsDep,
) -> AlertTest:
    """Sends what an alert of this rule looks like, saying it is a test: a
    notification, unread, with `payload.test` true and the rule's investor or
    stock, and, when the rule emails, an email, which is sent before this
    answers. A rule that is off is a 422; one of a kind the plan does not
    send, a 402 `plan_limit`; a second test of a rule within a minute, a 429
    with `Retry-After`."""
    user = viewer.user
    found = (
        await session.execute(_rules_query(user.id).where(AlertRuleRow.id == rule_id))
    ).one_or_none()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No alert rule with that id is yours.")
    rule: AlertRuleRow = found[0]
    kind = AlertKind(rule.kind)
    if not rule.is_active:
        raise invalid("rule_id", "the rule is off: turn it on to test it", where="path")
    _require_sent(entitled, kind)

    label = alert_label(kind, found.display_name or found.ticker or found.issuer_name)
    title = f"Test alert: {label[0].upper()}{label[1:]}"
    body = f"This is how an alert of {label} will look. Nothing has happened: it is a test."
    now = utc_now()
    payload: dict[str, Any] = dict.fromkeys(PAYLOAD_KEYS) | {
        "ticker": found.ticker,
        "filer_slug": found.slug,
        "as_of": now.date().isoformat(),
        "test": True,
    }
    # One a minute per rule, by the key's idempotency: a second test within
    # the minute names the same event, and is nothing.
    minute = now.replace(second=0, microsecond=0)
    written = (
        await session.execute(
            insert(Notification)
            .values(
                user_id=user.id,
                kind=str(kind),
                title=title,
                body=body,
                payload=payload,
                dedupe_key=f"{kind}:test:{rule.id}:{minute:%Y%m%dT%H%M}",
            )
            .on_conflict_do_nothing(constraint="uq_notification_user_id_dedupe_key")
            .returning(Notification.id, Notification.created_at)
        )
    ).one_or_none()
    if written is None:
        wait = max(1, int((minute + TEST_EVERY - now).total_seconds()) + 1)
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            ErrorCode.RATE_LIMITED,
            f"This rule was tested less than a minute ago: try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    await session.commit()
    await forget_count(redis, user.id)

    email = None
    if Channel.EMAIL in rule.channels:
        email = await courier.send(
            mail.alert(
                settings,
                to=user.email,
                user_id=user.id,
                rule_id=rule.id,
                kind=kind,
                title=title,
                body=body,
                payload=payload,
            )
        )
    return AlertTest(
        notification=NotificationOut(
            id=str(written.id),
            kind=kind,
            title=title,
            body=body,
            payload=payload,
            created_at=written.created_at,
            read_at=None,
        ),
        email=email,
    )
