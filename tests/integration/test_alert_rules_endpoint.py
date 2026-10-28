"""``/v1/me/alert-rules``, against a real Postgres: rules, their plan limits, presets and tests.

ALR-4: CRUD with ``alert_rules`` enforced per plan; one click from a
watchlist item; a threshold per rule; Free sent the season digest only, Pro
every kind; a test fire; and following an investor turning on its filings and
large changes, nothing else.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.alert_rules import LARGE_CHANGE
from app.api.deps import get_redis
from app.api.routers.notifications import count_key
from app.db.models import AlertRule, Filer, Insider, Notification, Security
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, Outbox, make_user, sign_in, subscribe

AAPL = "037833100"
BERKSHIRE = {"kind": "investor", "slug": "berkshire-hathaway"}
PERSHING = {"kind": "investor", "slug": "pershing-square"}
APPLE = {"kind": "stock", "cusip": AAPL}
HUANG = {"kind": "insider", "insider_cik": "0001197647"}


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, int]:
    """Two investors, a stock and an insider."""
    ids = {
        "aapl": await db_session.scalar(
            insert(Security)
            .values(cusip=AAPL, ticker="AAPL", name="APPLE INC", resolution_source="manual")
            .returning(Security.id)
        ),
    }
    for slug, display in (("berkshire-hathaway", "Berkshire Hathaway"), ("pershing-square", None)):
        ids[slug] = await db_session.scalar(
            insert(Filer)
            .values(name=slug.upper(), slug=slug, display_name=display)
            .returning(Filer.id)
        )
    await db_session.execute(insert(Insider).values(cik=HUANG["insider_cik"], name="HUANG JEN"))
    return {key: value for key, value in ids.items() if value is not None}


@pytest.fixture
async def free(client: AsyncClient, db_session: AsyncSession, world: dict[str, int]) -> int:
    user_id = await make_user(db_session)
    await sign_in(client)
    return user_id


@pytest.fixture
async def pro(client: AsyncClient, db_session: AsyncSession, world: dict[str, int]) -> int:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro")
    await sign_in(client)
    return user_id


@pytest.fixture
def redis(app: FastAPI) -> Iterator[FakeRedis]:
    """One Redis for the whole test, so the bell's kept count can be seen dropped."""
    fake = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake
    yield fake


