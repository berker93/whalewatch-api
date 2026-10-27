"""TIER-6, against a real Postgres: Paddle's webhooks through the endpoint, and dunning.

Every notification here is shaped as Paddle's sandbox sends it (the payloads
in developer.paddle.com's webhook reference, trimmed to what this API reads
plus enough beside it to be recognisable), signed with the test's secret the
way Paddle signs, and posted to ``/webhooks/paddle``. Paddle's API is a
``MockTransport``. What a test asserts is what the account sees: ``GET
/v1/me``'s plan, ``GET /v1/billing``, and the outbox.

Times are relative to now, because the grant is resolved against Postgres's
``now()``: a renewal that failed eight days ago is past its seven days' grace.
"""

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import privacy
from app.accounts.billing import cancel_for_deleted, run_dunning
from app.accounts.paddle import PaddleClient, http_client, sign
from app.admin import plans as admin_plans
from app.api.routers.billing import WEBHOOK_PATH, get_optional_paddle, get_paddle
from app.core.config import Settings
from app.db.models import AdminUser, PaddleEvent, Subscription
from tests.integration.accounts import (
    FRONTEND,
    PASSWORD,
    Outbox,
    make_user,
    sign_in,
    subscribe,
)

SECRET = "pdl_ntfset_01hv8_sandbox_secret"
PRICES = {"pro": "pri_01pro_monthly", "api_pro": "pri_01apipro_monthly"}
SUB = "sub_01hv8x29kz0t586xy6zn1a62ny"
CUSTOMER = "ctm_01hv6y1jedq4p1n0yqn5ba3ky4"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The integration suite's, with a Paddle sandbox account configured."""
    return settings.model_copy(
        update={
            "paddle_api_key": SecretStr("pdl_sdbx_apikey_01hv8_test"),
            "paddle_webhook_secret": SecretStr(SECRET),
            "paddle_prices": dict(PRICES),
        }
    )


@pytest.fixture
def billing_outbox(monkeypatch: pytest.MonkeyPatch, outbox: Outbox) -> Outbox:
    monkeypatch.setattr("app.api.routers.billing.mailer_for", lambda settings: outbox)
    return outbox


