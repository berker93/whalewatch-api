"""TIER-6, without a database: Paddle's signature, its notifications, its API, and the settings.

The API calls go to an ``httpx.MockTransport`` answering as sandbox-api.paddle.com
does, in the shapes its reference documents.
"""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.accounts.paddle import (
    API_URLS,
    PaddleClient,
    PaddleError,
    SignatureError,
    parse_notification,
    sign,
    verify_signature,
)
from tests.conftest import make_settings

SECRET = "pdl_ntfset_01_test_secret"
BODY = b'{"event_id":"evt_01","event_type":"subscription.created"}'
NOW = 1_800_000_000


# --- the signature -------------------------------------------------------------------


def test_paddles_own_example_header_shape_verifies() -> None:
    verify_signature(sign(BODY, SECRET, NOW), BODY, SECRET, now=NOW, tolerance=5)


@pytest.mark.parametrize(
    ("header", "body", "secret", "why"),
    [
        (None, BODY, SECRET, "no Paddle-Signature"),
        ("", BODY, SECRET, "no Paddle-Signature"),
        (f"h1={'0' * 64}", BODY, SECRET, "needs ts and h1"),
        (f"ts={NOW}", BODY, SECRET, "needs ts and h1"),
        (f"ts=soon;h1={'0' * 64}", BODY, SECRET, "not a number"),
        (sign(BODY, "another secret", NOW), BODY, SECRET, "no h1 matches"),
        # One byte more: whitespace a framework might add is a different body.
        (sign(BODY, SECRET, NOW), BODY + b" ", SECRET, "no h1 matches"),
        (sign(BODY, SECRET, NOW - 6), BODY, SECRET, "more than 5s"),
    ],
)
def test_anything_but_a_fresh_signature_of_these_bytes_is_refused(
    header: str | None, body: bytes, secret: str, why: str
) -> None:
    with pytest.raises(SignatureError, match=why):
        verify_signature(header, body, secret, now=NOW, tolerance=5)


def test_a_signature_from_up_to_the_tolerance_ago_is_accepted() -> None:
    verify_signature(sign(BODY, SECRET, NOW - 5), BODY, SECRET, now=NOW, tolerance=5)


def test_while_paddle_rotates_the_secret_either_h1_will_do() -> None:
    old = sign(BODY, "the old secret", NOW).partition("h1=")[2]
    new = sign(BODY, SECRET, NOW).partition("h1=")[2]
    verify_signature(f"ts={NOW};h1={old};h1={new}", BODY, SECRET, now=NOW, tolerance=5)


# --- notifications -------------------------------------------------------------------


def test_a_notification_is_read_with_an_aware_time() -> None:
    event = parse_notification(
        json.dumps(
            {
                "event_id": "evt_01hv8ytwcg91n07pa4jmvsdcst",
                "event_type": "subscription.updated",
                "occurred_at": "2024-04-12T10:49:43.056742Z",
                "notification_id": "ntf_01hv8ytwevhn1sdtwzhf4rz548",
                "data": {"id": "sub_01hv8x29kz0t586xy6zn1a62ny"},
            }
        ).encode()
    )
    assert event.event_type == "subscription.updated"
    assert event.entity_id == "sub_01hv8x29kz0t586xy6zn1a62ny"
    assert event.occurred_at.utcoffset() is not None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"event_type": "x", "occurred_at": "2024-04-12T10:49:43Z", "data": {}}',
        b'{"event_id": "e", "event_type": "x", "occurred_at": "2024-04-12T10:49:43", "data": {}}',
        b'{"event_id": "e", "event_type": "x", "occurred_at": null, "data": {}}',
        b'{"event_id": "e", "event_type": "x", "occurred_at": "2024-04-12T10:49:43Z", "data": []}',
    ],
)
def test_a_body_that_is_not_a_notification_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError, match="not a Paddle notification"):
        parse_notification(body)


# --- the API -------------------------------------------------------------------------


def _client(
    answer: Any, status: int = 200, seen: list[httpx.Request] | None = None
) -> PaddleClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=answer)

    http = httpx.AsyncClient(base_url=API_URLS["sandbox"], transport=httpx.MockTransport(handle))
    return PaddleClient(http)


async def test_a_checkout_carries_the_users_id_and_answers_its_link() -> None:
    seen: list[httpx.Request] = []
    paddle = _client(
        {
            "data": {
                "id": "txn_01",
                "status": "draft",
                "checkout": {"url": "https://whalewatch.io/pay?_ptxn=txn_01"},
            }
        },
        seen=seen,
    )
    checkout = await paddle.create_checkout(price_id="pri_pro", user_id=42, customer_id=None)

    assert checkout.url == "https://whalewatch.io/pay?_ptxn=txn_01"
    assert checkout.transaction_id == "txn_01"
    [request] = seen
    assert request.url.path == "/transactions"
    assert json.loads(request.content) == {
        "items": [{"price_id": "pri_pro", "quantity": 1}],
        "custom_data": {"user_id": "42"},
        "collection_mode": "automatic",
    }


async def test_a_checkout_for_someone_who_paid_before_reuses_their_customer() -> None:
    seen: list[httpx.Request] = []
    paddle = _client({"data": {"id": "txn_02", "checkout": {"url": "https://x/pay"}}}, seen=seen)
    await paddle.create_checkout(price_id="pri_pro", user_id=42, customer_id="ctm_01")
    assert json.loads(seen[0].content)["customer_id"] == "ctm_01"


async def test_a_transaction_without_a_checkout_link_says_what_is_missing() -> None:
    paddle = _client({"data": {"id": "txn_03", "checkout": {"url": None}}})
    with pytest.raises(PaddleError, match="default payment link"):
        await paddle.create_checkout(price_id="pri_pro", user_id=42, customer_id=None)


