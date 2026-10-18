"""Request and response models for signing in and out, registering, and passwords.

The session itself never appears in a body: it is the httpOnly cookie these
routes set and clear. What a body says is who the session is for.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel


class _Request(BaseModel):
    # A misspelt field is a 422, not a field quietly ignored.
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Request):
    """An email and a password, and whether to stay signed in."""

    email: str = Field(
        description="The login. Trimmed and compared without case.",
        examples=["ada@example.com"],
    )
    password: str = Field(description="As typed, never trimmed.", examples=["correct horse 42"])
    remember: bool = Field(
        default=True,
        description=(
            "Keep the session for 30 days, past the browser's own session. False: the "
            "cookie ends with the browser, and the session after 12 hours regardless."
        ),
        examples=[True],
    )


class RegisterRequest(_Request):
    """A new account: the name on it, its login, and its password."""

    name: str = Field(description="At least 2 characters once trimmed.", examples=["Ada Lovelace"])
    email: str = Field(
        description="The login. Stored trimmed and lowercased.", examples=["ada@example.com"]
    )
    password: str = Field(description="At least 10 characters.", examples=["correct horse 42"])
    time_zone: str | None = Field(
        default=None,
        description=(
            "An IANA zone, such as the browser's. The account's zone when given, "
            "`America/New_York` when not."
        ),
        examples=["Europe/London"],
    )


class SessionUser(BaseModel):
    """Who a session is for."""

    email: str = Field(description="The account's login.", examples=["ada@example.com"])


class Session(BaseModel):
    """A signed-in session, as the cookie names it."""

    user: SessionUser = Field(description="Who is signed in.")
    expires_at: datetime = Field(
        description="When the session ends, whatever the browser still holds.",
        examples=["2026-11-17T09:30:00Z"],
    )


class SessionState(BaseModel):
    """The session the request's cookie names, if any."""

    session: Session | None = Field(
        description="Null when the request carried no session cookie. A dead one is a 401."
    )


class Recorded(BaseModel):
    """Signups are closed: the request is recorded and no session starts."""

    status: Literal["recorded"] = Field(
        description="The request was recorded.", examples=["recorded"]
    )


class Authed(BaseModel):
    """Signups are open: the account exists and the response set its session cookie."""

    status: Literal["authed"] = Field(description="Signed in.", examples=["authed"])
    session: Session = Field(description="The new session.")


class RegisterResult(RootModel[Annotated[Recorded | Authed, Field(discriminator="status")]]):
    """What registering did, by `status`: `recorded` while signups are closed,
    `authed` once they are open."""


class PasswordChangeRequest(_Request):
    """The current password, to prove it is the account's owner, and the new one."""

    current_password: str = Field(
        description="The account's password now.", examples=["correct horse 42"]
    )
    new_password: str = Field(
        description="At least 10 characters, and not the current one.",
        examples=["battery staple 1999"],
    )


class PasswordResetRequest(_Request):
    """Where to send a reset link."""

    email: str = Field(description="The account's login.", examples=["ada@example.com"])


class PasswordResetConfirm(_Request):
    """The token from a reset link, and the new password."""

    token: str = Field(
        description="The `token` query parameter of the emailed link.",
        examples=["Zq3c9V0yJmL2x5r8u1N4a7D6b0E3f9H2k5M8p1S4t7W"],
    )
    new_password: str = Field(
        description="At least 10 characters.", examples=["battery staple 1999"]
    )
