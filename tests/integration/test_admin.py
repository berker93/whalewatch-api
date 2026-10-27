"""TIER-7, against a real Postgres: the admin console's users, overrides and numbers.

Admins sign in through ``/admin/auth/login`` with an account of their own
(``admin_user``), and their sessions are kept in the same fake Redis as the
users', under other keys. What a test asserts about a change is what the
account sees (``GET /v1/me``), what the console shows, and the log.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import entitlements
from app.accounts.api_keys import find_key, new_key
from app.accounts.passwords import hash_password_sync
from app.accounts.sessions import SessionStore, digest
from app.admin import metrics
from app.admin.auth import (
    ADMIN_SESSION_PREFIX,
    AdminError,
    admin_store,
    create_admin,
    disable_admin,
)
from app.admin.users import Subject
from app.core.config import Settings
from app.db.models import (
    AdminAction,
    AdminUser,
    ApiKey,
    AppUser,
    MetricDay,
    Plan,
    PlanEntitlement,
    PlanOverride,
    SavedFilter,
    Subscription,
    UsageCounter,
    UserActivityDay,
)
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, PASSWORD, make_user, sign_in, subscribe

ADMIN = "ops@whalewatch.io"
LATER = "2099-01-01T00:00:00Z"


@pytest.fixture
def admins(
    app: FastAPI, client: AsyncClient, session_redis: FakeRedis, settings: Settings
) -> SessionStore:
    """The admins' sessions, beside the users' in the test's Redis, as the lifespan builds them."""
    store = admin_store(session_redis, settings)  # type: ignore[arg-type]
    app.state.admin_sessions = store
    return store


async def make_admin(session: AsyncSession, email: str = ADMIN, *, disabled: bool = False) -> int:
    admin_id = await session.scalar(
        insert(AdminUser)
        .values(
            email=email,
            name="Grace Hopper",
            password_hash=hash_password_sync(PASSWORD),
            disabled_at=func.now() if disabled else None,
        )
        .returning(AdminUser.id)
    )
    assert admin_id is not None
    return admin_id


async def admin_in(client: AsyncClient, email: str = ADMIN, password: str = PASSWORD) -> Response:
    return await client.post(
        "/admin/auth/login", json={"email": email, "password": password}, headers=FRONTEND
    )


@pytest.fixture
async def admin(client: AsyncClient, db_session: AsyncSession, admins: SessionStore) -> int:
    """An admin, signed in to the console."""
    admin_id = await make_admin(db_session)
    response = await admin_in(client)
    assert response.status_code == 200, response.text
    return admin_id


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _actions(session: AsyncSession) -> list[tuple[str, int | None, int | None]]:
    rows = await session.execute(
        select(AdminAction.action, AdminAction.admin_id, AdminAction.user_id).order_by(
            AdminAction.id
        )
    )
    return [tuple(row) for row in rows]


async def _plan_id(session: AsyncSession, slug: str) -> int:
    plan_id = await session.scalar(select(Plan.id).where(Plan.slug == slug))
    assert plan_id is not None
    return plan_id


async def _me_plan(client: AsyncClient) -> str:
    response = await client.get("/v1/me")
    assert response.status_code == 200, response.text
    plan: str = response.json()["plan"]["id"]
    return plan


# --- separate admin auth --------------------------------------------------------------


async def test_an_admin_signs_in_with_a_cookie_of_its_own_on_admin_only(
    client: AsyncClient, db_session: AsyncSession, admins: SessionStore
) -> None:
    admin_id = await make_admin(db_session)

    response = await admin_in(client)

    assert response.status_code == 200, response.text
    assert response.json()["email"] == ADMIN
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("ww_admin=")
    assert "Path=/admin" in cookie
    assert "SameSite=strict" in cookie
    assert "HttpOnly" in cookie
    assert "Domain" not in cookie
    assert "Max-Age" not in cookie
    assert await _actions(db_session) == [("login", admin_id, None)]
    assert (await client.get("/admin/auth/session")).json()["session"]["email"] == ADMIN


