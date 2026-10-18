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
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import quote

from pydantic import EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Where ``npm run dev`` and ``npm run preview`` serve whalewatch-web from,
#: both surfaces (the app is ``app.localhost`` in dev). The CORS allow-list when
#: none is configured, outside staging and production.
DEV_CORS_ORIGINS: Final = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://app.localhost:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://app.localhost:4173",
)

#: whalewatch-web's app surface under ``npm run dev`` (its VITE_APP_ORIGIN):
#: where account emails link when WEB_URL is unset, outside staging and production.
DEV_WEB_URL: Final = "http://app.localhost:5173"


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

    # --- the public edge -----------------------------------------------------
    # The origins a browser may call this API from, comma-separated:
    # CORS_ORIGINS=https://whalewatch.io,https://www.whalewatch.io. Never "*":
    # an allow-list is the point. Unset means the frontend's dev servers outside
    # staging and production, and refuses to boot inside them, where a missing
    # value would otherwise be a frontend that cannot reach its API.
    cors_origins: Annotated[list[str] | None, NoDecode] = None

    # Per client IP, over a sliding minute, kept in Redis so every API process
    # counts against one budget. /health and /ready are not counted.
    rate_limit_per_minute: int = Field(default=60, ge=1)

    # A request still running after this is answered 504 and its handler
    # cancelled. Long enough for the slowest cold query in
    # docs/query-performance.md many times over.
    request_timeout_seconds: float = Field(default=30.0, gt=0)

    # The account, auth and watchlist bodies are a few hundred bytes; this only
    # bounds what a client can make the server read before refusing it.
    max_request_body_bytes: int = Field(default=64 * 1024, ge=0)

    # --- accounts and sessions ---------------------------------------------------
    # The session is an opaque token in an httpOnly cookie (app/accounts). The
    # domain is what lets whalewatch.com and app.whalewatch.com share one
    # session: SESSION_COOKIE_DOMAIN=.whalewatch.com. Unset is the host that
    # set it, which is what localhost needs. Secure (HTTPS only) in staging and
    # production, where a cookie sent over plain HTTP is a session handed out.
    session_cookie_name: str = "ww_session"
    session_cookie_domain: str | None = None

    # Logins, registrations and password calls per client IP per minute, on top
    # of rate_limit_per_minute: a password is guessed one request at a time.
    auth_rate_limit_per_minute: int = Field(default=10, ge=1)

    # Whether POST /v1/auth/register makes an account. Closed, it records the
    # request (signup_request) and starts no session, as the frontend's
    # FEATURES.signupsOpen promises; `create-user` makes accounts by hand.
    signups_open: bool = False

    # Where whalewatch-web's app surface is served: the links in emails (confirm
    # an email change, reset a password) point here. Required in staging and
    # production, where a localhost link would be a dead one in someone's inbox.
    web_url: str | None = None

    # How account emails leave. "log" writes them to the log, link included, and
    # is for development: nothing is sent. "smtp" sends through smtp_host.
    email_backend: Literal["log", "smtp"] = "log"
    email_from: str = "WhaleWatch <no-reply@whalewatch.io>"
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True

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

    # Development only. When set, every EDGAR body is kept on disk under this
    # directory and served from there next time, so iterating on a parser does
    # not refetch a filer's twenty years of submissions. Never in a deployed
    # environment: a submissions index changes every time the filer files, and
    # a cached one is a quietly out-of-date answer.
    edgar_cache_dir: Path | None = None

    # --- Form 4 crawl ----------------------------------------------------------
    # Which issuers' Form 4s ``crawl-form4`` queues (app/ingestion/insider_discovery.py).
    # All of EDGAR is about 1,700 Form 4s a business day. "held" is the issuers
    # tracked investors hold, "sp500" the S&P 500 from form4_sp500_path.
    form4_universe: Literal["held", "sp500"] = "held"

    # For "held": an issuer counts when it was at least this percentage of one
    # tracked portfolio in the last four quarters. The default, 1.0, is ~740
    # issuers and ~420 Form 4s a business day on 2026's data; 0 is everything
    # held (~4,800 issuers and ~1,400 a day, most of EDGAR), because a stock a
    # trillion-dollar manager holds at 0.001% is not one it holds for a reason.
    form4_min_weight_pct: float = Field(default=1.0, ge=0, le=100)

    # For "sp500": a CSV with a CIK column. Unset is the snapshot shipped in
    # data/universe/sp500.csv.
    form4_sp500_path: Path | None = None

    # --- OpenFIGI -------------------------------------------------------------
    # Optional, and worth having: a free key (a form at openfigi.com) is ten
    # times the request rate and ten times the CUSIPs per request. Unset, the
    # client runs at the unkeyed quota. The quotas themselves live with the
    # client (app/ingestion/enrichment/figi.py), next to the date they were
    # last checked.
    openfigi_api_key: SecretStr | None = None

    # --- quotes -----------------------------------------------------------------
    # Where ``fetch-quotes`` gets latest closes (app/ingestion/enrichment/quotes.py).
    # "null" gets nothing and is the default, and production's: every price the
    # API publishes is derived from SEC filings, and a quote is a vendor's data
    # under the vendor's licence. "yfinance" scrapes Yahoo, which is fine for
    # development and licensed for no one to redistribute, so production refuses
    # it. It is also not installed in the image: it is in the "quotes"
    # dependency group (uv sync --group quotes).
    quote_provider: Literal["null", "yfinance"] = "null"

    # --- raw document archive -----------------------------------------------
    # Where every fetched EDGAR document is kept, byte for byte, before a parser
    # sees it (app/storage). "local" writes under raw_store_local_root and is for
    # development; "s3" is any S3-compatible bucket — AWS, or R2 by pointing the
    # endpoint URL at the account's r2.cloudflarestorage.com host.
    raw_store_backend: Literal["local", "s3"] = "local"

    # The root the object keys are laid out under, not the directory the files
    # land in. Every key starts ``raw/``, so the default puts documents in
    # ./data/raw/ — the same layout the bucket has, which is what makes an
    # ``aws s3 sync`` of one into the other a plain copy.
    raw_store_local_root: Path = Path("data")

    raw_store_s3_bucket: str | None = None
    # Unset for AWS itself. For R2: https://<account-id>.r2.cloudflarestorage.com
    raw_store_s3_endpoint_url: str | None = None
    # R2 wants "auto"; AWS wants the bucket's region.
    raw_store_s3_region: str | None = None
    # Both unset means botocore's own credential chain — an instance role, in a
    # deployment that has one. Declared here rather than left to botocore's
    # reading of AWS_* variables so that the store's whole configuration is on
    # this class, like everything else's.
    raw_store_s3_access_key_id: str | None = None
    raw_store_s3_secret_access_key: SecretStr | None = None

    @field_validator("openfigi_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: object) -> object:
        """``OPENFIGI_API_KEY=`` in a copied .env means "none", not a key of
        ``""`` — which would be sent, and refused with a 401."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _origins_are_origins(cls, value: list[str] | None) -> list[str] | None:
        """Refuse ``*`` and anything with a path: the browser compares the
        ``Origin`` header byte for byte, and ``https://whalewatch.io/`` (trailing
        slash) never matches it, which fails as CORS errors in the console and
        nothing at all here."""
        for origin in value or ():
            if origin == "*":
                raise ValueError("CORS_ORIGINS must list origins; '*' would allow any site")
            scheme, _, rest = origin.partition("://")
            if scheme not in ("http", "https") or not rest or "/" in rest:
                raise ValueError(
                    f"CORS_ORIGINS entry {origin!r} is not an origin: scheme://host[:port], "
                    "no path or trailing slash"
                )
        return value

    @model_validator(mode="after")
    def _edgar_cache_is_for_development(self) -> Self:
        if self.edgar_cache_dir is not None and self.environment in ("staging", "production"):
            raise ValueError(
                f"EDGAR_CACHE_DIR is for development and must be unset in {self.environment}"
            )
        return self

    @model_validator(mode="after")
    def _yfinance_is_not_for_production(self) -> Self:
        """Production only, not staging: staging is not public, and is where a
        provider is tried before anything is paid for. The provider refuses
        production too, when constructed; this is the same refusal at boot."""
        if self.quote_provider == "yfinance" and self.environment == "production":
            raise ValueError(
                "QUOTE_PROVIDER=yfinance is for development: Yahoo's data is not licensed "
                "for redistribution. Production runs QUOTE_PROVIDER=null"
            )
        return self

    @model_validator(mode="after")
    def _raw_store_is_complete(self) -> Self:
        """Refuse a raw store configuration that would lose documents quietly.

        The local backend in a deployed environment writes to a container's own
        filesystem, which is gone at the next deploy — an archive that silently
        is not one, discovered on the day it is needed. And an S3 backend with
        no bucket, or half a key pair, would otherwise fail on the first put of
        the first ingest rather than at boot.
        """
        if self.raw_store_backend == "local" and self.environment in ("staging", "production"):
            raise ValueError(
                f"RAW_STORE_BACKEND=local is for development; {self.environment} needs "
                "RAW_STORE_BACKEND=s3 so archived documents outlive the container"
            )
        if self.raw_store_backend == "s3" and not self.raw_store_s3_bucket:
            raise ValueError("RAW_STORE_BACKEND=s3 needs RAW_STORE_S3_BUCKET")
        if (self.raw_store_s3_access_key_id is None) != (
            self.raw_store_s3_secret_access_key is None
        ):
            raise ValueError(
                "RAW_STORE_S3_ACCESS_KEY_ID and RAW_STORE_S3_SECRET_ACCESS_KEY are set "
                "together or not at all"
            )
        return self

    @model_validator(mode="after")
    def _cors_is_configured_where_deployed(self) -> Self:
        if self.cors_origins is None:
            if self.environment in ("staging", "production"):
                raise ValueError(
                    f"CORS_ORIGINS must be set in {self.environment}: the origins the "
                    "frontend is served from"
                )
            self.cors_origins = list(DEV_CORS_ORIGINS)
        return self

    @model_validator(mode="after")
    def _accounts_are_configured_where_deployed(self) -> Self:
        """The same refusal as CORS_ORIGINS: a deployed API that mails links to
        localhost, or "sends" mail to its own log, fails quietly in someone
        else's inbox."""
        if self.environment in ("staging", "production"):
            if self.web_url is None:
                raise ValueError(
                    f"WEB_URL must be set in {self.environment}: where whalewatch-web is "
                    "served, for the links in account emails"
                )
            if self.email_backend == "log":
                raise ValueError(
                    f"EMAIL_BACKEND=log is for development; {self.environment} needs "
                    "EMAIL_BACKEND=smtp so account emails are sent"
                )
        if self.email_backend == "smtp" and not self.smtp_host:
            raise ValueError("EMAIL_BACKEND=smtp needs SMTP_HOST")
        if self.web_url is None:
            self.web_url = DEV_WEB_URL
        self.web_url = self.web_url.rstrip("/")
        return self

    @property
    def session_cookie_secure(self) -> bool:
        """Whether the session cookie is HTTPS-only: wherever the API is deployed."""
        return self.environment in ("staging", "production")

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
