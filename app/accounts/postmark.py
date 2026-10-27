"""Postmark: sending, and reading its webhooks.

Sending
-------
``POST /email`` with the server's token, one message at a time, on the
message stream its :class:`~app.db.models.Stream` names
(``POSTMARK_TRANSACTIONAL_STREAM``, ``POSTMARK_BROADCAST_STREAM``). Postmark
keeps a reputation and a suppression list per stream, which is what keeps a
digest's complaints away from password resets. Both bodies go with every
message, open and link tracking are off (a tracking pixel is a privacy cost
for no product use, and rewritten links break the unsubscribe), and the
``email_log`` id goes as metadata, so each webhook names its row.

A broadcast message carries ``List-Unsubscribe`` and
``List-Unsubscribe-Post: List-Unsubscribe=One-Click`` (RFC 8058). The broadcast
stream's own unsubscribe handling must be set to *None* in Postmark: this API
handles it, per list, which Postmark's one switch per address cannot.

Postmark has no idempotency key, so a send is retried only when it cannot
have arrived: no connection, or a 429 or 5xx answer. A send that timed out
waiting for the answer is not retried. It is logged ``failed``, and the
webhook saying it was delivered puts the row right, if it was.

Webhooks
--------
Delivery, Bounce, SpamComplaint and SubscriptionChange, all to one URL with
HTTP basic auth (Postmark signs nothing): ``https://postmark:<secret>@
api.whalewatch.com/webhooks/postmark``. :func:`parse_event` reads one into an
:class:`Event`; :func:`app.accounts.delivery.record_event` applies it.
"""

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import httpx

from app.accounts.mail import Message
from app.accounts.mailer import InactiveRecipientError, MailError
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Stream, SuppressionReason

logger = get_logger(__name__)

API_URL: Final = "https://api.postmarkapp.com"
TIMEOUT_SECONDS: Final = 10.0

#: Postmark's error code for a recipient it has deactivated on the stream.
INACTIVE_RECIPIENT: Final = 406

#: The waits before the second and third tries of a send that cannot have arrived.
RETRY_DELAYS: Final = (0.5, 2.0)

#: The basic-auth user in the webhook URL. The password is POSTMARK_WEBHOOK_SECRET.
WEBHOOK_USER: Final = "postmark"


def stream_id(settings: Settings, stream: Stream) -> str:
    """The Postmark message stream ``stream`` is sent on."""
    if stream is Stream.TRANSACTIONAL:
        return settings.postmark_transactional_stream
    return settings.postmark_broadcast_stream