class FakePaddle:
    """Paddle's API, answering as the sandbox does, and remembering what it was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            return httpx.Response(500, json={"error": {"code": "internal_error"}})
        if request.url.path.startswith("/subscriptions/"):
            sub = request.url.path.split("/")[2]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": sub,
                        "status": "canceled",
                        "scheduled_change": None,
                        "canceled_at": _at(now()),
                    }
                },
            )
        if request.url.path == "/transactions":
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "txn_01hv8wptq8987qeep44cyrewp9",
                        "status": "draft",
                        "checkout": {
                            "url": "https://whalewatch.io/pay?_ptxn=txn_01hv8wptq8987qeep44cyrewp9"
                        },
                    }
                },
            )
        customer = request.url.path.split("/")[2]
        subscription_ids = json.loads(request.content)["subscription_ids"]
        return httpx.Response(
            201,
            json={
                "data": {
                    "id": "cpls_01",
                    "customer_id": customer,
                    "urls": {
                        "general": {"overview": f"https://customer-portal.paddle.com/{customer}"},
                        "subscriptions": [
                            {
                                "id": sub,
                                "cancel_subscription": f"https://customer-portal.paddle.com/{sub}/cancel",
                                "update_subscription_payment_method": f"https://customer-portal.paddle.com/{sub}/card",
                            }
                            for sub in subscription_ids
                        ],
                    },
                }
            },
        )


@pytest.fixture
def paddle(app: FastAPI, client: AsyncClient) -> FakePaddle:
    fake = FakePaddle()

    async def override() -> AsyncIterator[PaddleClient]:
        # The app's own client, key and all, on a transport that is not the network.
        async with http_client(app.state.settings, httpx.MockTransport(fake.handle)) as http:
            yield PaddleClient(http)

    # Both ways a route reaches Paddle: nothing here may leave the machine.
    app.dependency_overrides[get_paddle] = override
    app.dependency_overrides[get_optional_paddle] = override
    return fake


# --- notifications, as Paddle sends them -----------------------------------------------

_events = iter(range(1, 1_000_000))


def _at(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def subscription_event(
    event_type: str,
    occurred_at: datetime,
    *,
    status: str = "active",
    price: str = PRICES["pro"],
    user_id: int | None = None,
    sub: str = SUB,
    customer: str = CUSTOMER,
    period_end: datetime | None = None,
    scheduled_change: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """A ``subscription.*`` notification carrying the whole subscription."""
    started = occurred_at - timedelta(days=1)
    return {
        "event_id": event_id or f"evt_{next(_events):026d}",
        "event_type": event_type,
        "occurred_at": _at(occurred_at),
        "notification_id": f"ntf_{next(_events):026d}",
        "data": {
            "id": sub,
            "status": status,
            "customer_id": customer,
            "address_id": "add_01hv8gq3318ktkfengj2r75gfx",
            "business_id": None,
            "currency_code": "USD",
            "created_at": _at(started),
            "updated_at": _at(occurred_at),
            "started_at": _at(started),
            "first_billed_at": _at(started),
            "next_billed_at": None
            if status in ("canceled", "paused")
            else _at(started + timedelta(days=30)),
            "paused_at": _at(occurred_at) if status == "paused" else None,
            "canceled_at": _at(occurred_at) if status == "canceled" else None,
            "custom_data": {"user_id": str(user_id)} if user_id is not None else None,
            "collection_mode": "automatic",
            "billing_details": None,
            "current_billing_period": None
            if status in ("canceled", "paused")
            else {
                "starts_at": _at(started),
                "ends_at": _at(period_end or started + timedelta(days=30)),
            },
            "billing_cycle": {"interval": "month", "frequency": 1},
            "scheduled_change": scheduled_change,
            "discount": None,
            "items": [
                {
                    "status": "active",
                    "quantity": 1,
                    "recurring": True,
                    "price": {
                        "id": price,
                        "product_id": "pro_01gsz4t5hdjse780zja8vvr7jg",
                        "billing_cycle": {"interval": "month", "frequency": 1},
                        "unit_price": {"amount": "1400", "currency_code": "USD"},
                    },
                }
            ],
        },
    }


def payment_failed(
    occurred_at: datetime, *, sub: str | None = SUB, origin: str = "subscription_recurring"
) -> dict[str, Any]:
    """``transaction.payment_failed``: a renewal's card declined."""
    return {
        "event_id": f"evt_{next(_events):026d}",
        "event_type": "transaction.payment_failed",
        "occurred_at": _at(occurred_at),
        "notification_id": f"ntf_{next(_events):026d}",
        "data": {
            "id": f"txn_{next(_events):026d}",
            "status": "past_due" if sub else "ready",
            "customer_id": CUSTOMER,
            "subscription_id": sub,
            "origin": origin,
            "custom_data": None,
            "collection_mode": "automatic",
            "payments": [
                {
                    "status": "error",
                    "error_code": "declined",
                    "amount": "1400",
                    "method_details": {"type": "card", "card": {"type": "visa", "last4": "0002"}},
                }
            ],
        },
    }


async def deliver(
    client: AsyncClient,
    event: dict[str, Any],
    *,
    secret: str = SECRET,
    signed_at: int | None = None,
    body: bytes | None = None,
) -> httpx.Response:
    """Post ``event`` as Paddle does: the exact bytes it signed."""
    raw = body if body is not None else json.dumps(event).encode()
    return await client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "content-type": "application/json",
            "paddle-signature": sign(raw, secret, signed_at or int(time.time())),
        },
    )


async def accepted(client: AsyncClient, event: dict[str, Any]) -> dict[str, Any]:
    response = await deliver(client, event)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


async def plan_of(client: AsyncClient) -> str:
    response = await client.get("/v1/me")
    assert response.status_code == 200, response.text
    plan: str = response.json()["plan"]["id"]
    return plan


