"""Paddle Billing: its webhook signature, its notifications, and the two calls this API makes.

Paddle is the merchant of record. It sells the plan, charges the card, retries
a card that fails, and tells this API what happened by webhook;
:mod:`app.accounts.billing` turns that into subscription rows. This module is
the wire: no database, nothing about plans.

The signature
-------------
Every webhook carries ``Paddle-Signature: ts=<unix seconds>;h1=<hex>``, where
``h1`` is HMAC-SHA256 of ``<ts>:<the raw body>`` under the notification
destination's secret key. There may be more than one ``h1`` while Paddle
rotates the secret; one matching is enough. A ``ts`` more than the tolerance
in the past is refused, so a captured request cannot be replayed later;
Paddle signs each redelivery afresh, so a refused one comes back with a new
timestamp. :func:`verify_signature` is checked against the bytes as received,
before anything parses them.

The calls
---------
:meth:`PaddleClient.create_checkout` makes a transaction for one plan's price
with the user's id in ``custom_data``, and answers its checkout link: Paddle
copies ``custom_data`` onto the subscription the checkout creates, which is
how its webhooks name the account. The link opens the account's default
payment link (Paddle > Checkout > Checkout settings), which must be set and
approved. :meth:`PaddleClient.create_portal_session` opens the customer
portal, where the customer changes plan, updates their card, or cancels.
:meth:`PaddleClient.cancel_subscription` ends a subscription at once, for an
account that has been deleted.
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

API_URLS: Final = {
    "sandbox": "https://sandbox-api.paddle.com",
    "production": "https://api.paddle.com",
}

#: The API version every request asks for, so a change of the account's
#: default does not change what this code reads.
API_VERSION: Final = "1"

#: Long enough for Paddle on a bad day, short enough that a checkout button
#: does not hang.
TIMEOUT_SECONDS: Final = 10.0


# --- the signature -------------------------------------------------------------------


class SignatureError(Exception):
    """The webhook is not Paddle's: no signature, a malformed one, a wrong one, or an old one."""


def sign(body: bytes, secret: str, ts: int) -> str:
    """The ``Paddle-Signature`` Paddle would send for ``body`` at ``ts``: for tests and tools."""
    digest = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def verify_signature(
    header: str | None, body: bytes, secret: str, *, now: float, tolerance: int
) -> None:
    """Return if ``header`` signs ``body`` under ``secret``, within ``tolerance``
    seconds before ``now``.

    :raises SignatureError: saying why not.
    """
    if not header:
        raise SignatureError("no Paddle-Signature header")
    ts: int | None = None
    signatures: list[str] = []
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "ts":
            try:
                ts = int(value)
            except ValueError:
                raise SignatureError("ts is not a number") from None
        elif name == "h1" and value:
            signatures.append(value)
    if ts is None or not signatures:
        raise SignatureError("Paddle-Signature needs ts and h1")
    if now > ts + tolerance:
        raise SignatureError(f"signed {int(now - ts)}s ago, more than {tolerance}s")
    expected = sign(body, secret, ts).partition("h1=")[2]
    # compare_digest on each, never ==: a comparison that stops at the first
    # wrong character says how many were right.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("no h1 matches")


# --- notifications -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notification:
    """One webhook event: what happened, when, and the entity as it is now."""

    event_id: str
    event_type: str
    occurred_at: datetime
    data: Mapping[str, Any]

    @property
    def entity_id(self) -> str | None:
        entity = self.data.get("id")
        return entity if isinstance(entity, str) else None