async def _rules(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/v1/me/alert-rules")
    assert response.status_code == 200, response.text
    rules: list[dict[str, Any]] = response.json()["data"]
    return rules


async def _save(
    client: AsyncClient, kind: str, ref: dict[str, str] | None = None, **body: Any
) -> Response:
    return await client.post(
        "/v1/me/alert-rules", json={"kind": kind, "ref": ref, **body}, headers=FRONTEND
    )


async def _patch(client: AsyncClient, rule_id: str, **body: Any) -> Response:
    return await client.patch(f"/v1/me/alert-rules/{rule_id}", json=body, headers=FRONTEND)


async def _presets(client: AsyncClient, ref: dict[str, str]) -> Response:
    return await client.put("/v1/me/alert-rules/presets", json={"ref": ref}, headers=FRONTEND)


async def _test(client: AsyncClient, rule_id: str) -> Response:
    return await client.post(f"/v1/me/alert-rules/{rule_id}/test", headers=FRONTEND)


async def _follow(client: AsyncClient, ref: dict[str, str]) -> Response:
    response = await client.put("/v1/watchlist/items", json={"ref": ref}, headers=FRONTEND)
    assert response.status_code == 200, response.text
    return response


async def _unfollow(client: AsyncClient, ref: dict[str, str]) -> Response:
    response = await client.request(
        "DELETE", "/v1/watchlist/items", json={"ref": ref}, headers=FRONTEND
    )
    assert response.status_code == 200, response.text
    return response


def _summary(rules: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Each rule as (kind, target, min_value, channels, on)."""
    return [
        (
            rule["kind"],
            (rule["investor"] or {}).get("slug") or (rule["stock"] or {}).get("ticker"),
            rule["min_value"],
            rule["channels"],
            rule["is_active"],
        )
        for rule in rules
    ]


async def _keep(
    session: AsyncSession, user_id: int, kind: str, *, active: bool = True, **target: int
) -> int:
    """A rule written as a plan since left would have left it."""
    rule_id = await session.scalar(
        insert(AlertRule)
        .values(user_id=user_id, kind=kind, is_active=active, **target)
        .returning(AlertRule.id)
    )
    assert rule_id is not None
    return rule_id


# --- reading ----------------------------------------------------------------------


async def test_none_is_an_empty_list(client: AsyncClient, free: int) -> None:
    response = await client.get("/v1/me/alert-rules")

    assert response.status_code == 200
    assert response.json() == {"data": []}
    assert response.headers["cache-control"] == "no-store"


async def test_every_route_needs_a_session(client: AsyncClient) -> None:
    for response in (
        await client.get("/v1/me/alert-rules"),
        await _save(client, "season_digest"),
        await _presets(client, BERKSHIRE),
        await _patch(client, "1", is_active=False),
        await client.delete("/v1/me/alert-rules/1", headers=FRONTEND),
        await _test(client, "1"),
    ):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


async def test_writes_need_a_verified_email(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, int]
) -> None:
    await make_user(db_session, verified=False)
    await sign_in(client)

    for response in (
        await _save(client, "season_digest"),
        await _presets(client, BERKSHIRE),
        await _patch(client, "1", is_active=False),
        await client.delete("/v1/me/alert-rules/1", headers=FRONTEND),
        await _test(client, "1"),
    ):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "email_unverified"
    assert (await client.get("/v1/me/alert-rules")).status_code == 200


# --- making one -------------------------------------------------------------------


async def test_pro_makes_a_rule_with_a_threshold(client: AsyncClient, pro: int) -> None:
    response = await _save(
        client,
        "filer_position_change",
        BERKSHIRE,
        min_value="10000000",
        channels=["email", "in_app", "email"],
    )

    assert response.status_code == 201, response.text
    rule = response.json()
    assert rule["kind"] == "filer_position_change"
    assert rule["investor"] == {"slug": "berkshire-hathaway", "display_name": "Berkshire Hathaway"}
    assert rule["stock"] is None
    assert rule["min_value"] == "10000000.00"
    assert rule["channels"] == ["in_app", "email"]
    assert (rule["is_active"], rule["sent"]) == (True, True)
    assert await _rules(client) == [rule]


async def test_a_second_save_sets_every_setting_of_the_first(client: AsyncClient, pro: int) -> None:
    first = (await _save(client, "stock_ownership_change", APPLE, min_value="5000000")).json()
    await _patch(client, first["id"], is_active=False)

    again = await _save(client, "stock_ownership_change", APPLE, channels=["email"])

    assert again.status_code == 200
    rule = again.json()
    assert rule["id"] == first["id"]
    assert rule["stock"] == {"cusip": AAPL, "ticker": "AAPL", "issuer_name": "APPLE INC"}
    assert (rule["min_value"], rule["channels"], rule["is_active"]) == (None, ["email"], True)
    assert len(await _rules(client)) == 1


async def test_a_rule_names_what_its_kind_is_about(client: AsyncClient, pro: int) -> None:
    for kind, ref in (
        ("filer_filed", None),
        ("filer_filed", APPLE),
        ("insider_cluster", BERKSHIRE),
        ("stock_ownership_change", HUANG),
        ("season_digest", BERKSHIRE),
    ):
        response = await _save(client, kind, ref)
        assert response.status_code == 422, (kind, ref)
        [error] = response.json()["error"]["detail"]["errors"]
        assert error["loc"] == ["body", "ref"]

    missing = await _save(client, "filer_filed", {"kind": "investor", "slug": "nobody"})
    assert missing.status_code == 404
    assert await _rules(client) == []


async def test_only_kinds_about_dollars_take_a_threshold(client: AsyncClient, pro: int) -> None:
    for kind, ref in (("filer_filed", BERKSHIRE), ("season_digest", None)):
        response = await _save(client, kind, ref, min_value="10000")
        assert response.status_code == 422
        [error] = response.json()["error"]["detail"]["errors"]
        assert error["loc"] == ["body", "min_value"]

    for value in ("0", "-5", "1.234", "abc"):
        response = await _save(client, "insider_cluster", APPLE, min_value=value)
        assert response.status_code == 422, value

    assert (await _save(client, "insider_cluster", APPLE, min_value="250000.50")).status_code == 201


async def test_channels_are_one_at_least(client: AsyncClient, pro: int) -> None:
    for channels in ([], ["sms"]):
        response = await _save(client, "season_digest", channels=channels)
        assert response.status_code == 422, channels

    made = await _save(client, "season_digest")
    assert made.json()["channels"] == ["in_app"]


# --- what a plan allows -------------------------------------------------------------


async def test_free_is_sent_the_season_digest_only(client: AsyncClient, free: int) -> None:
    digest = await _save(client, "season_digest", channels=["in_app", "email"])
    assert digest.status_code == 201
    assert digest.json()["sent"] is True

    for kind, ref in (
        ("filer_filed", BERKSHIRE),
        ("filer_position_change", BERKSHIRE),
        ("stock_ownership_change", APPLE),
        ("insider_cluster", APPLE),
    ):
        refused = await _save(client, kind, ref)
        assert refused.status_code == 402, kind
        error = refused.json()["error"]
        assert error["code"] == "plan_limit"
        assert "season digest only" in error["message"]
        assert error["message"].endswith("Pro does.")
        detail = error["detail"]
        assert (detail["limit"], detail["allowed"], detail["needed"]) == ("alerts_instant", 0, 1)
        assert (detail["plan"]["slug"], detail["plan"]["value"]) == ("pro", 1)

    assert [rule["kind"] for rule in await _rules(client)] == ["season_digest"]


async def test_the_plans_alert_rules_count_the_rules_on(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, int]
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", overrides={"alert_rules": 2})
    await sign_in(client)

    first = (await _save(client, "filer_filed", BERKSHIRE)).json()
    await _save(client, "filer_filed", PERSHING)
    refused = await _save(client, "season_digest")

    assert refused.status_code == 402
    detail = refused.json()["error"]["detail"]
    assert (detail["limit"], detail["allowed"], detail["needed"]) == ("alert_rules", 2, 3)
    # Pro itself keeps any number: only this subscription's override is 2.
    assert (detail["plan"]["slug"], detail["plan"]["value"]) == ("pro", None)
    assert refused.json()["error"]["message"].startswith(
        "Your plan keeps 2 alert rules on at once, and 2 are on."
    )

    # One turned off makes room; turning it back on is the one too many.
    assert (await _patch(client, first["id"], is_active=False)).status_code == 200
    assert (await _save(client, "season_digest")).status_code == 201
    again = await _patch(client, first["id"], is_active=True)
    assert again.status_code == 402
    assert again.json()["error"]["detail"]["limit"] == "alert_rules"
    # A rule that is on already, set again, needs no more room.
    assert (await _save(client, "season_digest", channels=["email"])).status_code == 200


async def test_a_rule_kept_from_pro_costs_free_nothing(
    client: AsyncClient, db_session: AsyncSession, free: int, world: dict[str, int]
) -> None:
    kept = await _keep(db_session, free, "filer_filed", filer_id=world["berkshire-hathaway"])
    await _keep(db_session, free, "insider_cluster", security_id=world["aapl"])

    rules = await _rules(client)
    assert [(rule["kind"], rule["is_active"], rule["sent"]) for rule in rules] == [
        ("filer_filed", True, False),
        ("insider_cluster", True, False),
    ]
    # Free's one rule is still its digest's to take.
    assert (await _save(client, "season_digest")).status_code == 201

    off = await _patch(client, str(kept), is_active=False)
    assert off.status_code == 200
    assert (await _patch(client, str(kept), channels=["email"])).status_code == 200
    on = await _patch(client, str(kept), is_active=True)
    assert on.status_code == 402
    assert on.json()["error"]["detail"]["limit"] == "alerts_instant"


# --- changing and deleting ------------------------------------------------------------


async def test_a_change_leaves_what_it_does_not_name(client: AsyncClient, pro: int) -> None:
    made = (
        await _save(
            client, "filer_position_change", BERKSHIRE, min_value="1000", channels=["email"]
        )
    ).json()

    raised = await _patch(client, made["id"], min_value="2500000")
    assert raised.status_code == 200
    assert (raised.json()["min_value"], raised.json()["channels"]) == ("2500000.00", ["email"])

    cleared = await _patch(client, made["id"], min_value=None)
    assert cleared.json()["min_value"] is None

    off = await _patch(client, made["id"], is_active=False, channels=["in_app", "email"])
    assert (off.json()["is_active"], off.json()["channels"]) == (False, ["in_app", "email"])


async def test_a_change_must_change_something_it_can(client: AsyncClient, pro: int) -> None:
    filed = (await _save(client, "filer_filed", BERKSHIRE)).json()

    for body in ({}, {"channels": []}, {"channels": None}, {"is_active": None}, {"name": "x"}):
        assert (await _patch(client, filed["id"], **body)).status_code == 422, body
    threshold = await _patch(client, filed["id"], min_value="100")
    assert threshold.status_code == 422
    assert threshold.json()["error"]["detail"]["errors"][0]["loc"] == ["body", "min_value"]


async def test_another_users_rule_is_not_found_and_not_deleted(
    client: AsyncClient, db_session: AsyncSession, pro: int, world: dict[str, int]
) -> None:
    other = await make_user(db_session, "grace@example.com")
    theirs = str(await _keep(db_session, other, "season_digest"))

    assert (await _patch(client, theirs, is_active=False)).status_code == 404
    assert (await _test(client, theirs)).status_code == 404
    deleted = await client.delete(f"/v1/me/alert-rules/{theirs}", headers=FRONTEND)
    assert deleted.status_code == 204
    assert await db_session.get(AlertRule, int(theirs)) is not None


async def test_delete_is_idempotent_and_keeps_what_was_sent(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    made = (await _save(client, "filer_filed", BERKSHIRE)).json()
    assert (await _test(client, made["id"])).status_code == 200

    for _ in range(2):
        response = await client.delete(f"/v1/me/alert-rules/{made['id']}", headers=FRONTEND)
        assert response.status_code == 204
    assert await _rules(client) == []
    assert len((await client.get("/v1/me/notifications")).json()["data"]) == 1


# --- one click ----------------------------------------------------------------------


async def test_alert_me_about_this_investor(client: AsyncClient, pro: int) -> None:
    response = await _presets(client, BERKSHIRE)

    assert response.status_code == 200, response.text
    large = str(LARGE_CHANGE.quantize(Decimal("0.01")))
    assert _summary(response.json()["data"]) == [
        ("filer_filed", "berkshire-hathaway", None, ["in_app", "email"], True),
        ("filer_position_change", "berkshire-hathaway", large, ["in_app"], True),
    ]
    again = await _presets(client, BERKSHIRE)
    assert again.json() == response.json()


async def test_one_click_turns_back_on_with_the_users_settings(
    client: AsyncClient, pro: int
) -> None:
    [filed, changes] = (await _presets(client, BERKSHIRE)).json()["data"]
    await _patch(client, changes["id"], is_active=False, min_value="50000000")
    await _save(client, "season_digest")

    rules = (await _presets(client, BERKSHIRE)).json()["data"]

    assert _summary(rules) == [
        ("filer_filed", "berkshire-hathaway", None, ["in_app", "email"], True),
        ("filer_position_change", "berkshire-hathaway", "50000000.00", ["in_app"], True),
    ]
    assert [rule["id"] for rule in rules] == [filed["id"], changes["id"]]


async def test_alert_me_about_this_stock(client: AsyncClient, pro: int) -> None:
    rules = (await _presets(client, APPLE)).json()["data"]

    assert _summary(rules) == [
        ("stock_ownership_change", "AAPL", "1000000.00", ["in_app"], True),
        ("insider_cluster", "AAPL", None, ["in_app"], True),
    ]


async def test_one_click_refusals(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, int]
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", overrides={"alert_rules": 3})
    await sign_in(client)

    insider = await _presets(client, HUANG)
    assert insider.status_code == 422
    assert (await _presets(client, {"kind": "investor", "slug": "nobody"})).status_code == 404

    assert (await _presets(client, BERKSHIRE)).status_code == 200
    # Two more would be four on: nothing is turned on.
    full = await _presets(client, APPLE)
    assert full.status_code == 402
    detail = full.json()["error"]["detail"]
    assert (detail["limit"], detail["allowed"], detail["needed"]) == ("alert_rules", 3, 4)
    assert len(await _rules(client)) == 2


async def test_one_click_on_free_offers_pro(client: AsyncClient, free: int) -> None:
    refused = await _presets(client, BERKSHIRE)

    assert refused.status_code == 402
    assert refused.json()["error"]["detail"]["limit"] == "alerts_instant"
    assert await _rules(client) == []


# --- following ----------------------------------------------------------------------


async def test_following_an_investor_turns_on_filings_and_large_changes(
    client: AsyncClient, pro: int
) -> None:
    await _follow(client, BERKSHIRE)
    await _follow(client, APPLE)
    await _follow(client, HUANG)

    assert _summary(await _rules(client)) == [
        ("filer_filed", "berkshire-hathaway", None, ["in_app", "email"], True),
        ("filer_position_change", "berkshire-hathaway", "1000000.00", ["in_app"], True),
    ]


async def test_unfollowing_turns_off_and_following_again_turns_on(
    client: AsyncClient, pro: int
) -> None:
    await _follow(client, BERKSHIRE)
    [_, changes] = await _rules(client)
    await _patch(client, changes["id"], min_value="20000000")
    await _presets(client, APPLE)

    await _unfollow(client, BERKSHIRE)
    await _unfollow(client, APPLE)
    assert [rule["is_active"] for rule in await _rules(client)] == [False] * 4

    await _follow(client, BERKSHIRE)
    assert _summary(await _rules(client)) == [
        ("filer_filed", "berkshire-hathaway", None, ["in_app", "email"], True),
        ("filer_position_change", "berkshire-hathaway", "20000000.00", ["in_app"], True),
        ("stock_ownership_change", "AAPL", "1000000.00", ["in_app"], False),
        ("insider_cluster", "AAPL", None, ["in_app"], False),
    ]


async def test_following_on_free_makes_the_digest_once(client: AsyncClient, free: int) -> None:
    await _follow(client, BERKSHIRE)
    [digest] = await _rules(client)
    assert _summary([digest]) == [("season_digest", None, None, ["in_app", "email"], True)]

    # Turned off by the user, it stays off.
    await _patch(client, digest["id"], is_active=False)
    await _follow(client, PERSHING)
    assert [rule["is_active"] for rule in await _rules(client)] == [False]


async def test_following_past_the_plans_rules_still_follows(
    client: AsyncClient, db_session: AsyncSession, world: dict[str, int]
) -> None:
    user_id = await make_user(db_session)
    await subscribe(db_session, user_id, "pro", overrides={"alert_rules": 1})
    await sign_in(client)

    response = await _follow(client, BERKSHIRE)

    assert [i["slug"] for i in response.json()["investors"]] == ["berkshire-hathaway"]
    assert _summary(await _rules(client)) == [
        ("filer_filed", "berkshire-hathaway", None, ["in_app", "email"], True),
    ]


# --- the test fire --------------------------------------------------------------------


async def test_a_test_alert_rings_the_bell_and_emails(
    client: AsyncClient,
    db_session: AsyncSession,
    pro: int,
    outbox: Outbox,
    redis: FakeRedis,
) -> None:
    rule = (await _save(client, "filer_filed", BERKSHIRE, channels=["in_app", "email"])).json()
    await redis.set(count_key(pro), "0")

    response = await _test(client, rule["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "sent"
    sent = body["notification"]
    assert sent["kind"] == "filer_filed"
    assert sent["title"] == "Test alert: New 13F filings by Berkshire Hathaway"
    assert sent["read_at"] is None
    assert sent["payload"]["filer_slug"] == "berkshire-hathaway"
    assert sent["payload"]["test"] is True
    assert sent["payload"]["ticker"] is None
    [listed] = (await client.get("/v1/me/notifications")).json()["data"]
    assert listed == sent
    assert await redis.get(count_key(pro)) is None

    [mail] = outbox.sent
    assert mail.subject == sent["title"]
    assert "/investors/berkshire-hathaway" in mail.body
    assert mail.unsubscribe is not None
    assert mail.unsubscribe.mailing_list.id == f"alert_rule:{rule['id']}"


async def test_a_test_of_a_bell_only_rule_sends_no_email(
    client: AsyncClient, pro: int, outbox: Outbox
) -> None:
    rule = (await _save(client, "insider_cluster", APPLE)).json()

    response = await _test(client, rule["id"])

    assert response.json()["email"] is None
    assert response.json()["notification"]["payload"]["ticker"] == "AAPL"
    assert outbox.sent == []


async def test_one_test_a_minute_per_rule(
    client: AsyncClient, pro: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = datetime(2026, 10, 28, 9, 30, 40, tzinfo=UTC)
    monkeypatch.setattr("app.api.routers.alert_rules.utc_now", lambda: at)
    filed = (await _save(client, "filer_filed", BERKSHIRE)).json()
    digest = (await _save(client, "season_digest")).json()

    assert (await _test(client, filed["id"])).status_code == 200
    again = await _test(client, filed["id"])
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "rate_limited"
    assert again.headers["retry-after"] == "21"
    assert (await _test(client, digest["id"])).status_code == 200

    monkeypatch.setattr("app.api.routers.alert_rules.utc_now", lambda: at + timedelta(seconds=20))
    assert (await _test(client, filed["id"])).status_code == 200


async def test_a_test_needs_a_rule_that_is_on_and_sent(
    client: AsyncClient, db_session: AsyncSession, free: int, world: dict[str, int]
) -> None:
    kept = await _keep(db_session, free, "filer_filed", filer_id=world["berkshire-hathaway"])
    digest = (await _save(client, "season_digest")).json()

    assert (await _test(client, str(kept))).status_code == 402
    await _patch(client, digest["id"], is_active=False)
    off = await _test(client, digest["id"])
    assert off.status_code == 422
    assert off.json()["error"]["detail"]["errors"][0]["loc"] == ["path", "rule_id"]

    await _patch(client, digest["id"], is_active=True)
    tested = await _test(client, digest["id"])
    assert tested.status_code == 200
    assert tested.json()["notification"]["title"] == "Test alert: The 13F season digest"
    rows = (await db_session.scalars(select(Notification.dedupe_key))).all()
    assert [key.split(":")[:3] for key in rows] == [["season_digest", "test", digest["id"]]]


async def test_a_rule_turned_off_by_unsubscribe_reads_off(
    client: AsyncClient, db_session: AsyncSession, pro: int
) -> None:
    rule = (await _save(client, "filer_filed", BERKSHIRE, channels=["email"])).json()
    await db_session.execute(
        update(AlertRule).where(AlertRule.id == int(rule["id"])).values(is_active=False)
    )

    [listed] = await _rules(client)
    assert (listed["is_active"], listed["channels"]) == (False, ["email"])