async def test_a_refusal_carries_paddles_code() -> None:
    paddle = _client(
        {"error": {"type": "request_error", "code": "not_found", "detail": "Price not found"}},
        status=404,
    )
    with pytest.raises(PaddleError, match="Price not found") as caught:
        await paddle.create_checkout(price_id="pri_gone", user_id=42, customer_id=None)
    assert (caught.value.status, caught.value.code) == (404, "not_found")


async def test_paddle_unreachable_is_a_paddle_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    paddle = PaddleClient(
        httpx.AsyncClient(base_url=API_URLS["sandbox"], transport=httpx.MockTransport(down))
    )
    with pytest.raises(PaddleError, match="could not be reached"):
        await paddle.create_checkout(price_id="pri_pro", user_id=42, customer_id=None)


async def test_a_portal_session_links_straight_to_the_subscription() -> None:
    seen: list[httpx.Request] = []
    paddle = _client(
        {
            "data": {
                "id": "cpls_01",
                "customer_id": "ctm_01",
                "urls": {
                    "general": {"overview": "https://customer-portal.paddle.com/cpl_01?token=a"},
                    "subscriptions": [
                        {
                            "id": "sub_01",
                            "cancel_subscription": "https://customer-portal.paddle.com/c",
                            "update_subscription_payment_method": "https://customer-portal.paddle.com/u",
                        }
                    ],
                },
            }
        },
        seen=seen,
    )
    portal = await paddle.create_portal_session(customer_id="ctm_01", subscription_id="sub_01")

    assert portal.url == "https://customer-portal.paddle.com/cpl_01?token=a"
    assert portal.cancel_url == "https://customer-portal.paddle.com/c"
    assert portal.update_payment_method_url == "https://customer-portal.paddle.com/u"
    assert seen[0].url.path == "/customers/ctm_01/portal-sessions"
    assert json.loads(seen[0].content) == {"subscription_ids": ["sub_01"]}


# --- settings ------------------------------------------------------------------------


def test_prices_are_read_as_plan_equals_price() -> None:
    settings = make_settings(paddle_prices=" pro=pri_01 , api_pro=pri_02,")
    assert settings.paddle_prices == {"pro": "pri_01", "api_pro": "pri_02"}


@pytest.mark.parametrize("prices", ["pro", "pro=", "=pri_01", "pro=pri_01,api_pro=pri_01"])
def test_prices_that_would_make_a_webhook_guess_are_refused(prices: str) -> None:
    with pytest.raises(ValidationError, match="PADDLE_PRICES"):
        make_settings(paddle_prices=prices)


def test_billing_is_off_until_both_the_key_and_the_secret_are_set() -> None:
    assert not make_settings().billing_enabled
    assert not make_settings(paddle_api_key="", paddle_webhook_secret="").billing_enabled
    with pytest.raises(ValidationError, match="set together"):
        make_settings(paddle_api_key=SecretStr("pdl_sdbx_apikey_01"))
    both = make_settings(paddle_api_key="pdl_sdbx_apikey_01", paddle_webhook_secret=SECRET)
    assert both.billing_enabled


def test_production_refuses_the_sandbox_but_not_billing_being_off() -> None:
    make_settings(environment="production")
    with pytest.raises(ValidationError, match="PADDLE_ENVIRONMENT=sandbox"):
        make_settings(
            environment="production",
            paddle_api_key="pdl_sdbx_apikey_01",
            paddle_webhook_secret=SECRET,
        )


def test_the_reminder_comes_inside_the_grace() -> None:
    with pytest.raises(ValidationError, match="BILLING_REMINDER_DAYS"):
        make_settings(billing_grace_days=1, billing_reminder_days=2)


# --- cancelling ---------------------------------------------------------------------


def _paddle(*answers: tuple[int, Any], seen: list[httpx.Request]) -> PaddleClient:
    queue = list(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return PaddleClient(
        httpx.AsyncClient(base_url=API_URLS["sandbox"], transport=httpx.MockTransport(handle))
    )


async def test_a_cancellation_is_immediate() -> None:
    seen: list[httpx.Request] = []
    paddle = _paddle((200, {"data": {"id": "sub_01", "status": "canceled"}}), seen=seen)

    assert (await paddle.cancel_subscription("sub_01"))["status"] == "canceled"
    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/subscriptions/sub_01/cancel")
    assert json.loads(request.content) == {"effective_from": "immediately"}


async def test_cancelling_one_already_cancelled_is_done_not_refused() -> None:
    seen: list[httpx.Request] = []
    paddle = _paddle(
        (400, {"error": {"code": "subscription_locked_canceled", "detail": "is canceled"}}),
        (200, {"data": {"id": "sub_01", "status": "canceled"}}),
        seen=seen,
    )
    assert (await paddle.cancel_subscription("sub_01"))["status"] == "canceled"
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/subscriptions/sub_01/cancel"),
        ("GET", "/subscriptions/sub_01"),
    ]


async def test_a_refused_cancellation_of_one_still_billing_is_an_error() -> None:
    paddle = _paddle(
        (400, {"error": {"code": "bad_request", "detail": "no"}}),
        (200, {"data": {"id": "sub_01", "status": "active"}}),
        seen=[],
    )
    with pytest.raises(PaddleError, match="no"):
        await paddle.cancel_subscription("sub_01")


async def test_paddle_failing_to_cancel_is_an_error_without_asking_again() -> None:
    seen: list[httpx.Request] = []
    paddle = _paddle((503, {"error": {"code": "service_unavailable"}}), seen=seen)
    with pytest.raises(PaddleError):
        await paddle.cancel_subscription("sub_01")
    assert len(seen) == 1
