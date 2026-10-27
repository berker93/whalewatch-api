"""``/v1/me/notifications``, against a real Postgres: the bell's list, count and mark-read.

ALR-2: a cursor-paged list newest first with an unread filter, a mark-read by
ids or all, and a count kept 30 seconds that a mark-read never leaves behind.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis
from app.api.routers.notifications import COUNT_TTL, count_key
from app.db.models import AlertRule, Filer, Notification, Security
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, make_user, sign_in

T0 = datetime(2026, 11, 14, 21, tzinfo=UTC)


@pytest.fixture
def redis(app: FastAPI) -> Iterator[FakeRedis]:
    """One Redis for the whole test, so a count kept by one request is read by the next."""
    fake = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake
    yield fake


def _payload(**keys: Any) -> dict[str, Any]:
    return {
        "ticker": None,
        "filer_slug": None,
        "period": None,
        "delta": None,
        "confidence": None,
        "as_of": None,
        **keys,
    }


async def _notify(
    session: AsyncSession,
    user_id: int,
    n: int,
    *,
    at: datetime | None = None,
    read: bool = False,
) -> int:
    """The ``n``th notification, ``n`` minutes after T0 unless ``at``: a
    followed filer's 13F."""
    created = at or T0 + timedelta(minutes=n)
    notification_id = await session.scalar(
        insert(Notification)
        .values(
            user_id=user_id,
            kind="filer_filed",
            title=f"Filing {n}",
            body="",
            payload=_payload(filer_slug="berkshire-hathaway", period="2026Q3", extra=n),
            dedupe_key=f"filer_filed:berkshire-hathaway:{n}",
            created_at=created,
            read_at=created if read else None,
        )
        .returning(Notification.id)
    )
    assert notification_id is not None
    return notification_id


