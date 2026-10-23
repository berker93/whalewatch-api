"""Password hashing: argon2id, from argon2-cffi.

argon2id because it is memory-hard: guessing one password costs 64 MiB of
memory as well as time, which takes away most of what a GPU farm gains over the
server that checks it. The parameters are argon2-cffi's defaults, RFC 9106's
"low memory" profile (t=3, m=64 MiB, p=4), which is OWASP's recommended
setting or stronger.

A hash is a PHC string, ``$argon2id$v=19$m=65536,t=3,p=4$salt$hash``. The
parameters travel with it, so raising them later re-hashes each password on its
owner's next login (:func:`needs_rehash`) rather than locking anyone out.

Hashing takes tens of milliseconds of CPU by design, so it runs in a thread:
an event loop that spent that long on every login would stall every other
request in the process for as long.
"""

import asyncio
import contextlib
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_HASHER: Final = PasswordHasher()


def hash_password_sync(password: str) -> str:
    return _HASHER.hash(password)


def verify_password_sync(password: str, stored: str) -> bool:
    """Whether ``password`` is the one ``stored`` was made from. False for a
    hash this cannot read, rather than an exception: it is a wrong password as
    far as the caller is concerned. The comparison is constant time inside
    argon2, so how long it takes says nothing about how much matched."""
    try:
        return _HASHER.verify(stored, password)
    except VerificationError:
        return False
    except InvalidHashError:
        # Refused without hashing, a hash argon2 cannot read would answer
        # faster than a wrong password, and say which accounts have one.
        with contextlib.suppress(VerificationError):
            _HASHER.verify(DUMMY_HASH, password)
        return False


def needs_rehash(stored: str) -> bool:
    """Whether ``stored`` was made with other parameters than today's."""
    try:
        return _HASHER.check_needs_rehash(stored)
    except InvalidHashError:
        return True


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password: str, stored: str) -> bool:
    return await asyncio.to_thread(verify_password_sync, password, stored)


#: Verified against when the email names no account, so that a login for an
#: unknown address does the same argon2 work as one with a wrong password, and
#: the time an answer takes does not say which addresses have accounts. Made
#: with today's parameters, as every real hash is after its owner's next login.
DUMMY_HASH: Final = hash_password_sync(secrets.token_urlsafe(16))
