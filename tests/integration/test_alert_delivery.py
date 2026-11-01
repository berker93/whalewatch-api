"""Alert email against a real Postgres: ALR-6's batches, cap, quiet hours and digest.

Events are made here rather than ingested (ALR-5's tests ingest): what is
under test is what happens after an event is heard. Each is fired as the
emission does, queued items are moved to the moment the test wants
(``alert_outbox.queued_at`` is the database's clock otherwise), and the
sender runs at a made-up ``now``. The user's local clock comes from Postgres,
as it does in production.

The day is Wednesday 2026-08-12, inside the August filing window; users are
in New York (UTC-4) unless the test says otherwise.
"""

from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import entitlements
from app.accounts.delivery import courier_for
from app.alerts.events import Event
from app.alerts.firing import fire
from app.alerts.outbox import KEEP_FOR, Decided, decide_due, send_due
from app.core.config import Settings
from app.db.models import (
    PAYLOAD_KEYS,
    AlertDelivery,
    AlertKind,
    AlertOutbox,
    AlertRule,
    AppUser,
    EmailLog,
    EmailStatus,
    Filer,
    Notification,
    SignupRequest,
    WatchlistItem,
)
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, PASSWORD, Outbox, make_user, sign_in, subscribe
from tests.integration.conftest import in_session

RULES_FROM = datetime(2026, 1, 1, tzinfo=UTC)
#: 14:00 in New York, 2026-08-12: a window day.
FILED = datetime(2026, 8, 12, 18, tzinfo=UTC)

_numbers = count(1)


def ny(hour: int, minute: int = 0, *, day: int = 12, month: int = 8) -> datetime:
    """A New York wall clock on 2026-08-``day``, as the UTC moment it is (EDT)."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC) + timedelta(hours=4)


@pytest.fixture(autouse=True)
async def catalog(db_session: AsyncSession) -> AsyncIterator[None]:
    entitlements.install(await entitlements.load_catalog(db_session))
    yield
    entitlements.install(None)


@pytest.fixture
async def pro(db_session: AsyncSession) -> int:
    user_id = await make_user(db_session, "pro@example.com")
    await subscribe(db_session, user_id, "pro")
    return user_id


async def _filer(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    return filer_id


async def _rule(
    session: AsyncSession,
    user_id: int,
    kind: AlertKind,
    *,
    filer_id: int | None = None,
    channels: tuple[str, ...] = ("in_app", "email"),
) -> int:
    rule_id = await session.scalar(
        insert(AlertRule)
        .values(
            user_id=user_id,
            kind=str(kind),
            filer_id=filer_id,
            channels=list(channels),
            created_at=RULES_FROM,
        )
        .returning(AlertRule.id)
    )
    assert rule_id is not None
    return rule_id


def _filed(
    filer_id: int, slug: str, *, filed_at: datetime = FILED, period: str = "2026Q2"
) -> Event:
    """``slug``'s 13F for ``period``, as :mod:`app.alerts.events` reads it."""
    return Event(
        kind=AlertKind.FILER_FILED,
        filer_id=filer_id,
        period=datetime(2026, 6, 30).date(),
        security_id=None,
        filed_at=filed_at,
        dollars=None,
        change=None,
        dedupe_key=f"filer_filed:{slug}:{period}:{next(_numbers)}",
        title=f"{slug.upper()} filed its 13F for {period}",
        body="2 positions worth $265,000.",
        payload=dict.fromkeys(PAYLOAD_KEYS)
        | {"filer_slug": slug, "period": period, "filer_name": slug.upper()},
    )


async def _fire(session: AsyncSession, *events: Event, queued_at: datetime) -> int:
    """Fire ``events`` as an emission does, the items it queues queued at ``queued_at``."""
    fired = await fire(session, list(events))
    await session.execute(
        update(AlertOutbox)
        .where(AlertOutbox.settled_at.is_(None), AlertOutbox.queued_at > queued_at)
        .values(queued_at=queued_at)
    )
    await session.commit()
    return fired.queued


async def _send(
    session: AsyncSession, settings: Settings, now: datetime, redis: FakeRedis | None = None
) -> Decided:
    decided = await decide_due(session, settings, now=now)
    courier = courier_for(settings, in_session(session))
    await send_due(session, decided, courier=courier, redis=redis or FakeRedis())  # type: ignore[arg-type]
    return decided