async def billing_of(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/v1/billing")
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


@pytest.fixture
async def ada(client: AsyncClient, db_session: AsyncSession) -> int:
    """Signed in, verified, and on Free."""
    user_id = await make_user(db_session)
    await sign_in(client)
    return user_id


def now() -> datetime:
    return datetime.now(UTC)


# --- the signature, first ----------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda raw: {"secret": "someone else's secret"}, id="wrong-secret"),
        pytest.param(lambda raw: {"signed_at": int(time.time()) - 60}, id="replayed"),
        pytest.param(lambda raw: {"body": raw.replace(b'"active"', b'"trialing"')}, id="altered"),
    ],
)
async def test_a_notification_paddle_did_not_sign_changes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    ada: int,
    tamper: Callable[[bytes], dict[str, Any]],
) -> None:
    event = subscription_event("subscription.created", now(), user_id=ada)
    raw = json.dumps(event).encode()
    signature = sign(raw, SECRET, int(time.time()))
    kwargs = tamper(raw)
    if "body" in kwargs:
        # Signed as sent, altered on the way.
        response = await client.post(
            WEBHOOK_PATH, content=kwargs["body"], headers={"paddle-signature": signature}
        )
    else:
        response = await deliver(client, event, **kwargs)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"
    assert await db_session.scalar(select(func.count()).select_from(PaddleEvent)) == 0
    assert await plan_of(client) == "free"