def _titles(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [n["title"] for n in response.json()["data"]]


async def _mark(client: AsyncClient, body: dict[str, Any]) -> Response:
    return await client.post("/v1/me/notifications/read", json=body, headers=FRONTEND)


# --- the list -------------------------------------------------------------------


async def test_none_is_an_empty_list(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.get("/v1/me/notifications")

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == []
    assert body["page"] == {"limit": 50, "next_cursor": None}
    assert body["meta"]["period"] is None
    assert response.headers["cache-control"] == "no-store"


async def test_every_route_needs_a_session(client: AsyncClient) -> None:
    for response in (
        await client.get("/v1/me/notifications"),
        await client.get("/v1/me/notifications/count"),
        await _mark(client, {"all": True}),
    ):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


async def test_newest_first_as_sent(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    first = await _notify(db_session, user_id, 1)
    await _notify(db_session, user_id, 2, read=True)
    await sign_in(client)

    response = await client.get("/v1/me/notifications")

    assert _titles(response) == ["Filing 2", "Filing 1"]
    oldest = response.json()["data"][1]
    assert oldest == {
        "id": str(first),
        "kind": "filer_filed",
        "title": "Filing 1",
        "body": "",
        # A kind's own keys come through beside the six.
        "payload": _payload(filer_slug="berkshire-hathaway", period="2026Q3", extra=1),
        "created_at": "2026-11-14T21:01:00Z",
        "read_at": None,
    }
    assert response.json()["data"][0]["read_at"] == "2026-11-14T21:02:00Z"


async def test_pages_follow_the_cursor_and_ties_break_by_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    for n in range(1, 6):
        # Two pairs written at one instant, as one transaction's batch is.
        await _notify(db_session, user_id, n, at=T0 + timedelta(minutes=(n + 1) // 2))
    await sign_in(client)

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        response = await client.get("/v1/me/notifications", params=params)
        seen += _titles(response)
        cursor = response.json()["page"]["next_cursor"]
        if cursor is None:
            break

    assert seen == ["Filing 5", "Filing 4", "Filing 3", "Filing 2", "Filing 1"]


async def test_unread_only(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await _notify(db_session, user_id, 1)
    await _notify(db_session, user_id, 2, read=True)
    await _notify(db_session, user_id, 3)
    await sign_in(client)

    response = await client.get("/v1/me/notifications", params={"unread_only": True})

    assert _titles(response) == ["Filing 3", "Filing 1"]


async def test_only_the_users_own(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    other = await make_user(db_session, "grace@example.com")
    await _notify(db_session, other, 1)
    await sign_in(client)

    assert _titles(await client.get("/v1/me/notifications")) == []


async def test_a_mangled_cursor_is_a_400(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.get("/v1/me/notifications", params={"cursor": "nonsense"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_both_lists_read_off_an_index_in_keyset_order(db_session: AsyncSession) -> None:
    """No sort node: the page is one index range, however deep."""
    user_id = await make_user(db_session)
    for n in range(1, 4):
        await _notify(db_session, user_id, n, read=n == 2)
    await db_session.execute(text("ANALYZE notification"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    for where, index in (
        ("", "ix_notification_feed"),
        ("AND read_at IS NULL", "ix_notification_unread"),
    ):
        plan = "\n".join(
            await db_session.scalars(
                text(
                    f"EXPLAIN SELECT id FROM notification WHERE user_id = :u {where} "
                    "ORDER BY created_at DESC, id DESC LIMIT 51"
                ),
                {"u": user_id},
            )
        )
        assert index in plan, plan
        assert "Sort" not in plan, plan


# --- marking read ---------------------------------------------------------------


async def test_marking_by_id_answers_the_count(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    one = await _notify(db_session, user_id, 1)
    two = await _notify(db_session, user_id, 2)
    await _notify(db_session, user_id, 3)
    await sign_in(client)

    response = await _mark(client, {"ids": [str(one), str(two)]})

    assert response.status_code == 200, response.text
    assert response.json() == {"unread": 1}
    assert _titles(await client.get("/v1/me/notifications?unread_only=true")) == ["Filing 3"]


async def test_marking_again_keeps_when_it_was_first_read(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    one = await _notify(db_session, user_id, 1, read=True)
    await sign_in(client)

    response = await _mark(client, {"ids": [str(one)]})

    assert response.status_code == 200
    read_at = await db_session.scalar(select(Notification.read_at).where(Notification.id == one))
    assert read_at == T0 + timedelta(minutes=1)


async def test_marking_all(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    other = await make_user(db_session, "grace@example.com")
    for n in range(1, 4):
        await _notify(db_session, user_id, n)
    theirs = await _notify(db_session, other, 4)
    await sign_in(client)

    response = await _mark(client, {"all": True})

    assert response.json() == {"unread": 0}
    assert (
        await db_session.scalar(select(Notification.read_at).where(Notification.id == theirs))
        is None
    )


async def test_another_users_id_is_no_change_not_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    other = await make_user(db_session, "grace@example.com")
    theirs = await _notify(db_session, other, 1)
    await sign_in(client)

    response = await _mark(client, {"ids": [str(theirs), "999999"]})

    assert response.status_code == 200
    assert response.json() == {"unread": 0}
    assert (
        await db_session.scalar(select(Notification.read_at).where(Notification.id == theirs))
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ids": ["1"], "all": True},
        {"ids": []},
        {"ids": ["one"]},
        {"ids": ["0"]},
        {"ids": ["9" * 19]},
        {"ids": [str(n) for n in range(1, 202)]},
        {"all": False},
        {"all": True, "before": "2026-11-14"},
    ],
)
async def test_a_body_that_is_not_ids_or_all_is_a_422(
    client: AsyncClient, db_session: AsyncSession, body: dict[str, Any]
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _mark(client, body)

    assert response.status_code == 422, response.text


async def test_marking_needs_no_verified_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session, verified=False)
    await _notify(db_session, user_id, 1)
    await sign_in(client)

    assert (await _mark(client, {"all": True})).json() == {"unread": 0}


# --- the count ------------------------------------------------------------------


async def test_the_count_is_the_unread(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    await _notify(db_session, user_id, 1)
    await _notify(db_session, user_id, 2, read=True)
    await _notify(db_session, user_id, 3)
    await sign_in(client)

    response = await client.get("/v1/me/notifications/count")

    assert response.status_code == 200
    assert response.json() == {"unread": 2}
    assert response.headers["cache-control"] == "no-store"
    assert redis.values[count_key(user_id)] == "2"
    assert redis.ttls[count_key(user_id)] == COUNT_TTL


async def test_the_count_is_kept_for_its_ttl(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    await _notify(db_session, user_id, 1)
    await sign_in(client)
    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 1}

    # Sent since: the badge catches up when the kept count expires.
    await _notify(db_session, user_id, 2)
    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 1}
    del redis.values[count_key(user_id)]
    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 2}


async def test_marking_read_replaces_the_kept_count(
    client: AsyncClient, db_session: AsyncSession, redis: FakeRedis
) -> None:
    user_id = await make_user(db_session)
    one = await _notify(db_session, user_id, 1)
    await _notify(db_session, user_id, 2)
    await sign_in(client)
    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 2}

    await _mark(client, {"ids": [str(one)]})

    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 1}


async def test_the_count_stops_at_a_hundred(client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    for n in range(1, 106):
        await _notify(db_session, user_id, n)
    await sign_in(client)

    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 100}


async def test_the_count_is_counted_when_redis_is_down(
    app: FastAPI, client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await _notify(db_session, user_id, 1)
    await sign_in(client)
    app.dependency_overrides[get_redis] = lambda: FakeRedis(down=True)

    assert (await client.get("/v1/me/notifications/count")).json() == {"unread": 1}
    assert (await _mark(client, {"all": True})).json() == {"unread": 0}


# --- the export -----------------------------------------------------------------


async def test_the_export_has_the_rules_and_every_notification(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    filer_id = await db_session.scalar(
        insert(Filer).values(name="BERKSHIRE", slug="berkshire-hathaway").returning(Filer.id)
    )
    security_id = await db_session.scalar(
        insert(Security)
        .values(cusip="037833100", ticker="AAPL", name="APPLE INC", resolution_source="manual")
        .returning(Security.id)
    )
    await db_session.execute(
        insert(AlertRule).values(
            [
                {
                    "user_id": user_id,
                    "kind": "filer_filed",
                    "filer_id": filer_id,
                    "security_id": None,
                    "min_value": None,
                },
                {
                    "user_id": user_id,
                    "kind": "stock_ownership_change",
                    "filer_id": None,
                    "security_id": security_id,
                    "min_value": 1_000_000,
                },
            ]
        )
    )
    await _notify(db_session, user_id, 1)
    await _notify(db_session, user_id, 2, read=True)
    await sign_in(client)

    body = (await client.get("/v1/me/export")).json()

    assert [
        {k: rule[k] for k in ("kind", "filer_slug", "cusip", "min_value", "channels")}
        for rule in body["alert_rules"]
    ] == [
        {
            "kind": "filer_filed",
            "filer_slug": "berkshire-hathaway",
            "cusip": None,
            "min_value": None,
            "channels": ["in_app"],
        },
        {
            "kind": "stock_ownership_change",
            "filer_slug": None,
            "cusip": "037833100",
            "min_value": "1000000.00",
            "channels": ["in_app"],
        },
    ]
    assert [(n["title"], n["read_at"]) for n in body["notifications"]] == [
        ("Filing 2", "2026-11-14T21:02:00Z"),
        ("Filing 1", None),
    ]