def parse_notification(body: bytes) -> Notification:
    """The event in a verified body.

    :raises ValueError: when it is not one: Paddle's fault, not a retry's to fix.
    """
    try:
        payload = json.loads(body)
        event_id, event_type = payload["event_id"], payload["event_type"]
        occurred_at = parse_time(payload["occurred_at"])
        data = payload["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"not a Paddle notification: {exc!r}") from exc
    if not (isinstance(event_id, str) and isinstance(event_type, str) and isinstance(data, dict)):
        raise ValueError("not a Paddle notification: event_id, event_type or data has a wrong type")
    if occurred_at is None:
        raise ValueError("not a Paddle notification: no occurred_at")
    return Notification(event_id, event_type, occurred_at, data)


def parse_time(value: object) -> datetime | None:
    """Paddle's RFC 3339 timestamp, aware; None for null.

    :raises ValueError: for anything else.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a timestamp")
    # Paddle sends microseconds and a Z; fromisoformat reads both since 3.11.
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no timezone")
    return parsed


# --- the API -------------------------------------------------------------------------


class PaddleError(Exception):
    """Paddle could not be reached, or refused the call."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class Checkout:
    transaction_id: str
    url: str


@dataclass(frozen=True, slots=True)
class PortalSession:
    url: str
    """The portal's overview: the customer's subscriptions, payments and invoices."""
    cancel_url: str | None
    """Straight to cancelling the subscription asked about."""
    update_payment_method_url: str | None
    """Straight to replacing its card."""


class PaddleClient:
    """Paddle's API, with the account's key."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", path, body)

    async def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise PaddleError(f"Paddle could not be reached: {exc!r}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_error:
            error = payload.get("error") or {}
            raise PaddleError(
                f"Paddle refused {method} {path}: {error.get('detail') or response.reason_phrase}",
                status=response.status_code,
                code=error.get("code"),
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PaddleError(f"Paddle answered {method} {path} without data")
        return data

    async def create_checkout(
        self, *, price_id: str, user_id: int, customer_id: str | None
    ) -> Checkout:
        """A transaction for one of ``price_id``, carrying ``user_id``, and its
        checkout link. ``customer_id`` when the user has paid before, so Paddle
        keeps one customer for them."""
        body: dict[str, Any] = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "custom_data": {"user_id": str(user_id)},
            "collection_mode": "automatic",
        }
        if customer_id:
            body["customer_id"] = customer_id
        data = await self._post("/transactions", body)
        url = (data.get("checkout") or {}).get("url")
        if not url:
            raise PaddleError(
                "Paddle made the transaction but no checkout link: is a default "
                "payment link set and approved?"
            )
        return Checkout(transaction_id=data["id"], url=url)

    async def create_portal_session(
        self, *, customer_id: str, subscription_id: str | None
    ) -> PortalSession:
        """A customer portal session for ``customer_id``, with links straight to
        ``subscription_id``'s cancel and card pages when given."""
        body: dict[str, Any] = {"subscription_ids": [subscription_id] if subscription_id else []}
        data = await self._post(f"/customers/{customer_id}/portal-sessions", body)
        urls = data.get("urls") or {}
        overview = (urls.get("general") or {}).get("overview")
        if not overview:
            raise PaddleError("Paddle opened a portal session with no link")
        deep: dict[str, Any] = next(
            (s for s in urls.get("subscriptions") or [] if s.get("id") == subscription_id), {}
        )
        return PortalSession(
            url=overview,
            cancel_url=deep.get("cancel_subscription"),
            update_payment_method_url=deep.get("update_subscription_payment_method"),
        )

    async def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Cancel ``subscription_id`` at once, and answer it as it is now.

        Immediately, never at the period's end: a paused subscription can only
        be cancelled so, and a past-due one would go on retrying the card until
        then. One Paddle has already cancelled is answered as it is, not
        refused: the call is safe to repeat.
        """
        try:
            return await self._post(
                f"/subscriptions/{subscription_id}/cancel", {"effective_from": "immediately"}
            )
        except PaddleError as exc:
            if exc.status is None or exc.status >= 500:
                raise
            # Refused: already cancelled, by the customer or by an earlier try
            # whose answer was lost? Then it is done; anything else is not.
            current = await self._call("GET", f"/subscriptions/{subscription_id}", None)
            if current.get("status") != "canceled":
                raise
            return current


def http_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """An HTTP client for Paddle's API in ``settings``' environment, with its key."""
    assert settings.paddle_api_key is not None  # callers check billing_enabled
    return httpx.AsyncClient(
        base_url=API_URLS[settings.paddle_environment],
        headers={
            "authorization": f"Bearer {settings.paddle_api_key.get_secret_value()}",
            "paddle-version": API_VERSION,
        },
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )
