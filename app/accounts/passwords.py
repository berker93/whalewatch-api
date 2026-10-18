"""Password hashing: scrypt, from the standard library.

scrypt rather than bcrypt or argon2 because it is in ``hashlib`` and is a
memory-hard KDF of the same class as argon2: nothing to install, nothing to pin,
and nothing for a C extension to fail to build on the next base image. The
parameters (n=2^14, r=8, p=1, 16 MiB of memory) are the ones Python's own docs
and OWASP's cheat sheet give as the interactive-login minimum.

A hash is stored as ``scrypt$n$r$p$salt$hash``, the salt and hash base64. The
parameters travel with it, so raising them later re-hashes each password on its
owner's next login (:func:`needs_rehash`) rather than locking anyone out.

Hashing takes tens of milliseconds of CPU by design, so it runs in a thread:
an event loop that spent that long on every login would stall every other
request in the process for as long.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import Final

N: Final = 2**14
R: Final = 8
P: Final = 1
_SALT_BYTES: Final = 16
_KEY_BYTES: Final = 32
# scrypt needs 128 * n * r bytes; OpenSSL refuses anything over maxmem.
_MAXMEM: Final = 64 * 1024 * 1024
_SCHEME: Final = "scrypt"


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_KEY_BYTES
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password_sync(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _derive(password, salt, N, R, P)
    return f"{_SCHEME}${N}${R}${P}${_b64(salt)}${_b64(key)}"


def verify_password_sync(password: str, stored: str) -> bool:
    """Whether ``password`` is the one ``stored`` was made from. False for a
    hash this cannot read, rather than an exception: it is a wrong password as
    far as the caller is concerned."""
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != _SCHEME:
            return False
        derived = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    # Constant time, so how long a comparison takes says nothing about how
    # much of the hash matched.
    return hmac.compare_digest(derived, base64.b64decode(key))


def needs_rehash(stored: str) -> bool:
    """Whether ``stored`` was made with other parameters than today's."""
    return not stored.startswith(f"{_SCHEME}${N}${R}${P}$")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password: str, stored: str) -> bool:
    return await asyncio.to_thread(verify_password_sync, password, stored)


#: Verified against when the email names no account, so that a login for an
#: unknown address takes as long as one with a wrong password, and the time
#: an answer takes does not say which addresses have accounts.
DUMMY_HASH: Final = hash_password_sync(secrets.token_urlsafe(16))
