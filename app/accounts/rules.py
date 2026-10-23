"""What a name, an email, a password and a time zone must be.

Each rule is the frontend's (``src/modules/Auth/validation.ts`` and
``src/constants/auth.ts`` in whalewatch-web) or looser, never stricter: a value
the form accepts and the API refuses is a 422 the user cannot understand. The
limits the frontend has no counterpart for (the maximum lengths) are far past
anything a person types.

The password rule is the frontend's, at least 10 characters (Q1 in
docs/be-requirements-timeline-account.md, settled). Change
:data:`PASSWORD_MIN_LENGTH` here and ``PASSWORD_MIN_LENGTH`` there together.
"""

import re
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

NAME_MIN_LENGTH: Final = 2
NAME_MAX_LENGTH: Final = 200

#: The frontend's EMAIL_PATTERN: something, an @, something, a dot, something.
EMAIL_PATTERN: Final = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
#: RFC 5321's limit on a forward path.
EMAIL_MAX_LENGTH: Final = 254

PASSWORD_MIN_LENGTH: Final = 10
#: Bounds what one login makes the server read and hash, and nothing a person would hit.
PASSWORD_MAX_LENGTH: Final = 1000

#: Every new account's zone: the market's clock (Q2, settled), not the
#: browser's. The user changes it on the account page. Registration still
#: takes an optional ``time_zone``, which the frontend does not send.
DEFAULT_TIME_ZONE: Final = "America/New_York"


def normalise_name(name: str) -> str:
    return name.strip()


def normalise_email(email: str) -> str:
    """Trimmed, as login and registration read an address. Not lowercased:
    the column is a ``citext``, which compares without case, so the address is
    kept as its owner typed it."""
    return email.strip()


def name_problem(name: str) -> str | None:
    """Why ``name`` (already normalised) cannot be an account's, or None."""
    if len(name) < NAME_MIN_LENGTH:
        return f"must be at least {NAME_MIN_LENGTH} characters"
    if len(name) > NAME_MAX_LENGTH:
        return f"must be at most {NAME_MAX_LENGTH} characters"
    return None


def email_problem(email: str) -> str | None:
    """Why ``email`` (already normalised) cannot be a login, or None."""
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.match(email):
        return "is not a valid email address"
    return None


def password_problem(password: str) -> str | None:
    """Why ``password`` cannot be a new password, or None. Not trimmed: a
    space is a character like any other."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"must be at least {PASSWORD_MIN_LENGTH} characters"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"must be at most {PASSWORD_MAX_LENGTH} characters"
    return None


# --- time zones -----------------------------------------------------------------

#: Every zone Postgres knows, read once per process. Asked of Postgres, as
#: app.api.routers.meta asks it for EDGAR's clock, rather than of ``zoneinfo``,
#: which in the container has a partial database: the zone a timeline is
#: counted in is the zone Postgres converts with, so the two must agree.
_zones: frozenset[str] | None = None
_NOT_ZONES: Final = frozenset({"localtime", "posixrules", "Factory"})


async def known_time_zones(session: AsyncSession) -> frozenset[str]:
    global _zones
    if _zones is None:
        names = await session.scalars(select(text("name")).select_from(text("pg_timezone_names")))
        # The tz database's duplicate trees and files that are not zones.
        _zones = frozenset(
            name
            for name in names
            if not name.startswith(("posix/", "right/")) and name not in _NOT_ZONES
        )
    return _zones


async def time_zone_problem(session: AsyncSession, zone: str) -> str | None:
    """Why ``zone`` is not an IANA time zone this server can count days in, or None."""
    if zone not in await known_time_zones(session):
        return f"'{zone}' is not an IANA time zone, such as America/New_York"
    return None
