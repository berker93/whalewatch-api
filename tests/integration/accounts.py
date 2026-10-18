"""Helpers for the account, auth, watchlist and timeline tests: users, sign-ins, and mail."""

from dataclasses import dataclass, field
from typing import Any

from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.mail import Message
from app.accounts.passwords import hash_password_sync
from app.db.models import AppUser

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
) -> int:
    user_id = await session.scalar(
        insert(AppUser)
        .values(
            email=email,
            name=name,
            password_hash=hash_password_sync(password),
            time_zone=time_zone,
        )
        .returning(AppUser.id)
    )
    assert user_id is not None
    return user_id


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

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    def token_for(self, to: str) -> str:
        [body] = [m.body for m in self.sent if m.to == to and "token=" in m.body]
        return body.split("token=", 1)[1].split()[0]
