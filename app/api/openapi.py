"""The OpenAPI document, built the one way the committed ``openapi.json`` is.

``openapi.json`` at the repo root is the contract the frontend's types are
generated from (``npm run gen:api`` in whalewatch-web). It is committed, and
CI rebuilds it and fails on any difference, so a change to a response model is
a reviewed diff here before it is a type error there.

That only works if building it is deterministic. Settings are therefore made
here, from nothing: the title is ``settings.app_name``, and neither a
developer's ``.env`` nor their shell may rename the API in a file everybody
commits.
"""

import json
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from app.core.config import Settings

# app/api/openapi.py -> app/api -> app -> repo root.
OPENAPI_PATH: Final = Path(__file__).resolve().parents[2] / "openapi.json"


def build() -> dict[str, Any]:
    """The document, as ``GET /openapi.json`` would serve it outside production."""
    # Not at module level: app.main builds its own app at import, from the
    # environment, and a module that only renders a schema should not need one
    # to be importable.
    from app.main import create_app

    # model_construct, not Settings(_env_file=None): that skips the file but
    # still reads os.environ, where APP_NAME would retitle the document.
    # Construction reads nothing, so every field the call leaves out is the
    # default written in app.core.config.
    settings = Settings.model_construct(
        postgres_password=SecretStr("unused"),
        sec_contact_email="openapi@whalewatch.io",
        environment="test",
    )
    return create_app(settings).openapi()


def render() -> str:
    """``build()`` as the bytes committed: two-space indent, a trailing newline.

    Keys stay in FastAPI's order rather than sorted, so the file reads path by
    path in the order the routers declare them.
    """
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