async def test_no_signature_is_refused(client: AsyncClient) -> None:
    response = await client.post(WEBHOOK_PATH, json={"event_id": "evt_01"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_signature"


async def test_with_billing_unconfigured_paddle_is_told_to_retry(
    app: FastAPI, client: AsyncClient
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"paddle_api_key": None, "paddle_webhook_secret": None}
    )
    response = await deliver(client, subscription_event("subscription.created", now()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_unavailable"


# --- checkout and the redirect -----------------------------------------------------------


async def test_a_checkout_link_carries_the_account_and_the_redirect_grants_nothing(
    client: AsyncClient, ada: int, paddle: FakePaddle
) -> None:
    response = await client.post("/v1/billing/checkout", json={"plan": "pro"}, headers=FRONTEND)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "url": "https://whalewatch.io/pay?_ptxn=txn_01hv8wptq8987qeep44cyrewp9",
        "transaction_id": "txn_01hv8wptq8987qeep44cyrewp9",
    }
    [request] = paddle.requests
    sent = json.loads(request.content)
    assert sent["items"] == [{"price_id": PRICES["pro"], "quantity": 1}]
    assert sent["custom_data"] == {"user_id": str(ada)}
    assert request.headers["authorization"] == "Bearer pdl_sdbx_apikey_01hv8_test"
    # Paying and coming back is not a subscription; only the webhook is.
    assert (await billing_of(client))["subscription"] is None
    assert await plan_of(client) == "free"


@pytest.mark.parametrize("plan", ["free", "pro_annual", "api_hobbyist"])
async def test_only_a_plan_for_sale_can_be_checked_out(
    client: AsyncClient, ada: int, paddle: FakePaddle, plan: str
) -> None:
    """Free costs nothing, pro_annual does not exist, and api_hobbyist has no
    price in PADDLE_PRICES here."""
    response = await client.post("/v1/billing/checkout", json={"plan": plan}, headers=FRONTEND)
    assert response.status_code == 422
    assert paddle.requests == []


async def test_an_unverified_account_cannot_check_out(
    client: AsyncClient, db_session: AsyncSession, paddle: FakePaddle
) -> None:
    await make_user(db_session, verified=False)
    await sign_in(client)
    response = await client.post("/v1/billing/checkout", json={"plan": "pro"}, headers=FRONTEND)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_unverified"


async def test_an_account_with_a_subscription_changes_plan_in_the_portal_instead(
    client: AsyncClient, db_session: AsyncSession, ada: int, paddle: FakePaddle
) -> None:
    await subscribe(db_session, ada, "pro", status="past_due")
    response = await client.post("/v1/billing/checkout", json={"plan": "api_pro"}, headers=FRONTEND)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_subscribed"
    assert paddle.requests == []


async def test_paddle_down_is_a_503_not_a_500(
    client: AsyncClient, ada: int, paddle: FakePaddle
) -> None:
    paddle.down = True
    response = await client.post("/v1/billing/checkout", json={"plan": "pro"}, headers=FRONTEND)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "billing_unavailable"


# --- grandfathering ---------------------------------------------------------------------


async def test_a_plan_change_ends_grandfathering_to_a_snapshot_of_the_old_plan(
    client: AsyncClient, db_session: AsyncSession, ada: int
) -> None:
    """A snapshot of Pro, kept after an upgrade, would win whole over API Pro:
    the user would pay for API Pro and get Pro. A renewal on Pro keeps it."""
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    admin_id = await db_session.scalar(
        insert(AdminUser)
        .values(email="ops@whalewatch.io", name="Grace Hopper", password_hash="x")
        .returning(AdminUser.id)
    )
    assert admin_id is not None
    _, snapshot = await admin_plans.grandfather(db_session, user_id=ada, admin_id=admin_id)

    await accepted(client, subscription_event("subscription.updated", t + timedelta(minutes=1)))
    row = await db_session.scalar(select(Subscription).where(Subscription.user_id == ada))
    assert row is not None
    await db_session.refresh(row)
    assert row.grandfathered_plan_id == snapshot.plan.id

    await accepted(
        client,
        subscription_event(
            "subscription.updated", t + timedelta(minutes=2), price=PRICES["api_pro"]
        ),
    )
    await db_session.refresh(row)
    assert row.grandfathered_plan_id is None
    assert await plan_of(client) == "api_pro"


# --- the lifecycle ----------------------------------------------------------------------


async def test_created_changed_cancelled_at_period_end_and_cancelled(
    client: AsyncClient, db_session: AsyncSession, ada: int, billing_outbox: Outbox
) -> None:
    t = now() - timedelta(minutes=10)
    assert await plan_of(client) == "free"  # the session now holds Free's grant

    assert (await accepted(client, subscription_event("subscription.created", t, user_id=ada)))[
        "outcome"
    ] == "applied"
    # The webhook had every session read its grant again: no five minutes' wait.
    assert await plan_of(client) == "pro"
    billing = await billing_of(client)
    assert billing["enabled"] is True
    assert billing["subscription"]["plan"] == {"id": "pro", "name": "Pro"}
    assert billing["subscription"]["status"] == "active"
    assert billing["subscription"]["grants_plan"] is True
    assert billing["subscription"]["manageable"] is True

    # Upgraded in the portal: the same subscription, another price.
    await accepted(
        client,
        subscription_event(
            "subscription.updated", t + timedelta(minutes=1), price=PRICES["api_pro"]
        ),
    )
    assert await plan_of(client) == "api_pro"

    # Cancelled in the portal: it holds to the end of the period paid for.
    period_end = now() + timedelta(days=20)
    await accepted(
        client,
        subscription_event(
            "subscription.updated",
            t + timedelta(minutes=2),
            price=PRICES["api_pro"],
            period_end=period_end,
            scheduled_change={
                "action": "cancel",
                "effective_at": _at(period_end),
                "resume_at": None,
            },
        ),
    )
    assert await plan_of(client) == "api_pro"
    cancel_at = (await billing_of(client))["subscription"]["cancel_at"]
    assert datetime.fromisoformat(cancel_at) == period_end

    await accepted(
        client,
        subscription_event(
            "subscription.canceled",
            t + timedelta(minutes=3),
            status="canceled",
            price=PRICES["api_pro"],
        ),
    )
    assert await plan_of(client) == "free"
    billing = await billing_of(client)
    assert billing["subscription"]["status"] == "canceled"
    assert billing["subscription"]["grants_plan"] is False
    assert billing_outbox.sent == []


async def test_paused_is_free_and_resumed_is_the_plan_again(client: AsyncClient, ada: int) -> None:
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    await accepted(
        client, subscription_event("subscription.paused", t + timedelta(minutes=1), status="paused")
    )
    assert await plan_of(client) == "free"
    await accepted(client, subscription_event("subscription.resumed", t + timedelta(minutes=2)))
    assert await plan_of(client) == "pro"


async def test_a_redelivered_event_is_recorded_once_and_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, ada: int
) -> None:
    created = subscription_event("subscription.created", now() - timedelta(minutes=5), user_id=ada)
    first = await accepted(client, created)
    again = await accepted(client, created)

    assert first == {"event_id": created["event_id"], "outcome": "applied", "duplicate": False}
    assert again["duplicate"] is True
    assert await db_session.scalar(select(func.count()).select_from(PaddleEvent)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_an_older_event_arriving_later_is_stale(
    client: AsyncClient, db_session: AsyncSession, ada: int
) -> None:
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    await accepted(
        client,
        subscription_event("subscription.canceled", t + timedelta(minutes=2), status="canceled"),
    )
    late = await accepted(
        client, subscription_event("subscription.updated", t + timedelta(minutes=1))
    )

    assert late["outcome"] == "stale"
    assert await plan_of(client) == "free"


async def test_a_subscription_with_no_account_of_ours_is_recorded_unmatched(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    answer = await accepted(client, subscription_event("subscription.created", now()))
    assert answer["outcome"] == "unmatched"
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_a_price_no_plan_is_sold_at_is_retried_until_it_is(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, ada: int
) -> None:
    created = subscription_event(
        "subscription.created", now() - timedelta(minutes=5), user_id=ada, price="pri_01new"
    )
    refused = await deliver(client, created)
    assert refused.status_code == 500
    # Rolled back, event and all, so Paddle's retry is not a duplicate.
    assert await db_session.scalar(select(func.count()).select_from(PaddleEvent)) == 0

    app.state.settings.paddle_prices["pro"] = "pri_01new"
    assert (await accepted(client, created))["outcome"] == "applied"
    assert await plan_of(client) == "pro"


async def test_a_second_live_subscription_is_refused_for_a_person_to_settle(
    client: AsyncClient, db_session: AsyncSession, ada: int
) -> None:
    await subscribe(db_session, ada, "pro")  # a comp, given by hand
    response = await deliver(client, subscription_event("subscription.created", now(), user_id=ada))
    assert response.status_code == 500
    assert await db_session.scalar(select(func.count()).select_from(PaddleEvent)) == 0


async def test_a_customer_who_paid_before_is_matched_without_custom_data(
    client: AsyncClient, ada: int
) -> None:
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    await accepted(
        client,
        subscription_event("subscription.canceled", t + timedelta(minutes=1), status="canceled"),
    )
    again = subscription_event("subscription.created", t + timedelta(minutes=2), sub="sub_02")
    assert (await accepted(client, again))["outcome"] == "applied"
    assert await plan_of(client) == "pro"


# --- the portal ---------------------------------------------------------------------------


async def test_the_portal_opens_for_whoever_has_paid(
    client: AsyncClient, ada: int, paddle: FakePaddle
) -> None:
    before = await client.post("/v1/billing/portal", headers=FRONTEND)
    assert before.status_code == 404

    await accepted(client, subscription_event("subscription.created", now(), user_id=ada))
    response = await client.post("/v1/billing/portal", headers=FRONTEND)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "url": f"https://customer-portal.paddle.com/{CUSTOMER}",
        "cancel_url": f"https://customer-portal.paddle.com/{SUB}/cancel",
        "update_payment_method_url": f"https://customer-portal.paddle.com/{SUB}/card",
    }
    assert paddle.requests[-1].url.path == f"/customers/{CUSTOMER}/portal-sessions"


# --- a failed renewal ----------------------------------------------------------------------


async def test_a_failed_renewal_is_dunned_then_free_then_recovered(
    client: AsyncClient,
    db_session: AsyncSession,
    ada: int,
    settings: Settings,
    billing_outbox: Outbox,
) -> None:
    """The whole of it: the payment fails eight days ago, with seven days' grace.

    Paddle sends the failed payment and the subscription turning past_due;
    one email. The dunning job reminds once and then says Free; the account
    is Free as soon as the grace ends, job or not. A retry that pays brings
    the plan back with an email.
    """
    failed_at = now() - timedelta(days=8)
    await accepted(
        client,
        subscription_event("subscription.created", failed_at - timedelta(days=30), user_id=ada),
    )
    await accepted(client, payment_failed(failed_at))
    await accepted(
        client,
        subscription_event(
            "subscription.past_due", failed_at + timedelta(seconds=1), status="past_due"
        ),
    )
    [failed] = billing_outbox.sent
    assert failed.to == "ada@example.com"
    assert failed.subject == "Your WhaleWatch payment didn't go through"
    assert "You keep Pro until" in failed.body

    row = await db_session.scalar(select(Subscription))
    assert row is not None and row.past_due_since is not None
    assert row.grace_ends_at == row.past_due_since + timedelta(days=settings.billing_grace_days)
    # Seven days from eight days ago: the grant ended yesterday, with no job run.
    assert await plan_of(client) == "free"
    billing = await billing_of(client)
    assert billing["subscription"]["status"] == "past_due"
    assert billing["subscription"]["grants_plan"] is False

    dunned = await run_dunning(db_session, settings, now())
    assert (dunned.reminded, dunned.downgraded) == (0, 1)
    [notice] = dunned.mail
    assert notice.subject == "Your WhaleWatch account is now on Free"
    assert (await run_dunning(db_session, settings, now())).mail == []

    # A retry Paddle makes with the card the user updated goes through.
    billing_outbox.sent.clear()
    await accepted(client, subscription_event("subscription.updated", now(), status="active"))
    [recovered] = billing_outbox.sent
    assert recovered.subject == "Your WhaleWatch payment went through"
    assert await plan_of(client) == "pro"
    dunning = await db_session.execute(
        select(
            Subscription.past_due_since, Subscription.grace_ends_at, Subscription.dunning_notified
        )
    )
    assert dunning.one() == (None, None, None)


async def test_inside_the_grace_the_plan_holds_and_the_reminder_goes_once(
    client: AsyncClient,
    db_session: AsyncSession,
    ada: int,
    settings: Settings,
    billing_outbox: Outbox,
) -> None:
    failed_at = now() - timedelta(days=6)
    await accepted(
        client,
        subscription_event("subscription.created", failed_at - timedelta(days=30), user_id=ada),
    )
    # Past due first, the failed payment after: one email whichever leads.
    await accepted(
        client, subscription_event("subscription.past_due", failed_at, status="past_due")
    )
    await accepted(client, payment_failed(failed_at + timedelta(seconds=1)))
    assert len(billing_outbox.sent) == 1
    assert await plan_of(client) == "pro"

    reminded = await run_dunning(db_session, settings, now())
    assert (reminded.reminded, reminded.downgraded) == (1, 0)
    assert reminded.mail[0].subject.startswith("Your WhaleWatch Pro plan ends on ")
    assert (await run_dunning(db_session, settings, now())).mail == []

    # Two days on, past the grace: the downgrade notice, once.
    later = await run_dunning(db_session, settings, now() + timedelta(days=2))
    assert (later.reminded, later.downgraded) == (0, 1)


async def test_paddle_giving_up_ends_the_dunning_quietly(
    client: AsyncClient,
    db_session: AsyncSession,
    ada: int,
    settings: Settings,
    billing_outbox: Outbox,
) -> None:
    failed_at = now() - timedelta(days=20)
    await accepted(
        client,
        subscription_event("subscription.created", failed_at - timedelta(days=30), user_id=ada),
    )
    await accepted(
        client, subscription_event("subscription.past_due", failed_at, status="past_due")
    )
    await run_dunning(db_session, settings, now())
    billing_outbox.sent.clear()

    await accepted(
        client,
        subscription_event(
            "subscription.canceled", now() - timedelta(minutes=1), status="canceled"
        ),
    )
    row = await db_session.scalar(select(Subscription))
    assert row is not None
    assert (row.status, row.grace_ends_at, row.dunning_notified) == ("canceled", None, None)
    assert billing_outbox.sent == []
    assert (await run_dunning(db_session, settings, now())).mail == []


async def test_a_failed_first_checkout_is_not_a_renewal(
    client: AsyncClient, ada: int, billing_outbox: Outbox
) -> None:
    answer = await accepted(client, payment_failed(now(), sub=None, origin="web"))
    assert answer["outcome"] == "ignored"
    assert billing_outbox.sent == []


async def test_a_card_that_fails_to_replace_a_good_one_starts_no_dunning(
    client: AsyncClient, db_session: AsyncSession, ada: int, billing_outbox: Outbox
) -> None:
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    answer = await accepted(
        client,
        payment_failed(t + timedelta(minutes=1), origin="subscription_payment_method_change"),
    )
    assert answer["outcome"] == "ignored"
    assert billing_outbox.sent == []
    row = await db_session.scalar(select(Subscription))
    assert row is not None and row.grace_ends_at is None


async def test_a_failure_older_than_a_recovery_starts_nothing(
    client: AsyncClient, db_session: AsyncSession, ada: int, billing_outbox: Outbox
) -> None:
    t = now() - timedelta(hours=2)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    await accepted(
        client,
        subscription_event("subscription.past_due", t + timedelta(minutes=1), status="past_due"),
    )
    await accepted(client, subscription_event("subscription.updated", t + timedelta(minutes=3)))
    billing_outbox.sent.clear()

    late = await accepted(client, payment_failed(t + timedelta(minutes=2)))
    assert late["outcome"] == "stale"
    assert billing_outbox.sent == []
    assert await plan_of(client) == "pro"


# --- deleting an account cancels its subscription ------------------------------------


async def delete_account(client: AsyncClient) -> None:
    response = await client.request(
        "DELETE", "/v1/account", json={"password": PASSWORD}, headers=FRONTEND
    )
    assert response.status_code == 204, response.text


def cancels(paddle: FakePaddle) -> list[str]:
    return [
        request.url.path
        for request in paddle.requests
        if request.method == "POST" and request.url.path.endswith("/cancel")
    ]


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", "paused"])
async def test_deleting_the_account_cancels_its_subscription_at_once(
    client: AsyncClient,
    db_session: AsyncSession,
    ada: int,
    paddle: FakePaddle,
    outbox: Outbox,
    status: str,
) -> None:
    t = now() - timedelta(minutes=10)
    await accepted(client, subscription_event("subscription.created", t, user_id=ada))
    if status != "active":
        event = f"subscription.{status}" if status != "trialing" else "subscription.updated"
        await accepted(client, subscription_event(event, t + timedelta(minutes=1), status=status))

    await delete_account(client)

    assert cancels(paddle) == [f"/subscriptions/{SUB}/cancel"]
    assert json.loads(paddle.requests[-1].content) == {"effective_from": "immediately"}
    row = await db_session.scalar(select(Subscription))
    assert row is not None
    assert (row.status, row.grace_ends_at) == ("canceled", None)
    [notice] = outbox.sent
    assert "Your Pro subscription is cancelled" in notice.body

    # Paddle's webhook follows, for an account that is gone: applied, quietly.
    after = await accepted(
        client, subscription_event("subscription.canceled", now(), status="canceled")
    )
    assert after["outcome"] == "applied"
    assert len(outbox.sent) == 1


async def test_deleting_an_account_that_never_paid_asks_paddle_nothing(
    client: AsyncClient, db_session: AsyncSession, ada: int, paddle: FakePaddle, outbox: Outbox
) -> None:
    await subscribe(db_session, ada, "pro")  # a comp: Paddle never heard of it
    await delete_account(client)
    assert paddle.requests == []
    assert "subscription" not in outbox.sent[0].body


async def test_with_paddle_down_the_deletion_stands_and_the_purge_waits_for_the_cancel(
    client: AsyncClient, db_session: AsyncSession, ada: int, paddle: FakePaddle, outbox: Outbox
) -> None:
    await accepted(
        client,
        subscription_event("subscription.created", now() - timedelta(minutes=5), user_id=ada),
    )
    paddle.down = True
    await delete_account(client)

    # Deleted, but still billing: no promise in the email, and no purge.
    assert await db_session.scalar(select(func.count()).select_from(Subscription)) == 1
    assert "subscription" not in outbox.sent[0].body
    past_grace = now() + timedelta(days=31)
    assert await privacy.purge_due(db_session, at=past_grace) == []

    # The hourly billing job's retry, with Paddle back.
    paddle.down = False
    async with httpx.AsyncClient(
        base_url="https://sandbox-api.paddle.com", transport=httpx.MockTransport(paddle.handle)
    ) as http:
        retried = await cancel_for_deleted(db_session, PaddleClient(http))
    assert (retried.plans, retried.failed) == (["Pro"], 0)

    [purged] = await privacy.purge_due(db_session, at=past_grace)
    assert purged.user_id == ada
    assert purged.erased["subscription"] == 1


async def test_a_live_account_s_subscription_is_never_cancelled_by_the_retry(
    client: AsyncClient, db_session: AsyncSession, ada: int, paddle: FakePaddle
) -> None:
    await accepted(client, subscription_event("subscription.created", now(), user_id=ada))
    async with httpx.AsyncClient(
        base_url="https://sandbox-api.paddle.com", transport=httpx.MockTransport(paddle.handle)
    ) as http:
        retried = await cancel_for_deleted(db_session, PaddleClient(http))
    assert (retried.plans, retried.failed) == ([], 0)
    assert cancels(paddle) == []