async def test_a_user_session_is_not_an_admin_session(
    client: AsyncClient, db_session: AsyncSession, admins: SessionStore
) -> None:
    await make_user(db_session)
    await sign_in(client)

    assert _error(await client.get("/admin/users")) == (401, "unauthenticated")


async def test_a_users_credentials_do_not_sign_in_to_the_console(
    client: AsyncClient, db_session: AsyncSession, admins: SessionStore
) -> None:
    await make_user(db_session, ADMIN)

    assert _error(await admin_in(client)) == (403, "invalid_credentials")
    assert await _actions(db_session) == []


async def test_an_admin_session_is_not_a_user_session(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    """Even when the admin's address has a user account too: two identities."""
    await make_user(db_session, ADMIN)

    assert _error(await client.get("/v1/me")) == (401, "unauthenticated")


async def test_the_admin_cookie_names_no_user_session(
    client: AsyncClient, db_session: AsyncSession, admin: int, session_redis: FakeRedis
) -> None:
    """The same token, sent as a user's cookie, names nothing in the users' keys."""
    token = client.cookies.get("ww_admin", path="/admin")
    assert token is not None
    client.cookies.set("ww_session", token)

    assert _error(await client.get("/v1/me")) == (401, "session_expired")


async def test_a_wrong_password_and_an_unknown_admin_answer_alike(
    client: AsyncClient, db_session: AsyncSession, admins: SessionStore
) -> None:
    await make_admin(db_session)

    assert _error(await admin_in(client, password="wrong password 1")) == (
        403,
        "invalid_credentials",
    )
    assert _error(await admin_in(client, email="nobody@whalewatch.io")) == (
        403,
        "invalid_credentials",
    )


async def test_a_disabled_admin_cannot_sign_in(
    client: AsyncClient, db_session: AsyncSession, admins: SessionStore
) -> None:
    await make_admin(db_session, disabled=True)

    assert _error(await admin_in(client)) == (403, "invalid_credentials")


async def test_disabling_an_admin_ends_their_sessions_at_once(
    client: AsyncClient, db_session: AsyncSession, admin: int, admins: SessionStore
) -> None:
    await disable_admin(db_session, admins, ADMIN)

    assert _error(await client.get("/admin/users")) == (401, "session_expired")
    assert ("admin_disabled", None, None) in await _actions(db_session)


async def test_signing_out_ends_the_session_and_is_logged(
    client: AsyncClient, db_session: AsyncSession, admin: int, session_redis: FakeRedis
) -> None:
    token = client.cookies.get("ww_admin", path="/admin")

    response = await client.post("/admin/auth/logout", headers=FRONTEND)

    assert response.status_code == 204
    assert token is not None
    assert await session_redis.get(ADMIN_SESSION_PREFIX + digest(token)) is None
    assert [action for action, _, _ in await _actions(db_session)] == ["login", "logout"]


async def test_create_admin_refuses_an_address_ever_used(db_session: AsyncSession) -> None:
    await make_admin(db_session, disabled=True)

    with pytest.raises(AdminError, match="is an admin already, or was one"):
        await create_admin(db_session, ADMIN.upper(), "Grace Hopper", PASSWORD)


async def test_create_admin_is_logged_from_the_cli(db_session: AsyncSession) -> None:
    made = await create_admin(db_session, "  New@WhaleWatch.io ", "Ada Admin", PASSWORD)

    [row] = (await db_session.scalars(select(AdminAction))).all()
    assert (row.action, row.source, row.admin_id, row.target_admin_id) == (
        "admin_created",
        "cli",
        None,
        made.id,
    )
    assert row.detail == {}


# --- the admin log ------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE admin_action SET action = 'logout'",
        "DELETE FROM admin_action",
        "TRUNCATE admin_action",
    ],
)
async def test_the_log_is_append_only(db_session: AsyncSession, statement: str) -> None:
    await create_admin(db_session, "new@whalewatch.io", "Ada Admin", PASSWORD)

    with pytest.raises(DBAPIError, match="admin_action is append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))

    assert await db_session.scalar(select(func.count()).select_from(AdminAction)) == 1


async def test_the_log_lists_newest_first_and_pages(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    user_id = await make_user(db_session)
    for days in (10, 20):
        await _override(client, user_id, "pro", expires_at=_ahead(days))

    first = (await client.get("/admin/actions", params={"limit": 2})).json()
    rest = (
        await client.get("/admin/actions", params={"limit": 2, "before": first["next_before"]})
    ).json()

    assert [a["action"] for a in first["items"]] == ["override_set", "override_set"]
    assert [a["action"] for a in rest["items"]] == ["login"]
    assert rest["next_before"] is None
    about = (await client.get("/admin/actions", params={"user_id": user_id})).json()
    assert len(about["items"]) == 2


# --- the user list ------------------------------------------------------------------


async def _users(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/admin/users", params=params)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


def _emails(page: dict[str, Any]) -> list[str]:
    return [row["email"] for row in page["items"]]


async def test_the_list_shows_email_plan_signup_and_last_seen(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session, "ada@example.com")
    bob = await make_user(db_session, "bob@example.com")
    await subscribe(db_session, bob, "pro")
    seen = datetime(2026, 10, 1, 12, tzinfo=UTC)
    await db_session.execute(update(AppUser).where(AppUser.id == ada).values(last_seen_at=seen))

    page = await _users(client)

    assert page["total"] == 2
    rows = {row["email"]: row for row in page["items"]}
    assert rows["ada@example.com"]["plan"] == "free"
    assert rows["ada@example.com"]["last_seen_at"] == "2026-10-01T12:00:00Z"
    assert rows["bob@example.com"]["plan"] == rows["bob@example.com"]["billed_plan"] == "pro"
    assert rows["bob@example.com"]["subscription_status"] == "active"
    assert rows["bob@example.com"]["last_seen_at"] is None
    assert rows["bob@example.com"]["created_at"] is not None


async def test_last_seen_is_the_later_of_a_session_and_an_api_key(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await db_session.execute(
        update(AppUser)
        .where(AppUser.id == ada)
        .values(last_seen_at=datetime(2026, 9, 1, tzinfo=UTC))
    )
    db_session.add(
        ApiKey(
            user_id=ada,
            name="nightly",
            prefix="ww_abcdefgh",
            token_hash="0" * 64,
            last_used_at=datetime(2026, 10, 2, tzinfo=UTC),
        )
    )
    await db_session.flush()

    [row] = (await _users(client))["items"]

    assert row["last_seen_at"] == "2026-10-02T00:00:00Z"


async def test_the_list_filters(client: AsyncClient, db_session: AsyncSession, admin: int) -> None:
    ada = await make_user(db_session, "ada@example.com", name="Ada Lovelace")
    bob = await make_user(db_session, "bob@example.com", name="Bob Barker", verified=False)
    cy = await make_user(db_session, "cy@example.com", name="Cy Young")
    gone = await make_user(db_session, "gone@example.com")
    await subscribe(db_session, bob, "pro")
    await db_session.execute(
        update(AppUser).where(AppUser.id == gone).values(deleted_at=func.now())
    )
    await db_session.execute(
        update(AppUser)
        .where(AppUser.id == ada)
        .values(created_at=datetime(2026, 1, 15, tzinfo=UTC))
    )
    await db_session.execute(
        update(AppUser)
        .where(AppUser.id == cy)
        .values(last_seen_at=datetime(2026, 10, 20, 23, 59, tzinfo=UTC))
    )
    await _override(client, cy, "api_pro")

    assert _emails(await _users(client, q="LOVE")) == ["ada@example.com"]
    assert _emails(await _users(client, q="%")) == []
    assert _emails(await _users(client, plan="pro")) == ["bob@example.com"]
    assert _emails(await _users(client, plan="api_pro")) == ["cy@example.com"]
    assert sorted(_emails(await _users(client, plan="free"))) == ["ada@example.com"]
    assert _emails(await _users(client, status="unverified")) == ["bob@example.com"]
    assert _emails(await _users(client, status="deleted")) == ["gone@example.com"]
    assert (await _users(client, status="all"))["total"] == 4
    assert _emails(await _users(client, comped=True)) == ["cy@example.com"]
    assert _emails(
        await _users(client, signed_up_from="2026-01-15", signed_up_to="2026-01-15")
    ) == ["ada@example.com"]
    assert _emails(await _users(client, seen_from="2026-10-20", seen_to="2026-10-20")) == [
        "cy@example.com"
    ]


async def test_the_list_sorts_and_pages_with_a_total(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    for email in ("a@example.com", "b@example.com", "c@example.com"):
        await make_user(db_session, email)
    await db_session.execute(
        update(AppUser)
        .where(AppUser.email == "b@example.com")
        .values(last_seen_at=datetime(2026, 10, 1, tzinfo=UTC))
    )

    first = await _users(client, sort="email", order="asc", limit=2)
    second = await _users(client, sort="email", order="asc", limit=2, offset=2)
    seen = await _users(client, sort="last_seen")

    assert (_emails(first), first["total"]) == (["a@example.com", "b@example.com"], 3)
    assert _emails(second) == ["c@example.com"]
    # Never seen goes last, whichever way.
    assert _emails(seen)[0] == "b@example.com"


async def test_the_list_needs_an_admin(client: AsyncClient, admins: SessionStore) -> None:
    assert _error(await client.get("/admin/users")) == (401, "unauthenticated")


# --- overrides ------------------------------------------------------------------------


def _ahead(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def _override(
    client: AsyncClient,
    user_id: int,
    plan: str,
    *,
    reason: str = "beta tester",
    expires_at: str = LATER,
) -> Response:
    response = await client.post(
        f"/admin/users/{user_id}/override",
        json={"plan": plan, "reason": reason, "expires_at": expires_at},
        headers=FRONTEND,
    )
    return response


async def test_an_override_reaches_the_users_next_request(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    """The user signed in before it: their session's grant is read again at once."""
    ada = await make_user(db_session)
    await sign_in(client)
    assert await _me_plan(client) == "free"

    response = await _override(client, ada, "pro", reason="  friend of the founders ")

    assert response.status_code == 201, response.text
    override = response.json()["override"]
    assert (override["plan"], override["reason"], override["live"]) == (
        "pro",
        "friend of the founders",
        True,
    )
    assert override["created_by"] == ADMIN
    assert await _me_plan(client) == "pro"
    me = (await client.get("/v1/me")).json()
    assert me["entitlements"]["export_enabled"] is True
    [row] = (
        await db_session.scalars(select(AdminAction).where(AdminAction.action == "override_set"))
    ).all()
    assert (row.admin_id, row.user_id, row.source) == (admin, ada, "console")
    assert row.detail["plan"] == "pro"
    assert row.detail["reason"] == "friend of the founders"
    assert row.detail["expires_at"] == "2099-01-01T00:00:00+00:00"
    assert row.request_id is not None


async def test_an_override_leaves_the_subscription_alone(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    subscription_id = await subscribe(db_session, ada, "pro")

    assert (await _override(client, ada, "api_pro")).status_code == 201

    subscription = await db_session.get(Subscription, subscription_id)
    assert subscription is not None
    await db_session.refresh(subscription)
    assert (subscription.plan_id, subscription.status) == (
        await _plan_id(db_session, "pro"),
        "active",
    )
    [row] = (await _users(client))["items"]
    assert (row["plan"], row["billed_plan"], row["override_plan"]) == ("api_pro", "pro", "api_pro")


async def test_a_new_override_ends_the_one_before(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    first = (await _override(client, ada, "pro")).json()["override"]["id"]

    response = await _override(client, ada, "api_pro")

    assert response.json()["replaced"] == [first]
    ended = await db_session.get(PlanOverride, first)
    assert ended is not None
    await db_session.refresh(ended)
    assert (ended.ended_at is not None, ended.ended_by) == (True, admin)
    grant = await entitlements.grant_for(db_session, ada)
    assert grant is not None and grant.override == "api_pro"


async def test_ending_an_override_puts_the_user_back(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await sign_in(client)
    await _override(client, ada, "pro")

    response = await client.post(
        f"/admin/users/{ada}/override/end", json={"reason": "beta over"}, headers=FRONTEND
    )

    assert response.status_code == 200, response.text
    assert await _me_plan(client) == "free"
    again = await client.post(f"/admin/users/{ada}/override/end", json={}, headers=FRONTEND)
    assert _error(again) == (404, "not_found")


async def test_an_override_ends_on_its_own_at_its_expiry(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await _override(client, ada, "pro")
    await db_session.execute(
        update(PlanOverride)
        .where(PlanOverride.user_id == ada)
        .values(
            starts_at=datetime.now(UTC) - timedelta(days=2),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    assert await entitlements.grant_for(db_session, ada) is None
    [row] = (await _users(client))["items"]
    assert (row["plan"], row["override_plan"]) == ("free", None)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"plan": "pro", "reason": " a ", "expires_at": LATER}, "validation_error"),
        (
            {"plan": "pro", "reason": "beta", "expires_at": "2020-01-01T00:00:00Z"},
            "validation_error",
        ),
        (
            {"plan": "pro", "reason": "beta", "expires_at": "2099-01-01T00:00:00"},
            "validation_error",
        ),
        ({"plan": "platinum", "reason": "beta", "expires_at": LATER}, "unknown_plan"),
        ({"plan": "pro", "reason": "beta"}, "validation_error"),
    ],
)
async def test_an_override_needs_a_reason_an_expiry_ahead_and_a_plan(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: int,
    body: dict[str, Any],
    expected: str,
) -> None:
    ada = await make_user(db_session)

    response = await client.post(f"/admin/users/{ada}/override", json=body, headers=FRONTEND)

    assert _error(response) == (422, expected)
    assert await db_session.scalar(select(func.count()).select_from(PlanOverride)) == 0


async def test_a_deleted_account_gets_no_override(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await db_session.execute(update(AppUser).where(AppUser.id == ada).values(deleted_at=func.now()))

    assert _error(await _override(client, ada, "pro")) == (404, "not_found")
    assert _error(await _override(client, 999_999, "pro")) == (404, "not_found")


async def test_an_override_is_in_the_users_data_export(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await _override(client, ada, "pro", reason="beta tester")
    await sign_in(client)

    export = (await client.get("/v1/me/export")).json()

    [given] = export["plan_overrides"]
    assert (given["plan"], given["reason"], given["ended_at"]) == ("pro", "beta tester", None)


# --- grandfathering ---------------------------------------------------------------------


async def _grandfather(client: AsyncClient, user_id: int) -> Response:
    return await client.post(
        f"/admin/users/{user_id}/grandfather", json={"reason": "price change"}, headers=FRONTEND
    )


async def test_grandfathering_pins_todays_terms_through_a_plan_change(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session, "ada@example.com")
    bob = await make_user(db_session, "bob@example.com")
    subscription_id = await subscribe(db_session, ada, "pro")
    await subscribe(db_session, bob, "pro")
    pro = await _plan_id(db_session, "pro")
    before = await db_session.scalar(
        select(PlanEntitlement.value).where(
            PlanEntitlement.plan_id == pro, PlanEntitlement.key == "history_quarters"
        )
    )

    response = await _grandfather(client, ada)

    assert response.status_code == 200, response.text
    snapshot = response.json()["snapshot"]
    assert response.json() == {
        "plan": "pro",
        "snapshot": f"pro_{datetime.now(UTC):%Y%m%d}",
        "snapshot_created": True,
    }
    copied = await db_session.scalar(select(Plan).where(Plan.slug == snapshot))
    assert copied is not None
    assert (copied.is_public, copied.snapshot_of_plan_id) == (False, pro)
    subscription = await db_session.get(Subscription, subscription_id)
    assert subscription is not None
    await db_session.refresh(subscription)
    assert subscription.grandfathered_plan_id == copied.id

    # Pro gets more for everyone else; Ada keeps what she had.
    await db_session.execute(
        update(PlanEntitlement)
        .where(PlanEntitlement.plan_id == pro, PlanEntitlement.key == "history_quarters")
        .values(value=func.to_jsonb(40))
    )
    entitlements.install(await entitlements.load_catalog(db_session))
    for user_id, expected in ((ada, before), (bob, 40)):
        user = await db_session.get(AppUser, user_id)
        assert user is not None
        subject = Subject(user=user, grant=await entitlements.grant_for(db_session, user_id))
        assert entitlements.limit(subject, "history_quarters") == expected
    [row] = (
        await db_session.scalars(select(AdminAction).where(AdminAction.action == "grandfathered"))
    ).all()
    assert row.detail["snapshot"] == snapshot and row.detail["snapshot_created"] is True


async def test_a_snapshot_with_the_same_terms_is_reused(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    for email in ("ada@example.com", "bob@example.com"):
        await subscribe(db_session, await make_user(db_session, email), "pro")
    ada, bob = (await db_session.scalars(select(AppUser.id).order_by(AppUser.id))).all()

    first, second = (
        (await _grandfather(client, ada)).json(),
        (await _grandfather(client, bob)).json(),
    )

    assert first["snapshot"] == second["snapshot"]
    assert (first["snapshot_created"], second["snapshot_created"]) == (True, False)


async def test_grandfathering_needs_a_live_subscription_and_happens_once(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    free = await make_user(db_session, "free@example.com")
    paying = await make_user(db_session, "pro@example.com")
    await subscribe(db_session, paying, "pro")

    assert _error(await _grandfather(client, free)) == (409, "no_subscription")
    assert (await _grandfather(client, paying)).status_code == 200
    assert _error(await _grandfather(client, paying)) == (409, "already_grandfathered")


async def test_ungrandfathering_puts_the_subscription_on_current_terms(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await subscribe(db_session, ada, "pro")
    snapshot = (await _grandfather(client, ada)).json()["snapshot"]

    response = await client.post(f"/admin/users/{ada}/grandfather/end", json={}, headers=FRONTEND)

    assert response.json() == {"snapshot": snapshot}
    grant = await entitlements.grant_for(db_session, ada)
    assert grant is not None and grant.grandfathered is None
    again = await client.post(f"/admin/users/{ada}/grandfather/end", json={}, headers=FRONTEND)
    assert _error(again) == (404, "not_found")


async def test_the_plans_list_shows_snapshots(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await subscribe(db_session, ada, "pro")
    snapshot = (await _grandfather(client, ada)).json()["snapshot"]

    plans = {plan["slug"]: plan for plan in (await client.get("/admin/plans")).json()}

    assert {"free", "pro", snapshot} <= plans.keys()
    assert plans[snapshot]["snapshot_of"] == "pro"
    assert plans[snapshot]["values"] == plans["pro"]["values"]


# --- one user's page --------------------------------------------------------------------


async def test_a_users_page_has_their_usage(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    ada = await make_user(db_session)
    await subscribe(db_session, ada, "api_hobbyist")
    db_session.add(SavedFilter(user_id=ada, name="big buys", query={"direction": "buy"}))
    db_session.add(
        UsageCounter(user_id=ada, key="api_calls_month", period=date(2026, 9, 1), count=812)
    )
    await db_session.flush()
    await sign_in(client)
    for _ in range(2):
        assert (await client.get("/v1/me/export")).status_code == 200

    response = await client.get(f"/admin/users/{ada}")

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["account"]["plan"] == "api_hobbyist"
    assert page["entitlements"]["api_calls_month"] == 10_000
    usage = page["usage"]
    assert usage["saved_filters"] == {"count": 1, "limit": None}
    assert usage["watchlist_items"]["count"] == 0
    assert usage["data_exports"]["total"] == 2
    assert usage["data_exports"]["last_on"] == datetime.now(UTC).date().isoformat()
    assert usage["active_days_30"] == 1
    assert usage["months"][0]["count"] == 812
    assert usage["quotas"]["api_calls_month"]["limit"] == 10_000
    [subscription] = page["subscriptions"]
    assert subscription["plan"] == "api_hobbyist"
    assert [a["action"] for a in page["actions"]] == []


async def test_an_unknown_account_has_no_page(client: AsyncClient, admin: int) -> None:
    assert _error(await client.get("/admin/users/999999")) == (404, "not_found")


# --- activity ----------------------------------------------------------------------------


async def test_signing_in_and_an_api_key_mark_the_day_active(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ada = await make_user(db_session, "ada@example.com")
    bob = await make_user(db_session, "bob@example.com")
    await sign_in(client)
    key = new_key()
    db_session.add(ApiKey(user_id=bob, name="job", prefix=key[:11], token_hash=digest(key)))
    await db_session.flush()

    assert await find_key(db_session, key) is not None

    days = (await db_session.execute(select(UserActivityDay.user_id, UserActivityDay.day))).all()
    today = datetime.now(UTC).date()
    assert sorted(days) == [(ada, today), (bob, today)]


# --- metrics -----------------------------------------------------------------------------


async def test_the_snapshot_writes_each_day_once_with_the_mix_only_for_yesterday(
    db_session: AsyncSession, admins: SessionStore
) -> None:
    today = datetime.now(UTC).date()
    ada = await make_user(db_session, "ada@example.com")
    bob = await make_user(db_session, "bob@example.com")
    await subscribe(db_session, bob, "pro")
    three_days_ago = datetime.now(UTC) - timedelta(days=3)
    await db_session.execute(
        update(AppUser).where(AppUser.id == ada).values(created_at=three_days_ago)
    )
    for user_id, days_ago in ((ada, 3), (ada, 1), (bob, 1)):
        db_session.add(UserActivityDay(user_id=user_id, day=today - timedelta(days=days_ago)))
    await db_session.flush()

    written = await metrics.snapshot(db_session, today)
    again = await metrics.snapshot(db_session, today)

    assert written == [today - timedelta(days=d) for d in (3, 2, 1)]
    assert again == []
    rows = {row.day: row for row in await db_session.scalars(select(MetricDay))}
    oldest, yesterday = rows[today - timedelta(days=3)], rows[today - timedelta(days=1)]
    assert (oldest.signups, oldest.dau, oldest.plan_mix) == (1, 1, None)
    assert (yesterday.dau, yesterday.wau, yesterday.mau) == (2, 2, 2)
    assert yesterday.plan_mix == {"free": 1, "pro": 1}
    assert (yesterday.accounts, yesterday.comped) == (2, 0)


async def test_the_series_groups_by_week_with_today_live(
    client: AsyncClient, db_session: AsyncSession, admin: int
) -> None:
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    last_week = monday - timedelta(days=7)
    for offset, signups in ((0, 2), (1, 3)):
        db_session.add(
            MetricDay(
                day=last_week + timedelta(days=offset),
                signups=signups,
                deletions=0,
                dau=5,
                wau=9,
                mau=20,
                accounts=40,
                plan_mix={"free": 38, "pro": 2},
                comped=1,
            )
        )
    await make_user(db_session)  # signed up today
    await db_session.flush()

    response = await client.get(
        "/admin/metrics", params={"every": "week", "start": last_week.isoformat()}
    )

    assert response.status_code == 200, response.text
    first, current = response.json()["points"]
    assert (first["start"], first["signups"], first["plan_mix"]) == (
        last_week.isoformat(),
        5,
        {"free": 38, "pro": 2},
    )
    # Days 3 to 7 of last week have no row: its active is not its last day's.
    assert (first["active"], first["partial"]) == (None, True)
    assert (current["end"], current["signups"], current["partial"]) == (
        today.isoformat(),
        1,
        True,
    )
    assert current["plan_mix"] == {"free": 1}


async def test_the_series_refuses_a_range_backwards_or_too_wide(
    client: AsyncClient, admin: int
) -> None:
    backwards = await client.get(
        "/admin/metrics", params={"start": "2026-10-10", "end": "2026-10-01"}
    )
    wide = await client.get("/admin/metrics", params={"start": "2020-01-01", "end": "2026-10-01"})

    assert _error(backwards) == (422, "validation_error")
    assert _error(wide) == (422, "validation_error")
