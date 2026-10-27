"""Helpers for the account, auth, watchlist and timeline tests: users, sign-ins, and mail."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.mail import Message
from app.accounts.passwords import hash_password_sync
from app.db.models import AppUser, Plan, Subscription, SubscriptionEntitlement

PASSWORD = "correct horse 42"

#: What a browser on the frontend's dev server sends with every write.
FRONTEND = {"origin": "http://localhost:5173"}


async def make_user(
    session: AsyncSession,
    email: str = "ada@example.com",
    *,
    name: str = "Ada Lovelace",
    password: str = PASSWORD,
    time_zone: str = "America/New_York",
    verified: bool = True,
) -> int:
    """An account, verified unless ``verified=False``: most tests are about
    what a verified account does."""
    user_id = await session.scalar(
        insert(AppUser)
        .values(
            email=email,
            name=name,
            password_hash=hash_password_sync(password),
            time_zone=time_zone,
            email_verified_at=func.now() if verified else None,
        )
        .returning(AppUser.id)
    )
    assert user_id is not None
    return user_id


async def subscribe(
    session: AsyncSession,
    user_id: int,
    plan: str,
    *,
    status: str = "active",
    grandfathered: str | None = None,
    cancel_at: datetime | None = None,
    overrides: dict[str, Any] | None = None,
) -> int:
    """Put the user on ``plan``, by slug, as a payment would; with its own
    ``overrides`` when given."""

    def plan_id(slug: str) -> Any:
        return select(Plan.id).where(Plan.slug == slug).scalar_subquery()

    subscription_id = await session.scalar(
        insert(Subscription)
        .values(
            user_id=user_id,
            plan_id=plan_id(plan),
            status=status,
            grandfathered_plan_id=plan_id(grandfathered) if grandfathered else None,
            cancel_at=cancel_at,
        )
        .returning(Subscription.id)
    )
    assert subscription_id is not None
    for key, value in (overrides or {}).items():
        session.add(SubscriptionEntitlement(subscription_id=subscription_id, key=key, value=value))
    await session.flush()
    return subscription_id


async def sign_in(
    client: AsyncClient, email: str = "ada@example.com", password: str = PASSWORD, **body: Any
) -> Any:
    response = await client.post(
        "/v1/auth/login", json={"email": email, "password": password, **body}, headers=FRONTEND
    )
    assert response.status_code == 200, response.text
    return response.json()


@dataclass
class Outbox:
    """Every message the routes asked to send, instead of sending it."""

    sent: list[Message] = field(default_factory=list)

    async def send(self, message: Message, *, reference: str) -> str | None:
        self.sent.append(message)
        return f"outbox-{reference}"

    def token_for(self, to: str) -> str:
        [body] = [m.body for m in self.sent if m.to == to and "token=" in m.body]
        return body.split("token=", 1)[1].split()[0]
