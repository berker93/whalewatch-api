"""Email delivery against the database: the courier's checks and log, unsubscribing,
and Postmark's webhooks.

Postmark is the outbox: each message still goes through the courier, so it is
checked against the suppression list and its list, and logged, and the outbox
answers with an id as Postmark would.
"""

import base64
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import mail, privacy
from app.accounts.delivery import Courier, address_hash
from app.accounts.mailer import InactiveRecipientError
from app.accounts.unsubscribe import ListKind, MailingList, token_for
from app.api.routers.email import WEBHOOK_PATH
from app.core.config import Settings
from app.db.models import (
    AlertKind,
    AlertRule,
    ConsentSource,
    EmailLog,
    EmailStatus,
    EmailSuppression,
    Purpose,
)
from tests.integration.accounts import FRONTEND, Outbox, make_user, sign_in
from tests.integration.conftest import in_session

WEBHOOK_SECRET = "pm-webhook-secret"
AUTH = {
    "authorization": "Basic " + base64.b64encode(f"postmark:{WEBHOOK_SECRET}".encode()).decode()
}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The integration suite's, sending through Postmark (the outbox, here)."""
    return settings.model_copy(
        update={
            "email_backend": "postmark",
            "postmark_server_token": SecretStr("pm-server-token"),
            "postmark_webhook_secret": SecretStr(WEBHOOK_SECRET),
            "email_unsubscribe_secret": SecretStr("unsubscribe-secret"),
        }
    )


@pytest.fixture
async def ada(db_session: AsyncSession) -> int:
    user_id = await make_user(db_session)
    privacy.record_signup(db_session, user_id, marketing_email=False, source=ConsentSource.REGISTER)
    await db_session.flush()
    return user_id


@pytest.fixture
def courier(settings: Settings, db_session: AsyncSession, outbox: Outbox) -> Courier:
    return Courier(settings, in_session(db_session), outbox)


async def digest_rule(session: AsyncSession, user_id: int, channels: list[str]) -> int:
    rule = AlertRule(user_id=user_id, kind=AlertKind.SEASON_DIGEST.value, channels=channels)
    session.add(rule)
    await session.flush()
    return rule.id


def reset(settings: Settings, user_id: int) -> mail.Message:
    return mail.password_reset(
        settings, to="ada@example.com", user_id=user_id, token="t", minutes=60
    )


def digest(settings: Settings, user_id: int, rule_id: int) -> mail.Message:
    return mail.alert(
        settings,
        to="ada@example.com",
        user_id=user_id,
        rule_id=rule_id,
        kind=AlertKind.SEASON_DIGEST,
        title="The 2026Q3 season so far",
        body="61 of 100 filers are in.",
        payload={"period": "2026Q3"},
    )


async def logged(session: AsyncSession) -> list[EmailLog]:
    session.expire_all()
    return list(await session.scalars(select(EmailLog).order_by(EmailLog.id)))


async def webhook(client: AsyncClient, **record: Any) -> Response:
    return await client.post(WEBHOOK_PATH, json=record, headers=AUTH)


def complaint(row: EmailLog, stream: str) -> dict[str, Any]:
    return {
        "RecordType": "SpamComplaint",
        "MessageID": row.provider_id,
        "Email": "ada@example.com",
        "MessageStream": stream,
        "Metadata": {"email_log_id": str(row.id)},
    }


def token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


# --- the courier ------------------------------------------------------------------------


async def test_a_sent_email_is_logged_with_its_kind_stream_and_the_providers_id(
    db_session: AsyncSession, courier: Courier, outbox: Outbox, settings: Settings, ada: int
) -> None:
    assert await courier.send(reset(settings, ada)) is EmailStatus.SENT

    [row] = await logged(db_session)
    assert (row.user_id, row.kind, row.stream, row.status) == (
        ada,
        "password_reset",
        "transactional",
        "sent",
    )
    assert row.provider_id == f"outbox-{row.id}"
    assert len(outbox.sent) == 1


