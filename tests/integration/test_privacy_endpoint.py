"""Privacy, against a real Postgres: terms at registration, the lawful-basis ledger,
the export, and deleting an account until its purge.

The checklist this answers: terms accepted at registration, marketing consent
as a box of its own and off by default, a basis recorded per purpose, a
JSON export of everything held, and deletion as a soft delete, a purge
thirty days on, and an audit row that survives it.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import privacy
from app.accounts.sessions import SessionStore, now
from app.db.models import (
    AccountDeletion,
    AccountToken,
    AppUser,
    ConsentRecord,
    ConsentSource,
    DeletionRequester,
    Filer,
    SignupRequest,
    Subscription,
    SubscriptionEntitlement,
    WatchlistItem,
)
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis
from tests.integration.accounts import FRONTEND, PASSWORD, Outbox, make_user, sign_in, subscribe


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _register(client: AsyncClient, **body: Any) -> Response:
    return await client.post(
        "/v1/auth/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "password": PASSWORD, **body},
        headers=FRONTEND,
    )


async def _ledger(session: AsyncSession, user_id: int) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        select(
            ConsentRecord.purpose,
            ConsentRecord.basis,
            ConsentRecord.granted,
            ConsentRecord.terms_version,
            ConsentRecord.source,
        )
        .where(ConsentRecord.user_id == user_id)
        .order_by(ConsentRecord.id)
    )
    return [tuple(row) for row in rows]


async def _user_id(session: AsyncSession, email: str = "ada@example.com") -> int:
    user_id = await session.scalar(
        select(AppUser.id).where(AppUser.email == email, AppUser.deleted_at.is_(None))
    )
    assert user_id is not None
    return user_id


# --- registration -----------------------------------------------------------------


@pytest.mark.parametrize("accept", [None, False])
async def test_registering_without_accepting_the_terms_is_refused(
    client: AsyncClient, db_session: AsyncSession, accept: bool | None
) -> None:
    body = {} if accept is None else {"accept_terms": accept}

    response = await _register(client, **body)

    assert _error(response) == (422, "terms_not_accepted")
    [error] = response.json()["error"]["detail"]["errors"]
    assert error["loc"] == ["body", "accept_terms"]
    assert await db_session.scalar(select(func.count()).select_from(SignupRequest)) == 0


async def test_an_open_registration_records_a_basis_for_each_purpose(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(signups_open=True)

    response = await _register(client, accept_terms=True)

    assert response.status_code == 202, response.text
    assert await _ledger(db_session, await _user_id(db_session)) == [
        ("account", "contract", True, privacy.TERMS_VERSION, "register"),
        ("product_email", "legitimate_interest", True, None, "register"),
        # Left unticked, as the box is unless the user ticks it.
        ("marketing_email", "consent", False, None, "register"),
    ]


async def test_marketing_consent_is_the_users_own_box(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(signups_open=True)

    await _register(client, accept_terms=True, marketing_email=True)

    ledger = await _ledger(db_session, await _user_id(db_session))
    assert ("marketing_email", "consent", True, None, "register") in ledger


async def test_a_closed_registration_keeps_the_answers_for_create_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _register(client, accept_terms=True, marketing_email=True)

    request = await db_session.scalar(select(SignupRequest))
    assert request is not None
    assert (request.terms_version, request.privacy_version, request.marketing_email) == (
        privacy.TERMS_VERSION,
        privacy.PRIVACY_VERSION,
        True,
    )


async def test_an_account_made_from_a_signup_request_forgets_the_request(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    await _register(client, accept_terms=True)
    app.state.settings = make_settings(signups_open=True)

    await _register(client, accept_terms=True)

    assert await db_session.scalar(select(func.count()).select_from(SignupRequest)) == 0


# --- /v1/me/privacy ---------------------------------------------------------------


async def _privacy(client: AsyncClient, **body: Any) -> Any:
    if body:
        response = await client.patch("/v1/me/privacy", json=body, headers=FRONTEND)
    else:
        response = await client.get("/v1/me/privacy")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _granted(body: Any) -> dict[str, bool]:
    return {purpose["purpose"]: purpose["granted"] for purpose in body["purposes"]}


async def test_privacy_lists_every_purpose_with_its_basis(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI
) -> None:
    app.state.settings = make_settings(signups_open=True)
    await _register(client, accept_terms=True)
    await sign_in(client)

    body = await _privacy(client)

    assert [
        (p["purpose"], p["basis"], p["granted"], p["changeable"]) for p in body["purposes"]
    ] == [
        ("account", "contract", True, False),
        ("product_email", "legitimate_interest", True, True),
        ("marketing_email", "consent", False, True),
    ]
    assert body["terms"]["accepted_version"] == body["terms"]["current_version"]
    assert body["terms"]["accepted_at"] is not None


async def test_consent_is_given_and_withdrawn_and_each_change_is_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)

    assert _granted(await _privacy(client, marketing_email=True))["marketing_email"] is True
    # The same answer again is the same choice: no second row.
    await _privacy(client, marketing_email=True)
    withdrawn = await _privacy(client, marketing_email=False, product_email=False)

    assert _granted(withdrawn) == {
        "account": True,
        "product_email": False,
        "marketing_email": False,
    }
    assert [row[:3] for row in await _ledger(db_session, user_id)] == [
        ("marketing_email", "consent", True),
        ("product_email", "legitimate_interest", False),
        ("marketing_email", "consent", False),
    ]


async def test_an_account_with_no_record_is_asked_nothing_and_accepted_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    body = await _privacy(client)

    assert body["terms"]["accepted_version"] is None
    assert _granted(body)["marketing_email"] is False


async def test_the_account_purpose_is_not_a_choice(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.patch("/v1/me/privacy", json={"account": False}, headers=FRONTEND)

    assert _error(response) == (422, "validation_error")


async def test_an_unverified_account_may_still_withdraw(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, verified=False)
    await sign_in(client)

    assert _granted(await _privacy(client, product_email=False))["product_email"] is False


# --- the export -------------------------------------------------------------------


async def test_the_export_is_everything_held_and_no_secret(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI, outbox: Outbox
) -> None:
    app.state.settings = make_settings(signups_open=True)
    await _register(client, accept_terms=True, time_zone="Europe/London")
    await sign_in(client)
    user_id = await _user_id(db_session)
    filer_id = await db_session.scalar(
        insert(Filer)
        .values(name="BERKSHIRE HATHAWAY INC", slug="berkshire-hathaway")
        .returning(Filer.id)
    )
    db_session.add(WatchlistItem(user_id=user_id, filer_id=filer_id))
    await subscribe(db_session, user_id, "free", status="canceled")
    await subscribe(
        db_session, user_id, "pro", grandfathered="api_hobbyist", overrides={"mcp_enabled": False}
    )

    response = await client.get("/v1/me/export")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="whalewatch-export-'
    )
    body = response.json()
    assert body["format"] == "whalewatch-export/1"
    assert body["account"]["email"] == "ada@example.com"
    assert body["account"]["time_zone"] == "Europe/London"
    assert body["watchlist"]["investors"][0]["slug"] == "berkshire-hathaway"
    assert [c["purpose"] for c in body["consents"]] == [
        "account",
        "product_email",
        "marketing_email",
    ]
    # The verification link registering sent, and the session it started.
    assert [link["purpose"] for link in body["links"]] == ["email_verify"]
    assert [session["remember"] for session in body["sessions"]] == [True]
    assert [
        {k: s[k] for k in ("plan", "status", "grandfathered_plan", "overrides")}
        for s in body["subscriptions"]
    ] == [
        {
            "plan": "pro",
            "status": "active",
            "grandfathered_plan": "api_hobbyist",
            "overrides": {"mcp_enabled": False},
        },
        {"plan": "free", "status": "canceled", "grandfathered_plan": None, "overrides": {}},
    ]
    assert body["not_included"]
    password_hash = await db_session.scalar(select(AppUser.password_hash))
    token_hash = await db_session.scalar(select(AccountToken.token_hash))
    assert password_hash is not None and password_hash not in response.text
    assert token_hash is not None and token_hash not in response.text
    assert outbox.token_for("ada@example.com") not in response.text


async def test_the_export_needs_a_session(client: AsyncClient) -> None:
    assert _error(await client.get("/v1/me/export")) == (401, "unauthenticated")


# --- deletion ---------------------------------------------------------------------


async def _delete(client: AsyncClient, password: str = PASSWORD) -> Response:
    return await client.request(
        "DELETE", "/v1/account", json={"password": password}, headers=FRONTEND
    )


async def test_deleting_needs_the_password(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _delete(client, "not the password")

    assert _error(response) == (403, "invalid_credentials")
    assert (await client.get("/v1/me")).status_code == 200


async def test_deleting_signs_out_frees_the_address_and_records_the_request(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI, outbox: Outbox
) -> None:
    user_id = await make_user(db_session)
    await sign_in(client)
    await client.patch("/v1/account", json={"email": "ada@new.example"}, headers=FRONTEND)

    response = await _delete(client)

    assert response.status_code == 204, response.text
    assert "ww_session" not in client.cookies
    assert (await client.get("/v1/me")).json()["user"] is None
    login = await client.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers=FRONTEND
    )
    assert _error(login) == (403, "invalid_credentials")
    # The pending email change's link is dead.
    assert await db_session.scalar(select(func.count()).select_from(AccountToken)) == 0
    [deletion] = (await db_session.scalars(select(AccountDeletion))).all()
    assert (deletion.user_id, deletion.requested_by) == (user_id, "user")
    assert deletion.purge_after - deletion.requested_at == timedelta(days=30)
    assert outbox.sent[-1].to == "ada@example.com"
    assert outbox.sent[-1].subject == "Your WhaleWatch account is deleted"

    app.state.settings = make_settings(signups_open=True)
    again = await _register(client, accept_terms=True)
    assert again.status_code == 202, again.text


async def test_every_other_session_ends_too(
    client: AsyncClient, db_session: AsyncSession, app: FastAPI, session_redis: FakeRedis
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await sign_in(client)  # a second session: another device

    await _delete(client)

    assert not [key for key in session_redis.values if key.startswith("ww:session:")]


# --- purge and restore ------------------------------------------------------------


async def _deleted_user(session: AsyncSession, email: str = "ada@example.com") -> AppUser:
    user_id = await make_user(session, email)
    user = await session.get(AppUser, user_id)
    assert user is not None
    privacy.record_signup(session, user_id, marketing_email=True, source=ConsentSource.REGISTER)
    filer_id = await session.scalar(
        insert(Filer).values(name=f"FILER {user_id}", slug=f"filer-{user_id}").returning(Filer.id)
    )
    session.add(WatchlistItem(user_id=user_id, filer_id=filer_id))
    await subscribe(session, user_id, "pro", overrides={"watchlist_items": 50})
    await privacy.delete_account(
        session,
        SessionStore(FakeRedis()),  # type: ignore[arg-type]
        user,
        requested_by=DeletionRequester.USER,
    )
    return user


async def test_the_purge_erases_the_account_and_the_audit_row_survives(
    db_session: AsyncSession,
) -> None:
    user = await _deleted_user(db_session)

    purged = await privacy.purge_due(db_session, at=now() + timedelta(days=30, seconds=1))

    assert [p.user_id for p in purged] == [user.id]
    for model in (AppUser, WatchlistItem, ConsentRecord, Subscription, SubscriptionEntitlement):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0, model
    [deletion] = (
        await db_session.execute(select(AccountDeletion.purged_at, AccountDeletion.erased))
    ).all()
    assert deletion.purged_at is not None
    assert deletion.erased == {
        "app_user": 1,
        "watchlist_item": 1,
        "saved_filter": 0,
        "consent_record": 3,
        "account_token": 0,
        "subscription": 1,
        "api_key": 0,
        "usage_counter": 0,
        "plan_override": 0,
        "user_activity_day": 0,
    }


async def test_nothing_is_purged_before_its_thirty_days(db_session: AsyncSession) -> None:
    await _deleted_user(db_session)

    assert await privacy.purge_due(db_session, at=now() + timedelta(days=29)) == []
    assert await db_session.scalar(select(func.count()).select_from(AppUser)) == 1


async def test_a_restored_account_is_as_it_was_and_never_purged(
    db_session: AsyncSession,
) -> None:
    user = await _deleted_user(db_session)

    restored = await privacy.restore_account(db_session, "ADA@example.com")

    assert restored.id == user.id and restored.deleted_at is None
    assert await privacy.purge_due(db_session, at=now() + timedelta(days=31)) == []
    assert await db_session.scalar(select(func.count()).select_from(WatchlistItem)) == 1
    restored_at = await db_session.scalar(select(AccountDeletion.restored_at))
    assert restored_at is not None


async def test_an_address_taken_since_cannot_be_restored(db_session: AsyncSession) -> None:
    await _deleted_user(db_session)
    await make_user(db_session, "ada@example.com", name="Another Ada")

    with pytest.raises(privacy.RestoreError, match="another account"):
        await privacy.restore_account(db_session, "ada@example.com")


async def test_the_database_holds_one_waiting_deletion_per_account(
    db_session: AsyncSession,
) -> None:
    user = await _deleted_user(db_session)
    await db_session.execute(update(AppUser).values(deleted_at=None))

    with pytest.raises(Exception, match="uq_account_deletion_user_id"):
        await privacy.delete_account(
            db_session,
            SessionStore(FakeRedis()),  # type: ignore[arg-type]
            user,
            requested_by=DeletionRequester.OPERATOR,
        )