async def _waiting(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.execute(
        select(AlertOutbox.route, AlertOutbox.title)
        .where(AlertOutbox.settled_at.is_(None))
        .order_by(AlertOutbox.id)
    )
    return [tuple(row) for row in rows]  # type: ignore[misc]


# --- batches ---------------------------------------------------------------------------


async def test_alerts_in_one_window_go_as_one_email_once_it_closes(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    a, b = await _filer(db_session, "fund-a"), await _filer(db_session, "fund-b")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=b)
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(10))
    await _fire(db_session, _filed(b, "fund-b"), queued_at=ny(10, 9))

    early = await _send(db_session, settings, ny(10, 14))
    assert early.batches == 0
    assert outbox.sent == []

    await _send(db_session, settings, ny(10, 15))

    [message] = outbox.sent
    assert message.subject == "FUND-B filed its 13F for 2026Q2, and 1 more"
    assert "2 alerts, newest first." in message.body
    assert message.body.index("FUND-B filed") < message.body.index("FUND-A filed")
    # Two rules: the link leaves every alert email, not one rule's.
    assert message.unsubscribe is not None
    assert message.unsubscribe.mailing_list.id == "alert_email"
    assert await _waiting(db_session) == []
    [delivery] = (await db_session.execute(select(AlertDelivery))).scalars()
    assert (delivery.kind, delivery.items, delivery.emailed, delivery.email_status) == (
        "batch",
        2,
        True,
        "sent",
    )
    assert delivery.local_at == datetime(2026, 8, 12, 10, 15)


async def test_one_alert_is_the_alerts_own_email_on_its_rules_list(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    a = await _filer(db_session, "fund-a")
    rule_id = await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(10))

    await _send(db_session, settings, ny(10, 15))

    [message] = outbox.sent
    assert message.subject == "FUND-A filed its 13F for 2026Q2"
    assert message.unsubscribe is not None
    assert message.unsubscribe.mailing_list.id == f"alert_rule:{rule_id}"


async def test_a_rule_turned_off_while_its_batch_waits_sends_nothing(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    a = await _filer(db_session, "fund-a")
    rule_id = await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(10))
    await db_session.execute(
        update(AlertRule).where(AlertRule.id == rule_id).values(is_active=False)
    )

    decided = await _send(db_session, settings, ny(10, 15))

    assert decided.dropped == 1
    assert outbox.sent == []
    assert await _waiting(db_session) == []
    # The bell has it all the same: only the email waited.
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_a_second_emission_queues_nothing_more(
    db_session: AsyncSession, settings: Settings, pro: int
) -> None:
    a = await _filer(db_session, "fund-a")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    event = _filed(a, "fund-a")

    assert await _fire(db_session, event, queued_at=ny(10)) == 1
    assert await _fire(db_session, event, queued_at=ny(10)) == 0


# --- quiet hours ---------------------------------------------------------------------------


async def test_quiet_hours_hold_email_until_they_end_in_the_users_own_zone(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    """Tokyo is 13 hours ahead of New York: 10:00 there is 21:00 here."""
    await db_session.execute(
        update(AppUser).where(AppUser.id == pro).values(quiet_time_zone="Asia/Tokyo")
    )
    a, b = await _filer(db_session, "fund-a"), await _filer(db_session, "fund-b")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=b)
    # 23:00 and 05:00 in Tokyo.
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(10))
    await _fire(db_session, _filed(b, "fund-b"), queued_at=ny(16))

    await _send(db_session, settings, ny(18, 59))  # 07:59 in Tokyo
    assert outbox.sent == []

    await _send(db_session, settings, ny(19))  # 08:00 in Tokyo

    [message] = outbox.sent
    assert message.subject == "FUND-B filed its 13F for 2026Q2, and 1 more"


# --- the cap ----------------------------------------------------------------------------------


async def test_three_emails_a_day_and_the_rest_in_the_next_digest(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    filers = [await _filer(db_session, f"fund-{n}") for n in range(4)]
    for filer_id in filers:
        await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=filer_id)
    for hour, filer_id in zip((9, 10, 11, 12), filers, strict=True):
        await _fire(db_session, _filed(filer_id, f"fund-{filer_id}"), queued_at=ny(hour))
        await _send(db_session, settings, ny(hour, 15))

    assert len(outbox.sent) == 3
    assert await _waiting(db_session) == [("digest", f"FUND-{filers[3]} filed its 13F for 2026Q2")]

    # The evening's digest finds the day's emails spent: it goes in the morning.
    await _send(db_session, settings, ny(19))
    assert len(outbox.sent) == 3
    await _send(db_session, settings, ny(8, day=13))

    digest = outbox.sent[3]
    assert digest.subject == "1 more alert"
    assert "came after the day's emails ran out" in digest.body
    assert f"FUND-{filers[3]} filed its 13F" in digest.body
    assert await _waiting(db_session) == []


