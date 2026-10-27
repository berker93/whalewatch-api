"""Email without a database: the two bodies, the unsubscribe tokens, Postmark's
side of a send and of a webhook, and the sending domain's DNS check."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import SecretStr

from app.accounts import mail, postmark
from app.accounts.email_domain import Check, check_domain
from app.accounts.mailer import InactiveRecipientError, MailError
from app.accounts.postmark import EventKind, PostmarkMailer, parse_event
from app.accounts.unsubscribe import (
    InvalidTokenError,
    ListKind,
    MailingList,
    alert_label,
    read_token,
    token_for,
)
from app.core.config import Settings
from app.db.models import AlertKind, EmailKind, Stream, SuppressionReason
from tests.conftest import make_settings


@pytest.fixture
def settings() -> Settings:
    return make_settings(
        web_url="https://app.whalewatch.io",
        api_url="https://api.whalewatch.io",
        email_backend="postmark",
        postmark_server_token=SecretStr("server-token"),
        postmark_webhook_secret=SecretStr("webhook-secret"),
        email_unsubscribe_secret=SecretStr("unsubscribe-secret"),
    )


def digest(settings: Settings, **payload: str) -> mail.Message:
    return mail.alert(
        settings,
        to="ada@example.com",
        user_id=7,
        rule_id=42,
        kind=AlertKind.SEASON_DIGEST,
        title="The 2026Q3 season so far",
        body="61 of 100 filers are in.",
        payload={"period": "2026Q3", **payload},
    )


# --- the two bodies ------------------------------------------------------------------


def every_account_message(settings: Settings) -> list[mail.Message]:
    to, user_id = "ada@example.com", 7
    return [
        mail.email_verification(settings, to=to, user_id=user_id, token="t", hours=24),
        mail.registration_attempt(settings, to=to, user_id=user_id),
        mail.email_change_confirmation(settings, to=to, user_id=user_id, token="t", hours=24),
        mail.email_change_notice(settings, to=to, user_id=user_id, new_email="b@x.io"),
        mail.password_reset(settings, to=to, user_id=user_id, token="t", minutes=60),
        mail.account_deleted(settings, to=to, user_id=user_id, purge_after=date(2026, 11, 26)),
        mail.quota_warning(
            settings,
            to=to,
            user_id=user_id,
            what="API calls",
            used=8,
            limit=10,
            resets_on=date(2026, 11, 1),
        ),
    ]


def test_every_account_message_has_both_bodies_and_no_unsubscribe(settings: Settings) -> None:
    for message in every_account_message(settings):
        assert message.stream is Stream.TRANSACTIONAL, message.kind
        assert message.unsubscribe is None
        assert message.html.startswith("<!DOCTYPE html>")
        assert "unsubscribe" not in message.body.lower()
        assert "unsubscribe" not in message.html.lower()
        assert "sent whatever your email settings say" in message.body


def test_a_link_is_a_button_in_html_and_a_line_in_text(settings: Settings) -> None:
    message = mail.password_reset(settings, to="a@x.io", user_id=1, token="tok", minutes=60)
    url = "https://app.whalewatch.io/reset-password?token=tok"

    assert f"Choose a new password: {url}\n" in message.body
    assert f'href="{url}"' in message.html
    assert ">Choose a new password</a>" in message.html


def test_what_goes_into_the_html_is_text_never_markup(settings: Settings) -> None:
    message = mail.email_change_notice(settings, to="a@x.io", user_id=1, new_email="<script>@x.io")

    assert "<script>" not in message.html
    assert "&lt;script&gt;@x.io" in message.html
    assert "<script>@x.io" in message.body


def test_an_alert_is_broadcast_with_its_list_in_the_footer_and_the_header(
    settings: Settings,
) -> None:
    message = digest(settings)
    assert message.kind is EmailKind.ALERT
    assert message.stream is Stream.BROADCAST
    assert message.unsubscribe is not None
    query = urlencode({"token": token_for(settings, 7, MailingList.for_rule(42))})

    assert message.unsubscribe.page_url == f"https://app.whalewatch.io/unsubscribe?{query}"
    assert message.unsubscribe.one_click_url == (
        f"https://api.whalewatch.io/v1/email/unsubscribe?{query}"
    )
    assert f"Unsubscribe from these emails: {message.unsubscribe.page_url}" in message.body
    assert f'href="{message.unsubscribe.page_url}"' in message.html
    assert "you asked to be emailed about the 13F season digest" in message.body
    assert "Open in WhaleWatch: https://app.whalewatch.io/overview?period=2026Q3" in message.body


def test_a_message_and_its_stream_agree_on_an_unsubscribe(settings: Settings) -> None:
    reset = mail.password_reset(settings, to="a@x.io", user_id=1, token="t", minutes=60)
    alert = digest(settings)

    with pytest.raises(ValueError, match="unsubscribe"):
        replace(reset, unsubscribe=alert.unsubscribe)
    with pytest.raises(ValueError, match="unsubscribe"):
        replace(alert, unsubscribe=None)


@pytest.mark.parametrize(
    ("kind", "payload", "path"),
    [
        (AlertKind.FILER_FILED, {"filer_slug": "berkshire", "period": "2026Q3"},
         "/investors/berkshire?period=2026Q3"),
        (AlertKind.FILER_POSITION_CHANGE, {"filer_slug": None}, None),
        (AlertKind.STOCK_OWNERSHIP_CHANGE, {"ticker": "BRK.B", "period": None}, "/stocks/BRK.B"),
        (AlertKind.INSIDER_CLUSTER, {"ticker": "AAPL", "period": "2026Q3"},
         "/insiders?ticker=AAPL"),
        (AlertKind.INSIDER_CLUSTER, {"ticker": None}, "/insiders"),
        (AlertKind.SEASON_DIGEST, {"period": None}, "/overview"),
    ],
)  # fmt: skip
def test_an_alert_links_where_the_bell_does(
    kind: AlertKind, payload: dict[str, str | None], path: str | None
) -> None:
    assert mail.notification_path(kind, payload) == path


def test_an_alert_is_labelled_by_what_it_follows() -> None:
    assert alert_label(AlertKind.FILER_FILED, "Berkshire Hathaway") == (
        "new 13F filings by Berkshire Hathaway"
    )
    assert (
        alert_label(AlertKind.INSIDER_CLUSTER, None)
        == "insiders buying a stock you follow together"
    )
    assert alert_label(AlertKind.SEASON_DIGEST, "ignored") == "the 13F season digest"


# --- unsubscribe tokens -----------------------------------------------------------------


@pytest.mark.parametrize(
    "mailing_list",
    [MailingList.for_rule(42), MailingList(ListKind.PRODUCT_EMAIL)],
)
def test_a_token_names_its_user_and_list(settings: Settings, mailing_list: MailingList) -> None:
    assert read_token(settings, token_for(settings, 7, mailing_list)) == (7, mailing_list)


def test_a_token_is_refused_if_anything_in_it_is_changed(settings: Settings) -> None:
    token = token_for(settings, 7, MailingList.for_rule(42))
    payload, _, signature = token.rpartition(".")
    forged = [
        token.replace("7.", "8.", 1),
        token.replace("alert_rule:42", "alert_rule:43"),
        f"{payload}.{signature[:-1]}A" if signature[-1] != "A" else f"{payload}.{signature[:-1]}B",
        "7.product_email",
        "not-a-token",
        "",
    ]
    for value in forged:
        with pytest.raises(InvalidTokenError):
            read_token(settings, value)


def test_a_token_signed_with_another_secret_is_refused(settings: Settings) -> None:
    other = settings.model_copy(update={"email_unsubscribe_secret": SecretStr("rotated")})

    with pytest.raises(InvalidTokenError, match="not signed"):
        read_token(other, token_for(settings, 7, MailingList.for_rule(42)))


@pytest.mark.parametrize("text", ["alert_rule", "product_email:3", "nope", "alert_rule:0"])
def test_a_list_is_its_kind_and_only_an_alert_names_a_rule(text: str) -> None:
    with pytest.raises(ValueError):
        MailingList.parse(text)


# --- Postmark: a send --------------------------------------------------------------------


def mailer(
    settings: Settings, answer: Callable[[httpx.Request], httpx.Response]
) -> tuple[PostmarkMailer, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer(request)

    return PostmarkMailer(settings, transport=httpx.MockTransport(handler)), requests


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": "pm-1"})


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postmark, "RETRY_DELAYS", (0, 0))


async def test_an_account_email_goes_on_the_transactional_stream_with_both_bodies(
    settings: Settings,
) -> None:
    sender, requests = mailer(settings, accepted)
    message = mail.password_reset(settings, to="ada@example.com", user_id=7, token="t", minutes=60)

    assert await sender.send(message, reference="91") == "pm-1"
    [request] = requests
    sent = json.loads(request.content)
    assert request.url == "https://api.postmarkapp.com/email"
    assert request.headers["x-postmark-server-token"] == "server-token"
    assert sent["MessageStream"] == "outbound"
    assert sent["From"] == "WhaleWatch <no-reply@mail.whalewatch.com>"
    assert (sent["TextBody"], sent["HtmlBody"]) == (message.body, message.html)
    assert sent["Metadata"] == {"email_log_id": "91"}
    assert sent["Tag"] == "password_reset"
    assert sent["Headers"] == []
    assert (sent["TrackOpens"], sent["TrackLinks"]) == (False, "None")


async def test_an_alert_goes_on_the_broadcast_stream_with_one_click_unsubscribe(
    settings: Settings,
) -> None:
    sender, requests = mailer(settings, accepted)
    message = digest(settings)
    assert message.unsubscribe is not None

    await sender.send(message, reference="92")

    sent = json.loads(requests[0].content)
    assert sent["MessageStream"] == "broadcast"
    assert sent["Headers"] == [
        {"Name": "List-Unsubscribe", "Value": f"<{message.unsubscribe.one_click_url}>"},
        {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"},
    ]


async def test_an_inactive_recipient_is_its_own_refusal(settings: Settings) -> None:
    sender, _ = mailer(
        settings,
        lambda request: httpx.Response(
            422, json={"ErrorCode": 406, "Message": "You tried to send to inactive recipient(s)."}
        ),
    )

    with pytest.raises(InactiveRecipientError):
        await sender.send(digest(settings), reference="1")


async def test_a_refusal_is_not_retried(settings: Settings) -> None:
    sender, requests = mailer(
        settings, lambda request: httpx.Response(422, json={"ErrorCode": 300, "Message": "Bad"})
    )

    with pytest.raises(MailError, match="300"):
        await sender.send(digest(settings), reference="1")
    assert len(requests) == 1


async def test_a_busy_or_failing_postmark_is_asked_again(settings: Settings) -> None:
    answers = iter([httpx.Response(429), httpx.Response(503)])
    sender, requests = mailer(settings, lambda request: next(answers, accepted(request)))

    assert await sender.send(digest(settings), reference="1") == "pm-1"
    assert len(requests) == 3


async def test_a_postmark_never_reached_is_asked_three_times_then_given_up(
    settings: Settings,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sender, requests = mailer(settings, unreachable)

    with pytest.raises(MailError, match="could not be reached"):
        await sender.send(digest(settings), reference="1")
    assert len(requests) == 3


async def test_a_send_with_no_answer_is_not_sent_again(settings: Settings) -> None:
    """It may have arrived, and Postmark has no idempotency key."""

    def silent(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    sender, requests = mailer(settings, silent)

    with pytest.raises(MailError, match="maybe sent"):
        await sender.send(digest(settings), reference="1")
    assert len(requests) == 1


# --- Postmark: a webhook --------------------------------------------------------------


def event(settings: Settings, **record: object) -> postmark.Event:
    return parse_event(settings, json.dumps(record).encode())


def test_a_delivery_names_its_row(settings: Settings) -> None:
    delivered = event(
        settings,
        RecordType="Delivery",
        MessageID="pm-1",
        Recipient="ada@example.com",
        MessageStream="outbound",
        Metadata={"email_log_id": "91"},
        Details="Test delivery",
    )

    assert delivered.kind is EventKind.DELIVERED
    assert (delivered.reference, delivered.message_id) == (91, "pm-1")
    assert delivered.stream is Stream.TRANSACTIONAL
    assert delivered.recipient == "ada@example.com"


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        ({"Type": "HardBounce", "Inactive": True}, EventKind.HARD_BOUNCE),
        ({"Type": "BadEmailAddress", "Inactive": False}, EventKind.HARD_BOUNCE),
        ({"Type": "Blocked", "Inactive": True}, EventKind.HARD_BOUNCE),
        ({"Type": "Transient", "Inactive": False}, EventKind.SOFT_BOUNCE),
        ({"Type": "SoftBounce", "Inactive": False}, EventKind.SOFT_BOUNCE),
        ({"Type": "SpamComplaint", "Inactive": True}, EventKind.COMPLAINT),
    ],
)
def test_a_bounce_is_hard_when_the_address_is_gone(
    settings: Settings, record: dict[str, object], kind: EventKind
) -> None:
    bounced = event(
        settings,
        RecordType="Bounce",
        MessageID="pm-1",
        Email="ada@example.com",
        MessageStream="broadcast",
        Description="The server was unable to deliver your message",
        **record,
    )

    assert bounced.kind is kind
    assert bounced.recipient == "ada@example.com"
    assert bounced.stream is Stream.BROADCAST


def test_a_complaint_and_a_subscription_change(settings: Settings) -> None:
    complaint = event(
        settings, RecordType="SpamComplaint", Email="a@x.io", MessageStream="broadcast"
    )
    suppressed = event(
        settings,
        RecordType="SubscriptionChange",
        Recipient="a@x.io",
        MessageStream="outbound",
        SuppressSending=True,
        SuppressionReason="ManualSuppression",
    )
    reactivated = event(
        settings,
        RecordType="SubscriptionChange",
        Recipient="a@x.io",
        MessageStream="outbound",
        SuppressSending=False,
    )

    assert complaint.kind is EventKind.COMPLAINT
    assert (suppressed.kind, suppressed.reason) == (
        EventKind.SUPPRESSED,
        SuppressionReason.PROVIDER,
    )
    assert (reactivated.kind, reactivated.reason) == (EventKind.REACTIVATED, None)


def test_an_open_or_a_click_is_nothing_to_record(settings: Settings) -> None:
    assert event(settings, RecordType="Open", MessageID="pm-1").kind is EventKind.IGNORED


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"MessageID": "pm-1"}', b'{"RecordType": "SubscriptionChange"}']
)
def test_a_body_that_is_not_a_webhook_is_refused(settings: Settings, body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_event(settings, body)


# --- the sending domain's DNS ----------------------------------------------------------


class FakeDns:
    def __init__(self, txt: dict[str, list[str]], cname: dict[str, str]) -> None:
        self._txt, self._cname = txt, cname

    async def txt(self, name: str) -> list[str]:
        return self._txt.get(name, [])

    async def cname(self, name: str) -> str | None:
        return self._cname.get(name)


READY_TXT = {
    "mail.whalewatch.com": ["v=spf1 include:spf.mtasv.net -all"],
    "20261027pm._domainkey.mail.whalewatch.com": ["k=rsa;p=MIGfMA0GCSqGSIb3DQEBAQUAA4"],
    "_dmarc.mail.whalewatch.com": ["v=DMARC1; p=quarantine; rua=mailto:dmarc@whalewatch.com"],
}
READY_CNAME = {"pm-bounces.mail.whalewatch.com": "pm.mtasv.net"}


async def checks(
    txt: dict[str, list[str]], cname: dict[str, str] = READY_CNAME
) -> dict[str, Check]:
    found = await check_domain(
        FakeDns(txt, cname), "mail.whalewatch.com", dkim_selector="20261027pm"
    )
    return {check.name: check for check in found}


async def test_a_domain_with_spf_dkim_dmarc_and_a_return_path_is_ready() -> None:
    found = await checks(READY_TXT)

    assert all(check.ok for check in found.values()), found
    assert all(check.warning is None for check in found.values())


async def test_each_missing_record_fails_its_own_check() -> None:
    found = await checks({}, {})

    assert {name: check.ok for name, check in found.items()} == {
        "spf": False,
        "return_path": False,
        "dkim": False,
        "dmarc": False,
    }


async def test_spf_needs_postmark_and_only_one_record() -> None:
    without = await checks({**READY_TXT, "mail.whalewatch.com": ["v=spf1 -all"]})
    twice = await checks(
        {**READY_TXT, "mail.whalewatch.com": ["v=spf1 -all", "v=spf1 include:spf.mtasv.net -all"]}
    )

    assert not without["spf"].ok
    assert not twice["spf"].ok and "allows one" in twice["spf"].detail


async def test_a_return_path_elsewhere_fails() -> None:
    found = await checks(READY_TXT, {"pm-bounces.mail.whalewatch.com": "bounces.example.com"})

    assert not found["return_path"].ok


async def test_dmarc_falls_back_to_the_apex_and_its_subdomain_policy() -> None:
    txt = {k: v for k, v in READY_TXT.items() if not k.startswith("_dmarc")}
    enforced = await checks({**txt, "_dmarc.whalewatch.com": ["v=DMARC1; p=none; sp=reject"]})
    reporting = await checks({**txt, "_dmarc.whalewatch.com": ["v=DMARC1; p=none"]})

    assert enforced["dmarc"].ok and enforced["dmarc"].warning is None
    assert reporting["dmarc"].ok and reporting["dmarc"].warning is not None