def _payload(settings: Settings, message: Message, reference: str) -> dict[str, Any]:
    headers = []
    if message.unsubscribe is not None:
        headers = [
            {"Name": "List-Unsubscribe", "Value": f"<{message.unsubscribe.one_click_url}>"},
            {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"},
        ]
    return {
        "From": settings.email_from,
        "To": message.to,
        "Subject": message.subject,
        "TextBody": message.body,
        "HtmlBody": message.html,
        "MessageStream": stream_id(settings, message.stream),
        "Tag": message.kind.value,
        "Metadata": {"email_log_id": reference},
        "Headers": headers,
        "TrackOpens": False,
        "TrackLinks": "None",
    }


def _retryable(response: httpx.Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


class PostmarkMailer:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        assert settings.postmark_server_token is not None  # Settings refuses postmark without it
        self._settings = settings
        self._token = settings.postmark_server_token.get_secret_value()
        self._transport = transport

    async def send(self, message: Message, *, reference: str) -> str | None:
        payload = _payload(self._settings, message, reference)
        async with httpx.AsyncClient(
            base_url=API_URL,
            headers={"x-postmark-server-token": self._token, "accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=self._transport,
        ) as http:
            for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
                try:
                    response = await http.post("/email", json=payload)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    if delay is None:
                        raise MailError(f"Postmark could not be reached: {exc!r}") from exc
                except httpx.HTTPError as exc:
                    raise MailError(f"no answer from Postmark, so maybe sent: {exc!r}") from exc
                else:
                    if not (_retryable(response) and delay is not None):
                        return self._accepted(response)
                logger.warning("postmark.retrying", attempt=attempt, reference=reference)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")

    @staticmethod
    def _accepted(response: httpx.Response) -> str:
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        code = answer.get("ErrorCode") if isinstance(answer, dict) else None
        said = answer.get("Message") if isinstance(answer, dict) else None
        if response.status_code == 200 and code == 0 and answer.get("MessageID"):
            return str(answer["MessageID"])
        if code == INACTIVE_RECIPIENT:
            raise InactiveRecipientError(f"Postmark: {said}")
        raise MailError(f"Postmark refused it: HTTP {response.status_code}, {code}: {said}")


# --- webhooks -------------------------------------------------------------------------


class EventKind(StrEnum):
    DELIVERED = "delivered"
    SOFT_BOUNCE = "soft_bounce"
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    SUPPRESSED = "suppressed"
    """Postmark stopped sending to the address on a stream: SubscriptionChange."""
    REACTIVATED = "reactivated"
    """Postmark sends to the address on a stream again: someone reactivated it."""
    IGNORED = "ignored"
    """An open, a click, an inbound message: nothing this API records."""


#: Bounce types that say the address will never take mail. Postmark also
#: marks such a bounce ``Inactive``; either is enough.
_HARD_BOUNCES: Final = frozenset({"HardBounce", "BadEmailAddress"})

_SUPPRESSION_REASONS: Final = {
    "HardBounce": SuppressionReason.HARD_BOUNCE,
    "SpamComplaint": SuppressionReason.COMPLAINT,
}


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    record_type: str
    recipient: str | None = None
    message_id: str | None = None
    reference: int | None = None
    """The ``email_log`` id from the message's metadata."""
    stream: Stream | None = None
    """Which of ours the message stream is; None for one this API does not send on."""
    detail: str | None = None
    reason: SuppressionReason | None = None
    """For a SubscriptionChange that suppresses: why."""


def _stream(settings: Settings, value: object) -> Stream | None:
    if value == settings.postmark_transactional_stream:
        return Stream.TRANSACTIONAL
    if value == settings.postmark_broadcast_stream:
        return Stream.BROADCAST
    return None


def _reference(metadata: object) -> int | None:
    if isinstance(metadata, dict):
        value = metadata.get("email_log_id")
        if isinstance(value, str) and value.isdigit() and len(value) <= 18:
            return int(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_event(settings: Settings, body: bytes) -> Event:
    """:raises ValueError: Not a Postmark webhook's JSON."""
    try:
        record = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from None
    if not isinstance(record, dict) or not isinstance(record.get("RecordType"), str):
        raise ValueError("no RecordType")
    record_type: str = record["RecordType"]
    common: dict[str, Any] = {
        "record_type": record_type,
        "message_id": _text(record.get("MessageID")),
        "reference": _reference(record.get("Metadata")),
        "stream": _stream(settings, record.get("MessageStream")),
    }
    bounce_type = record.get("Type")
    if record_type == "Delivery":
        return Event(
            EventKind.DELIVERED,
            recipient=_text(record.get("Recipient")),
            detail=_text(record.get("Details")),
            **common,
        )
    if record_type == "SpamComplaint" or bounce_type == "SpamComplaint":
        return Event(EventKind.COMPLAINT, recipient=_text(record.get("Email")), **common)
    if record_type == "Bounce":
        hard = bounce_type in _HARD_BOUNCES or record.get("Inactive") is True
        description = _text(record.get("Description")) or _text(bounce_type)
        return Event(
            EventKind.HARD_BOUNCE if hard else EventKind.SOFT_BOUNCE,
            recipient=_text(record.get("Email")),
            detail=f"{bounce_type}: {description}" if bounce_type else description,
            **common,
        )
    if record_type == "SubscriptionChange":
        suppress = record.get("SuppressSending")
        if not isinstance(suppress, bool):
            raise ValueError("a SubscriptionChange without SuppressSending")
        said = _text(record.get("SuppressionReason"))
        reason = _SUPPRESSION_REASONS.get(said or "", SuppressionReason.PROVIDER)
        return Event(
            EventKind.SUPPRESSED if suppress else EventKind.REACTIVATED,
            recipient=_text(record.get("Recipient")),
            detail=said,
            reason=reason if suppress else None,
            **common,
        )
    return Event(EventKind.IGNORED, **common)
