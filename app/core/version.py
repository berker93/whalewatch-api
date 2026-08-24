"""The running version, resolved once.

Read from packaging metadata rather than a hand-maintained ``__version__``, so
it cannot drift from ``pyproject.toml``. Paired with ``settings.git_sha`` in the
/health response: the version says which release, the SHA says which commit.
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

DISTRIBUTION = "whalewatch-api"

# app/core/version.py -> app/core -> app -> repo root.
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _resolve_version() -> str:
    """Installed metadata first, ``pyproject.toml`` second, ``"unknown"`` last.

    Both paths are needed because this project has no ``[build-system]``: uv
    treats it as a virtual project and never installs a distribution, so
    ``importlib.metadata`` finds nothing here or in the Docker image. Reading
    pyproject.toml — which the image does contain — is what keeps /health from
    reporting "unknown" forever. The metadata lookup stays first so that adding
    a build backend later silently starts working, and so a wheel that ships
    without its sources still answers.
    """
    try:
        return package_version(DISTRIBUTION)
    except PackageNotFoundError:
        pass

    try:
        with PYPROJECT.open("rb") as handle:
            project: dict[str, object] = tomllib.load(handle).get("project", {})
    except OSError:  # pragma: no cover - only if pyproject is absent
        return "unknown"

    version = project.get("version")
    return version if isinstance(version, str) else "unknown"


VERSION = _resolve_version()
