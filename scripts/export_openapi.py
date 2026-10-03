"""Write the API's OpenAPI document to ``openapi.json``: ``make openapi``.

Commit what it writes. CI runs it again and fails if the file changes, which is
how the frontend's generated types are kept from describing an API that no
longer exists. Run it after changing a route or a response model, and read the
diff: it is the change to the contract.

On the host, not in the container: it builds the app but never starts it, so it
opens no socket to Postgres or Redis.
"""

import os

# Before the import below reaches app.main, which builds its own app at import
# and so validates the environment. These two are the only required fields, and
# the document never reads either; a fresh clone with no .env still exports.
os.environ.setdefault("POSTGRES_PASSWORD", "unused")
os.environ.setdefault("SEC_CONTACT_EMAIL", "openapi@whalewatch.io")

from app.api.openapi import OPENAPI_PATH, render


def main() -> None:
    OPENAPI_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {OPENAPI_PATH.name}")


if __name__ == "__main__":
    main()
