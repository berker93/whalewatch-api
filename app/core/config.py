"""Application settings via pydantic-settings.

One typed object, validated once, is the only place this codebase reads the
environment. Nothing else should touch ``os.environ``: a scattered ``getenv``
has no type, no default anyone can find, and no failure until the line that
needs it runs — which for a Celery task is 2am.

Precedence is pydantic-settings' default: real environment variables win over
``.env``. That is what lets compose inject config in dev (``env_file: .env``)
and a secret manager inject it in production, with the same code.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import EmailStr, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration.

    Constructing this reads the environment, so it raises ``ValidationError`` on
    a missing or malformed variable. Get it through :func:`get_settings`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env is also read by compose for ${...} substitution, so it carries
        # host-side port mappings (DB_PORT, API_PORT) that are none of the
        # app's business. Ignore them rather than fail on them.
        extra="ignore",
    )

    app_name: str = "WhaleWatch"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Stamped into the image at build time (``--build-arg GIT_SHA=$(git rev-parse
    # HEAD)``) and echoed by /health, so "which commit is actually serving?" is
    # answerable from outside the container. Defaults to "unknown" rather than
    # failing: a developer running uvicorn by hand has no build to stamp, and a
    # liveness endpoint that refuses to boot over a cosmetic field is a bad
    # trade.
    git_sha: str = "unknown"

    # --- Postgres ------------------------------------------------------------
    # Stored as parts, never as a pasted URL. A whole URL in the environment is
    # one string that four different consumers (app, alembic, psql, tests) each
    # have to re-parse to answer "which host?", and rotating the password means
    # rewriting a credential embedded in the middle of it.
    postgres_host: str = "db"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "whalewatch"
    postgres_password: SecretStr
    postgres_db: str = "whalewatch"
    postgres_test_db: str = "whalewatch_test"

    redis_url: str = "redis://redis:6379/0"

    # --- SEC / EDGAR ---------------------------------------------------------
    # No default, on purpose. SEC's fair-access policy requires a real contact
    # address in the User-Agent of every EDGAR request and blocks traffic that
    # omits or fakes one. A default here would be a plausible-looking value that
    # gets us rate-limited in production; refusing to boot is the cheaper
    # failure. EmailStr rather than str, so a typo fails at startup instead of
    # arriving as a 403 mid-backfill.
    sec_contact_email: EmailStr

    # SEC publishes a 10 req/s ceiling. Bounded so a fat-fingered `80.0` cannot
    # quietly turn the ingester into something that gets our IP banned.
    sec_rate_limit_per_second: float = Field(default=8.0, gt=0, le=10)

    def _postgres_dsn(self, database: str) -> str:
        """Assemble an asyncpg DSN for ``database`` from the parts above.

        ``quote`` is not decoration: ``PostgresDsn.build`` does not escape
        credentials, so a password containing ``@``, ``/`` or ``:`` produces a
        URL that parses into the wrong host. Encoding the two user-supplied
        fields is what makes an arbitrary rotated password safe to drop in.
        """
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{database}"
        )

    # A plain property, deliberately not a `computed_field`: a computed field is
    # included in `model_dump()`, which would put the plaintext password back
    # into every log line or error report that dumps settings — undoing the
    # point of SecretStr. Nothing needs to serialize the DSN.
    @property
    def database_url(self) -> str:
        """DSN for the application database."""
        return self._postgres_dsn(self.postgres_db)

    @property
    def test_database_url(self) -> str:
        """DSN for the pytest database created by ``scripts/init-db.sql``."""
        return self._postgres_dsn(self.postgres_test_db)

    @property
    def sec_user_agent(self) -> str:
        """User-Agent sent to EDGAR, in the ``Name contact@example.com`` shape
        SEC asks for. Derived here so the contact address has exactly one home."""
        return f"{self.app_name} {self.sec_contact_email}"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first call.

    Cached because validation reads files and should happen once, and because
    every caller sharing one instance means a test can override it in one place.
    Tests that manipulate the environment must call ``get_settings.cache_clear()``.
    """
    # No arguments: every field comes from the environment. The pydantic mypy
    # plugin (configured in pyproject.toml) knows this, so no type: ignore is
    # needed here — without the plugin it would flag the required fields.
    return Settings()