# --- the season digest --------------------------------------------------------------------------


async def test_a_free_accounts_digest_counts_its_investors_filings_once_an_evening(
    db_session: AsyncSession, settings: Settings, outbox: Outbox
) -> None:
    user_id = await make_user(db_session)
    rule_id = await _rule(db_session, user_id, AlertKind.SEASON_DIGEST)
    followed = [await _filer(db_session, slug) for slug in ("fund-a", "fund-b")]
    other = await _filer(db_session, "fund-c")
    for filer_id in followed:
        await db_session.execute(insert(WatchlistItem).values(user_id=user_id, filer_id=filer_id))
    late = datetime(2026, 8, 25, 18, tzinfo=UTC)  # after the window

    queued = await _fire(
        db_session,
        _filed(followed[0], "fund-a"),
        _filed(followed[1], "fund-b", filed_at=FILED + timedelta(hours=2)),
        _filed(other, "fund-c"),
        _filed(followed[0], "fund-a", filed_at=late, period="2026Q1"),
        queued_at=ny(16),
    )
    assert queued == 2

    redis = FakeRedis()
    await _send(db_session, settings, ny(18, 59), redis)
    assert outbox.sent == []
    await _send(db_session, settings, ny(19), redis)
    await _send(db_session, settings, ny(21), redis)

    [message] = outbox.sent
    assert message.subject == "2 of your investors filed today"
    assert "FUND-A filed its 13F for 2026Q2" in message.body
    assert "/overview?period=2026Q2" in message.body
    assert message.unsubscribe is not None
    assert message.unsubscribe.mailing_list.id == f"alert_rule:{rule_id}"
    [bell] = (await db_session.execute(select(Notification))).scalars()
    assert (bell.kind, bell.title, bell.body, bell.dedupe_key) == (
        "season_digest",
        "2 of your investors filed today",
        "FUND-A and FUND-B filed their 13F.",
        "season_digest:2026-08-12",
    )
    assert bell.payload["period"] == "2026Q2"
    assert bell.payload["investors"] == ["fund-a", "fund-b"]


async def test_a_digest_that_only_rings_the_bell_emails_nothing(
    db_session: AsyncSession, settings: Settings, outbox: Outbox
) -> None:
    user_id = await make_user(db_session)
    await _rule(db_session, user_id, AlertKind.SEASON_DIGEST, channels=("in_app",))
    a = await _filer(db_session, "fund-a")
    await db_session.execute(insert(WatchlistItem).values(user_id=user_id, filer_id=a))
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(16))

    decided = await _send(db_session, settings, ny(19))

    assert decided.digests == 1
    assert outbox.sent == []
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1
    [delivery] = (await db_session.execute(select(AlertDelivery))).scalars()
    assert (delivery.emailed, delivery.email_status) == (False, None)


async def test_a_digest_rule_made_after_the_filing_does_not_count_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    user_id = await make_user(db_session)
    rule_id = await _rule(db_session, user_id, AlertKind.SEASON_DIGEST)
    await db_session.execute(
        update(AlertRule).where(AlertRule.id == rule_id).values(created_at=FILED + timedelta(1))
    )
    a = await _filer(db_session, "fund-a")
    await db_session.execute(insert(WatchlistItem).values(user_id=user_id, filer_id=a))

    assert await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(16)) == 0


# --- unsubscribing ------------------------------------------------------------------------------


async def test_leaving_every_alert_email_stops_each_rules_email(
    client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    pro: int,
    outbox: Outbox,
) -> None:
    a, b = await _filer(db_session, "fund-a"), await _filer(db_session, "fund-b")
    both = await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    email_only = await _rule(
        db_session, pro, AlertKind.FILER_FILED, filer_id=b, channels=("email",)
    )
    await _fire(db_session, _filed(a, "fund-a"), _filed(b, "fund-b"), queued_at=ny(10))
    await _send(db_session, settings, ny(10, 15))
    [message] = outbox.sent
    token = message.body.split("token=", 1)[1].split()[0]

    response = await client.post("/v1/email/unsubscribe", params={"token": token}, headers=FRONTEND)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "mailing_list": "alert_email",
        "kind": "alert_email",
        "label": "your alerts",
        "subscribed": False,
    }
    rules = {
        rule.id: (rule.channels, rule.is_active)
        for rule in (await db_session.execute(select(AlertRule))).scalars()
    }
    assert rules == {both: (["in_app"], True), email_only: (["email"], False)}


