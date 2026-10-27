"""Application settings via pydantic-settings.

One typed object, validated once, is the only place this codebase reads the
environment. Nothing else should touch ``os.environ``: a scattered ``getenv``
has no type, no default anyone can find, and no failure until the line that
needs it runs — which for a Celery task is 2am.

Precedence is pydantic-settings' default: real environment variables win over
``.env``. That is what lets compose inject config in dev (``env_file: .env``)
and a secret manager inject it in production, with the same code.
"""

from email.utils import parseaddr
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

#: Where ``make up`` serves this API: the one-click unsubscribe URL when
#: API_URL is unset, outside staging and production.
DEV_API_URL: Final = "http://localhost:8000"

#: What signs unsubscribe links when EMAIL_UNSUBSCRIBE_SECRET is unset, which
#: only the log backend allows: those links are in a log, not an inbox.
DEV_UNSUBSCRIBE_SECRET: Final = "development-only-unsubscribe-secret"


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

    # Celery's broker and result backend (app/jobs/celery_app.py). The same
    # server as redis_url, but its own database: the cache and rate-limit keys
    # and the task queues never share a keyspace, so a FLUSHDB of the cache's,
    # or a `redis-cli KEYS` on it, cannot reach a queued ingestion.
    celery_redis_url: str = "redis://redis:6379/1"

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

    # The admin console's own session (app/admin/auth.py): another cookie,
    # never shared across subdomains (no domain), sent only to /admin, and
    # SameSite=Strict. It ends after this many hours however busy the admin
    # is, and with the browser before that.
    admin_session_cookie_name: str = "ww_admin"
    admin_session_hours: float = Field(default=8.0, gt=0, le=24)

    # Logins, registrations and password reset requests: this many attempts in
    # this many seconds, counted per client IP and per email address, each
    # action on its own. A password is guessed one request at a time, from one
    # address against many accounts or from many addresses against one.
    auth_attempt_limit: int = Field(default=5, ge=1)
    auth_attempt_window_seconds: int = Field(default=15 * 60, ge=1)

    # How often each API process reloads what every plan allows
    # (app/accounts/entitlements.py): how long an UPDATE to plan_entitlement
    # takes to reach every request.
    entitlements_refresh_seconds: float = Field(default=60.0, gt=0)

    # How often each API process writes the quota counters it finds changed
    # in Redis to Postgres (app/accounts/usage.py), and does once more as it
    # exits: how far behind Redis usage_counter may be.
    usage_flush_seconds: float = Field(default=180.0, gt=0)

    # The other credential calls (a password change, the emailed links' tokens)
    # per client IP per minute, on top of rate_limit_per_minute.
    auth_rate_limit_per_minute: int = Field(default=10, ge=1)

    # Whether POST /v1/auth/register makes an account. Closed, it records the
    # request (signup_request) and starts no session, as the frontend's
    # FEATURES.signupsOpen promises; `create-user` makes accounts by hand.
    signups_open: bool = False

    # Where whalewatch-web's app surface is served: the links in emails (confirm
    # an email change, reset a password) point here. Required in staging and
    # production, where a localhost link would be a dead one in someone's inbox.
    web_url: str | None = None

    # Where this API is served, as the world sees it: the one-click unsubscribe
    # header in an email points here, since a mail provider posts it with no
    # page in between. Required in staging and production, like WEB_URL.
    api_url: str | None = None

    # How email leaves (app/accounts/delivery.py). "log" writes each message to
    # the log, link included, and is for development: nothing is sent.
    # "postmark" sends through Postmark, on two message streams: the account's
    # own mail (verify, reset, billing) on one, everything a user can
    # unsubscribe from (alerts, digests) on the other, so that a complaint
    # about a digest cannot stop a password reset.
    email_backend: Literal["log", "postmark"] = "log"
    # On the sending subdomain, never the apex: a deliverability problem on
    # mail.whalewatch.com stays there (docs/email-delivery.md).
    email_from: str = "WhaleWatch <no-reply@mail.whalewatch.com>"
    # Signs the unsubscribe links. Rotating it ends every link already sent,
    # which a user then follows to a page saying the link is not valid.
    email_unsubscribe_secret: SecretStr | None = None
    # A Postmark server's API token, which sends.
    postmark_server_token: SecretStr | None = None
    # The password in the webhook URL Postmark is given:
    # https://postmark:<secret>@api.whalewatch.com/webhooks/postmark
    postmark_webhook_secret: SecretStr | None = None
    # The server's two message streams, by their ids in Postmark.
    postmark_transactional_stream: str = "outbound"
    postmark_broadcast_stream: str = "broadcast"

    # --- billing (Paddle) ------------------------------------------------------
    # Paddle is the merchant of record: it takes the payment, the tax and the
    # invoice, and tells this API what happened by webhook (app/accounts/billing.py).
    # Unset, billing is off: the checkout, portal and webhook answer 503.
    # "sandbox" is sandbox-api.paddle.com and its test cards; production
    # refuses it, as it refuses a key that would take no one's money.
    paddle_environment: Literal["sandbox", "production"] = "sandbox"
    paddle_api_key: SecretStr | None = None
    # The notification destination's secret key, which signs every webhook.
    paddle_webhook_secret: SecretStr | None = None
    # Which Paddle price each plan is bought at, by plan slug, comma-separated:
    # PADDLE_PRICES=pro=pri_01h...,api_hobbyist=pri_01h... In the environment
    # rather than on the plan row because a sandbox and a live account have
    # different ids, and travel with the account's key and secret.
    paddle_prices: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # How far a webhook's signed timestamp may be from now. Paddle's own SDKs
    # allow five seconds; a redelivery is signed afresh, so a refused one is
    # retried with a new timestamp.
    paddle_signature_tolerance_seconds: int = Field(default=5, ge=1)

    # A renewal whose payment failed keeps its plan this long, while Paddle
    # retries the card and the user is told to update it; then it is Free.
    billing_grace_days: int = Field(default=7, ge=0)
    # The reminder that the grace is ending goes this long before it ends.
    billing_reminder_days: int = Field(default=2, ge=0)

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

    @field_validator(
        "paddle_api_key",
        "paddle_webhook_secret",
        "postmark_server_token",
        "postmark_webhook_secret",
        "email_unsubscribe_secret",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("paddle_prices", mode="before")
    @classmethod
    def _split_prices(cls, value: object) -> object:
        """``pro=pri_1,api_pro=pri_2`` to ``{"pro": "pri_1", "api_pro": "pri_2"}``."""
        if not isinstance(value, str):
            return value
        prices: dict[str, str] = {}
        for pair in filter(None, (part.strip() for part in value.split(","))):
            slug, sep, price = (piece.strip() for piece in pair.partition("="))
            if not sep or not slug or not price:
                raise ValueError(f"PADDLE_PRICES entry {pair!r} is not plan=price_id")
            prices[slug] = price
        return prices

    @field_validator("paddle_prices")
    @classmethod
    def _one_plan_per_price(cls, value: dict[str, str]) -> dict[str, str]:
        """A webhook names a price, and finds its plan by it: two plans on one
        price would make that a guess."""
        if len(set(value.values())) != len(value):
            raise ValueError("PADDLE_PRICES gives two plans the same price")
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
    def _billing_is_whole(self) -> Self:
        """Half a Paddle configuration fails at the first checkout or the
        first webhook, in front of a customer; refuse it at boot."""
        if (self.paddle_api_key is None) != (self.paddle_webhook_secret is None):
            raise ValueError(
                "PADDLE_API_KEY and PADDLE_WEBHOOK_SECRET are set together or not at all"
            )
        if (
            self.paddle_api_key is not None
            and self.paddle_environment == "sandbox"
            and self.environment == "production"
        ):
            raise ValueError(
                "PADDLE_ENVIRONMENT=sandbox takes no one's money; production needs "
                "PADDLE_ENVIRONMENT=production"
            )
        if self.billing_reminder_days > self.billing_grace_days:
            raise ValueError("BILLING_REMINDER_DAYS cannot be longer than BILLING_GRACE_DAYS")
        return self

    @property
    def billing_enabled(self) -> bool:
        """Whether Paddle is configured: checkout, portal and webhooks work."""
        return self.paddle_api_key is not None and self.paddle_webhook_secret is not None

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
            if self.api_url is None:
                raise ValueError(
                    f"API_URL must be set in {self.environment}: where this API is "
                    "served, for the one-click unsubscribe header in emails"
                )
            if self.email_backend == "log":
                raise ValueError(
                    f"EMAIL_BACKEND=log is for development; {self.environment} needs "
                    "EMAIL_BACKEND=postmark so emails are sent"
                )
            domain = self.email_domain
            if domain.count(".") < 2:
                raise ValueError(
                    f"EMAIL_FROM is on {domain}: {self.environment} sends from a "
                    "subdomain of its own (mail.whalewatch.com), so a deliverability "
                    "problem cannot reach the apex"
                )
        if self.email_backend == "postmark":
            missing = [
                name
                for name, value in (
                    ("POSTMARK_SERVER_TOKEN", self.postmark_server_token),
                    ("POSTMARK_WEBHOOK_SECRET", self.postmark_webhook_secret),
                    ("EMAIL_UNSUBSCRIBE_SECRET", self.email_unsubscribe_secret),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"EMAIL_BACKEND=postmark needs {', '.join(missing)}")
        if self.email_unsubscribe_secret is None:
            self.email_unsubscribe_secret = SecretStr(DEV_UNSUBSCRIBE_SECRET)
        if self.api_url is None:
            self.api_url = DEV_API_URL
        self.api_url = self.api_url.rstrip("/")
        if self.web_url is None:
            self.web_url = DEV_WEB_URL
        self.web_url = self.web_url.rstrip("/")
        return self

    @property
    def email_domain(self) -> str:
        """The domain EMAIL_FROM sends from: ``mail.whalewatch.com``."""
        _, address = parseaddr(self.email_from)
        return address.rpartition("@")[2].lower()

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