async def test_a_hard_bounce_stops_every_email_to_the_address(
    client: AsyncClient,
    db_session: AsyncSession,
    courier: Courier,
    outbox: Outbox,
    settings: Settings,
    ada: int,
) -> None:
    await courier.send(reset(settings, ada))
    [row] = await logged(db_session)

    response = await webhook(
        client,
        RecordType="Bounce",
        Type="HardBounce",
        Inactive=True,
        MessageID=row.provider_id,
        Email="Ada@Example.com",
        MessageStream="outbound",
        Description="The server was unable to deliver your message (ex: unknown user).",
        Metadata={"email_log_id": str(row.id)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["suppression"] == "all"

    rule = await digest_rule(db_session, ada, ["email"])
    assert await courier.send(reset(settings, ada)) is EmailStatus.SUPPRESSED
    assert await courier.send(digest(settings, ada, rule)) is EmailStatus.SUPPRESSED
    first, second, third = await logged(db_session)
    assert first.status == "bounced" and "HardBounce" in (first.detail or "")
    assert second.status == third.status == "suppressed"
    assert second.detail == "address suppressed for all: hard_bounce"
    assert len(outbox.sent) == 1


async def test_a_complaint_about_a_digest_stops_digests_and_not_password_resets(
    client: AsyncClient,
    db_session: AsyncSession,
    courier: Courier,
    outbox: Outbox,
    settings: Settings,
    ada: int,
) -> None:
    rule = await digest_rule(db_session, ada, ["in_app", "email"])
    await courier.send(digest(settings, ada, rule))
    [sent] = await logged(db_session)

    response = await webhook(client, **complaint(sent, "broadcast"))
    assert response.json()["suppression"] == "broadcast"

    assert await courier.send(digest(settings, ada, rule)) is EmailStatus.SUPPRESSED
    assert await courier.send(reset(settings, ada)) is EmailStatus.SENT
    assert [m.kind.value for m in outbox.sent] == ["alert", "password_reset"]
    assert (await logged(db_session))[0].status == "complained"


async def test_a_complaint_about_an_account_email_stops_everything(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    await courier.send(reset(settings, ada))
    [sent] = await logged(db_session)

    await webhook(client, **complaint(sent, "outbound"))

    assert await courier.send(reset(settings, ada)) is EmailStatus.SUPPRESSED


async def test_a_complaint_after_a_hard_bounce_leaves_the_address_stopped_for_everything(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["email"])
    await courier.send(digest(settings, ada, rule))
    [sent] = await logged(db_session)
    bounce = {"RecordType": "Bounce", "Type": "HardBounce", "Email": "ada@example.com"}

    await webhook(client, **bounce, MessageStream="broadcast")
    await webhook(client, **complaint(sent, "broadcast"))

    suppression = await db_session.get(EmailSuppression, address_hash("ada@example.com"))
    assert suppression is not None
    await db_session.refresh(suppression)
    assert (suppression.scope, suppression.reason) == ("all", "hard_bounce")


async def test_an_address_postmark_has_stopped_is_stopped_here_too(
    db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    class Inactive:
        async def send(self, message: mail.Message, *, reference: str) -> str | None:
            raise InactiveRecipientError("Postmark: You tried to send to inactive recipient(s).")

    courier = Courier(settings, in_session(db_session), Inactive())
    rule = await digest_rule(db_session, ada, ["email"])

    assert await courier.send(digest(settings, ada, rule)) is EmailStatus.SUPPRESSED
    suppression = await db_session.get(EmailSuppression, address_hash("ada@example.com"))
    assert suppression is not None
    assert (suppression.scope, suppression.reason) == ("broadcast", "provider")
    [row] = await logged(db_session)
    assert row.status == "suppressed" and "inactive" in (row.detail or "")


async def test_an_alert_whose_rule_no_longer_emails_is_not_sent(
    db_session: AsyncSession, courier: Courier, outbox: Outbox, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["in_app"])

    assert await courier.send(digest(settings, ada, rule)) is EmailStatus.SUPPRESSED
    [row] = await logged(db_session)
    assert row.detail == f"not on alert_rule:{rule}"
    assert outbox.sent == []


# --- unsubscribing ---------------------------------------------------------------------


async def test_the_page_reads_the_list_and_leaves_nothing(
    client: AsyncClient, db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["in_app", "email"])
    token = token_for(settings, ada, MailingList.for_rule(rule))

    response = await client.get("/v1/email/unsubscribe", params={"token": token})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "mailing_list": f"alert_rule:{rule}",
        "kind": "alert_rule",
        "label": "the 13F season digest",
        "subscribed": True,
    }
    assert response.headers["cache-control"] == "no-store"


async def test_one_click_unsubscribe_needs_no_session_or_origin_and_counts_at_once(
    client: AsyncClient,
    db_session: AsyncSession,
    courier: Courier,
    outbox: Outbox,
    settings: Settings,
    ada: int,
) -> None:
    rule = await digest_rule(db_session, ada, ["in_app", "email"])
    queued = digest(settings, ada, rule)
    assert queued.unsubscribe is not None

    # As Gmail's servers post it: the header's URL, a form body, nothing else.
    response = await client.post(
        queued.unsubscribe.one_click_url.removeprefix(settings.api_url or ""),
        content="List-Unsubscribe=One-Click",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["subscribed"] is False
    row = await db_session.get(AlertRule, rule)
    assert row is not None
    await db_session.refresh(row)
    assert (row.channels, row.is_active) == (["in_app"], True)
    # Queued before the unsubscribe, sent after it: not sent.
    assert await courier.send(queued) is EmailStatus.SUPPRESSED
    assert outbox.sent == []


async def test_unsubscribing_from_a_digest_never_stops_a_password_reset(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["email"])
    token = token_for(settings, ada, MailingList.for_rule(rule))
    await client.post("/v1/email/unsubscribe", params={"token": token}, headers=FRONTEND)

    response = await client.post(
        "/v1/auth/password-reset", json={"email": "ada@example.com"}, headers=FRONTEND
    )

    assert response.status_code == 204
    [message] = outbox.sent
    assert message.kind.value == "password_reset"


async def test_an_alert_that_only_emailed_is_turned_off_and_unsubscribing_twice_is_once(
    client: AsyncClient, db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["email"])
    token = token_for(settings, ada, MailingList.for_rule(rule))

    first = await client.post("/v1/email/unsubscribe", params={"token": token}, headers=FRONTEND)
    second = await client.post("/v1/email/unsubscribe", params={"token": token}, headers=FRONTEND)

    assert first.json() == second.json()
    assert first.json()["subscribed"] is False
    row = await db_session.get(AlertRule, rule)
    assert row is not None
    await db_session.refresh(row)
    assert (row.channels, row.is_active) == (["email"], False)


async def test_unsubscribing_from_product_email_withdraws_it_in_the_ledger(
    client: AsyncClient, db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    token = token_for(settings, ada, MailingList(ListKind.PRODUCT_EMAIL))

    response = await client.post("/v1/email/unsubscribe", params={"token": token}, headers=FRONTEND)

    assert response.json() == {
        "mailing_list": "product_email",
        "kind": "product_email",
        "label": "news about WhaleWatch",
        "subscribed": False,
    }
    standing = await privacy.standing(db_session, ada)
    record = standing[Purpose.PRODUCT_EMAIL]
    assert (record.granted, record.source) == (False, "unsubscribe")


async def test_a_deleted_alerts_link_says_there_is_nothing_left_to_leave(
    client: AsyncClient, db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    token = token_for(settings, ada, MailingList.for_rule(999_999))

    response = await client.post("/v1/email/unsubscribe", params={"token": token})

    assert response.status_code == 200
    assert response.json()["subscribed"] is False


@pytest.mark.parametrize("token", ["7.alert_rule:1.forged", "garbage"])
async def test_a_link_this_site_did_not_sign_is_refused(client: AsyncClient, token: str) -> None:
    response = await client.post("/v1/email/unsubscribe", params={"token": token})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_token"


async def test_the_unsubscribe_link_in_an_alert_opens_the_web_page_with_a_working_token(
    client: AsyncClient, db_session: AsyncSession, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["in_app", "email"])
    message = digest(settings, ada, rule)
    assert message.unsubscribe is not None
    page = message.unsubscribe.page_url

    assert page.startswith(f"{settings.web_url}/unsubscribe?token=")
    response = await client.get("/v1/email/unsubscribe", params={"token": token_of(page)})
    assert response.json()["subscribed"] is True


# --- Postmark's webhooks -----------------------------------------------------------------


async def test_a_delivery_moves_the_row_on_and_an_old_event_never_moves_it_back(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    rule = await digest_rule(db_session, ada, ["email"])
    await courier.send(digest(settings, ada, rule))
    [row] = await logged(db_session)
    # Read now: the webhooks' updates expire the row in the shared session.
    delivery = {
        "RecordType": "Delivery",
        "MessageID": row.provider_id,
        "Recipient": "ada@example.com",
        "MessageStream": "broadcast",
        "Metadata": {"email_log_id": str(row.id)},
    }
    complained = complaint(row, "broadcast")

    assert (await webhook(client, **delivery)).json()["status"] == "delivered"
    await webhook(client, **complained)
    again = await webhook(client, **delivery)

    assert again.status_code == 200
    assert again.json()["status"] is None
    assert (await logged(db_session))[0].status == "complained"


async def test_a_webhook_without_metadata_finds_its_row_by_postmarks_id(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    await courier.send(reset(settings, ada))
    [row] = await logged(db_session)

    response = await webhook(
        client, RecordType="Delivery", MessageID=row.provider_id, Recipient="ada@example.com"
    )

    assert response.json()["email_log_id"] == row.id


async def test_reactivating_an_address_in_postmark_lifts_it_here(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    await webhook(
        client, RecordType="Bounce", Type="HardBounce", Email="ada@example.com", Inactive=True
    )
    assert await courier.send(reset(settings, ada)) is EmailStatus.SUPPRESSED

    await webhook(
        client,
        RecordType="SubscriptionChange",
        Recipient="ada@example.com",
        MessageStream="outbound",
        SuppressSending=False,
    )

    assert await courier.send(reset(settings, ada)) is EmailStatus.SENT


async def test_an_event_this_api_does_not_record_is_still_a_200(client: AsyncClient) -> None:
    response = await webhook(client, RecordType="Open", MessageID="pm-1")

    assert response.status_code == 200
    assert response.json()["event"] == "ignored"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Basic " + base64.b64encode(b"postmark:wrong").decode()},
        {"authorization": "Bearer " + WEBHOOK_SECRET},
        {"authorization": "Basic not-base64!"},
    ],
)
async def test_a_webhook_without_postmarks_credentials_is_refused(
    client: AsyncClient, db_session: AsyncSession, headers: dict[str, str]
) -> None:
    response = await client.post(
        WEBHOOK_PATH,
        json={"RecordType": "Bounce", "Type": "HardBounce", "Email": "ada@example.com"},
        headers=headers,
    )

    assert response.status_code == 401
    assert await db_session.scalar(select(EmailSuppression)) is None


async def test_a_webhook_where_postmark_is_not_the_backend_is_retried_later(
    app: FastAPI, client: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "settings", settings.model_copy(update={"email_backend": "log"}))

    response = await webhook(client, RecordType="Delivery", MessageID="pm-1")

    assert response.status_code == 503


async def test_a_malformed_webhook_is_a_400(client: AsyncClient) -> None:
    response = await client.post(WEBHOOK_PATH, content=b"{", headers=AUTH)

    assert response.status_code == 400


# --- the account's view --------------------------------------------------------------


async def test_the_export_lists_each_email_and_how_far_it_got(
    client: AsyncClient, db_session: AsyncSession, courier: Courier, settings: Settings, ada: int
) -> None:
    await courier.send(reset(settings, ada))
    await sign_in(client)

    response = await client.get("/v1/me/export")

    [email] = response.json()["emails"]
    assert {k: email[k] for k in ("kind", "stream", "status")} == {
        "kind": "password_reset",
        "stream": "transactional",
        "status": "sent",
    }
