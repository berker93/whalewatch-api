"""``/v1/account``, against a real Postgres: the profile, its edits, and an email change.

The rules are BE-1 and BE-2 in docs/be-requirements-timeline-account.md, as the
account page relies on them: one refused field refuses the whole PATCH, each
refusal names its field by the wire name, a new email waits for its link, and
the link changes the login itself.
"""

from datetime import timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.sessions import now
from app.db.models import AccountToken, AppUser
from tests.integration.accounts import FRONTEND, PASSWORD, Outbox, make_user, sign_in


async def _patch(client: AsyncClient, **body: object) -> Response:
    return await client.patch("/v1/account", json=body, headers=FRONTEND)


async def test_the_profile_is_the_signed_in_users(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, time_zone="Asia/Tokyo")
    await make_user(db_session, "grace@example.com", name="Grace Hopper")
    await sign_in(client)

    response = await client.get("/v1/account")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert {k: body[k] for k in ("name", "email", "time_zone", "pending_email")} == {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "time_zone": "Asia/Tokyo",
        "pending_email": None,
    }
    assert body["password_changed_at"] == body["member_since"]


async def test_name_and_time_zone_apply_at_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _patch(client, name="  Ada King ", time_zone="Europe/London")

    assert response.status_code == 200
    update_ = response.json()
    assert update_["profile"]["name"] == "Ada King"
    assert update_["profile"]["time_zone"] == "Europe/London"
    assert update_["pending_email"] is None
    assert (await client.get("/v1/account")).json()["name"] == "Ada King"


async def test_a_field_left_out_or_null_stays(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _patch(client, name=None)

    assert response.json()["profile"]["name"] == "Ada Lovelace"


async def test_one_refused_field_refuses_them_all_and_each_names_its_field(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await make_user(db_session, "grace@example.com", name="Grace Hopper")
    await sign_in(client)
    before = (await client.get("/v1/account")).json()

    response = await _patch(
        client, name="A", email="grace@example.com", time_zone="Mars/Olympus_Mons"
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert [e["loc"] for e in error["detail"]["errors"]] == [
        ["body", "name"],
        ["body", "email"],
        ["body", "time_zone"],
    ]
    assert (await client.get("/v1/account")).json() == before


async def test_a_valid_field_beside_a_refused_one_is_not_applied(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _patch(client, name="Ada King", time_zone="Nowhere/Special")

    assert response.status_code == 422
    assert (await client.get("/v1/account")).json()["name"] == "Ada Lovelace"


async def test_an_unknown_field_is_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _patch(client, timeZone="Europe/London")

    assert response.status_code == 422


@pytest.mark.parametrize("zone", ["UTC", "America/Argentina/Buenos_Aires", "Asia/Kolkata"])
async def test_any_iana_zone_is_accepted(
    client: AsyncClient, db_session: AsyncSession, zone: str
) -> None:
    await make_user(db_session)
    await sign_in(client)

    assert (await _patch(client, time_zone=zone)).status_code == 200


# --- the email --------------------------------------------------------------------


async def test_a_new_email_waits_for_its_link_and_tells_the_old_address(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await _patch(client, email="  Ada@Analytical.Engine ")

    update_ = response.json()
    assert update_["profile"]["email"] == "ada@example.com"
    assert (
        update_["pending_email"] == update_["profile"]["pending_email"] == "ada@analytical.engine"
    )
    assert [m.to for m in outbox.sent] == ["ada@analytical.engine", "ada@example.com"]
    assert "http://app.localhost:5173/account/confirm-email?token=" in outbox.sent[0].body
    # A reload still shows it.
    assert (await client.get("/v1/account")).json()["pending_email"] == "ada@analytical.engine"


async def test_confirming_swaps_the_login_everywhere(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="ada@analytical.engine")
    token = outbox.token_for("ada@analytical.engine")

    confirmed = await client.post(
        "/v1/account/email/confirm", json={"token": token}, headers=FRONTEND
    )

    assert confirmed.status_code == 204
    account = (await client.get("/v1/account")).json()
    assert (account["email"], account["pending_email"]) == ("ada@analytical.engine", None)
    assert (await client.get("/v1/auth/session")).json()["session"]["user"] == {
        "email": "ada@analytical.engine"
    }
    old = await client.post(
        "/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers=FRONTEND
    )
    assert old.status_code == 403
    await sign_in(client, "ada@analytical.engine")


async def test_confirming_needs_no_session(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    """The link may be opened on another device."""
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="ada@analytical.engine")
    await client.post("/v1/auth/logout", headers=FRONTEND)

    response = await client.post(
        "/v1/account/email/confirm",
        json={"token": outbox.token_for("ada@analytical.engine")},
        headers=FRONTEND,
    )

    assert response.status_code == 204


async def test_a_second_change_replaces_the_first(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="first@example.com")
    await _patch(client, email="second@example.com")

    stale = await client.post(
        "/v1/account/email/confirm",
        json={"token": outbox.token_for("first@example.com")},
        headers=FRONTEND,
    )

    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "invalid_token"
    assert (await client.get("/v1/account")).json()["pending_email"] == "second@example.com"


async def test_the_current_email_is_no_change_and_cancels_one_waiting(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="new@example.com")
    sent = len(outbox.sent)

    response = await _patch(client, email="ADA@example.com")

    assert response.json()["pending_email"] is None
    assert len(outbox.sent) == sent
    assert await db_session.scalar(select(func.count()).select_from(AccountToken)) == 0


@pytest.mark.parametrize("problem", ["reused", "expired", "unknown"])
async def test_a_dead_link_is_invalid_token(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox, problem: str
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="new@example.com")
    token = outbox.token_for("new@example.com")
    if problem == "reused":
        await client.post("/v1/account/email/confirm", json={"token": token}, headers=FRONTEND)
    elif problem == "expired":
        await db_session.execute(update(AccountToken).values(expires_at=now() - timedelta(1)))
    else:
        token = "not-a-token"

    response = await client.post(
        "/v1/account/email/confirm", json={"token": token}, headers=FRONTEND
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_token"


async def test_an_address_taken_before_confirming_is_refused(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await make_user(db_session)
    await sign_in(client)
    await _patch(client, email="new@example.com")
    await make_user(db_session, "new@example.com", name="Newcomer")

    response = await client.post(
        "/v1/account/email/confirm",
        json={"token": outbox.token_for("new@example.com")},
        headers=FRONTEND,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_email"
    emails = (await db_session.scalars(select(AppUser.email).order_by(AppUser.id))).all()
    assert emails == ["ada@example.com", "new@example.com"]