# --- signup and settings -----------------------------------------------------------------------


async def test_the_browsers_zone_is_kept_at_signup_for_quiet_hours(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    body = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "password": PASSWORD,
        "accept_terms": True,
    }

    async def register(**more: str) -> Response:
        return await client.post("/v1/auth/register", json=body | more, headers=FRONTEND)

    assert (await register(quiet_time_zone="Mars/Olympus")).status_code == 422
    # Signups closed: the request keeps it for create-user.
    assert (await register(quiet_time_zone="Europe/Berlin")).status_code == 202
    request = (await db_session.execute(select(SignupRequest))).scalar_one()
    assert request.quiet_time_zone == "Europe/Berlin"

    app.state.settings = make_settings(signups_open=True)
    assert (await register(quiet_time_zone="Europe/Berlin")).status_code == 202

    user = (await db_session.execute(select(AppUser))).scalar_one()
    # The account's own zone stays the market's (Q2); quiet hours are the browser's.
    assert (user.time_zone, user.quiet_time_zone) == ("America/New_York", "Europe/Berlin")
    assert (f"{user.quiet_start:%H:%M}", f"{user.quiet_end:%H:%M}") == ("22:00", "08:00")


async def test_quiet_hours_are_read_and_changed_on_their_own(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    read = await client.get("/v1/me/alert-delivery")
    assert read.status_code == 200, read.text
    assert read.json() == {
        "time_zone": "America/New_York",
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "batch_minutes": 15,
        "emails_per_day": 3,
        "digest_at": "19:00",
    }

    changed = await client.patch(
        "/v1/me/alert-delivery",
        json={"time_zone": "Asia/Tokyo", "quiet_start": "23:30"},
        headers=FRONTEND,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["time_zone"] == "Asia/Tokyo"
    assert changed.json()["quiet_start"] == "23:30"
    assert changed.json()["quiet_end"] == "08:00"

    for bad in ({"quiet_end": "24:00"}, {"quiet_end": "7:00"}, {"time_zone": "Nowhere"}):
        response = await client.patch("/v1/me/alert-delivery", json=bad, headers=FRONTEND)
        assert response.status_code == 422, bad
    account = await client.get("/v1/account")
    assert account.json()["time_zone"] == "America/New_York"


# --- keeping ------------------------------------------------------------------------------------


async def test_old_deliveries_are_forgotten(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    a = await _filer(db_session, "fund-a")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(10))
    await _send(db_session, settings, ny(10, 15))
    sent_at: Any = await db_session.scalar(select(AlertDelivery.sent_at))

    await decide_due(db_session, settings, now=sent_at + KEEP_FOR - timedelta(minutes=1))
    assert await db_session.scalar(select(func.count()).select_from(AlertDelivery)) == 1
    await decide_due(db_session, settings, now=sent_at + KEEP_FOR + timedelta(minutes=1))

    assert await db_session.scalar(select(func.count()).select_from(AlertDelivery)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AlertOutbox)) == 0
    # The email's log is the email's, kept as long as the account.
    assert await db_session.scalar(select(func.count()).select_from(EmailLog)) == 1


async def test_the_cap_counts_only_alert_emails_the_user_was_sent(
    db_session: AsyncSession, settings: Settings, pro: int, outbox: Outbox
) -> None:
    """A test fire is sent at once and is not counted (the route's own tests
    cover it); here, a bell-only digest is not counted either."""
    await db_session.execute(
        insert(AlertDelivery).values(
            user_id=pro, kind="digest", local_at=datetime(2026, 8, 12, 9), items=1, emailed=False
        )
    )
    a = await _filer(db_session, "fund-a")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=a)
    for hour in (10, 11, 12):
        await _fire(db_session, _filed(a, "fund-a"), queued_at=ny(hour))
        await _send(db_session, settings, ny(hour, 15))

    assert len(outbox.sent) == 3
    statuses = (await db_session.execute(select(AlertDelivery.email_status))).scalars().all()
    assert Counter(statuses) == {EmailStatus.SENT.value: 3, None: 1}
