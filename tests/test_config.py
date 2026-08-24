"""Settings are the one thing every other module depends on, so they get tested
first. Every case here builds Settings with ``_env_file=None`` and an explicitly
populated environment: reading the developer's real .env would make these pass
or fail based on a gitignored file.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

# The minimum that satisfies the two fields with no default.
REQUIRED = {
    "POSTGRES_PASSWORD": "localdev",
    "SEC_CONTACT_EMAIL": "ops@whalewatch.io",
}


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Construct Settings from a known environment, ignoring the repo's .env."""
    for key, value in {**REQUIRED, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


class TestRequiredVariables:
    def test_missing_sec_contact_email_refuses_to_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AC in one test: no default, no fallback, no start."""
        monkeypatch.setenv("POSTGRES_PASSWORD", "localdev")
        monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)

        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None)

        assert "sec_contact_email" in str(exc.value)

    def test_malformed_sec_contact_email_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd address should fail here, not as a 403 from EDGAR later."""
        with pytest.raises(ValidationError):
            build(monkeypatch, SEC_CONTACT_EMAIL="not-an-address")

    def test_missing_postgres_password_refuses_to_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEC_CONTACT_EMAIL", "ops@whalewatch.io")
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None)

        assert "postgres_password" in str(exc.value)


class TestDatabaseUrl:
    def test_assembled_from_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(
            monkeypatch,
            POSTGRES_HOST="db",
            POSTGRES_PORT="5432",
            POSTGRES_USER="whalewatch",
            POSTGRES_PASSWORD="localdev",
            POSTGRES_DB="whalewatch",
        )

        assert settings.database_url == (
            "postgresql+asyncpg://whalewatch:localdev@db:5432/whalewatch"
        )

    def test_test_database_url_differs_only_in_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pytest DB must be a different database on the same server —
        if these two ever collide, a test run truncates the dev data."""
        settings = build(monkeypatch, POSTGRES_DB="whalewatch", POSTGRES_TEST_DB="whalewatch_test")

        assert settings.test_database_url.endswith("/whalewatch_test")
        assert settings.test_database_url != settings.database_url

    def test_special_characters_in_password_are_encoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rotated password containing '@' or '/' must not be able to rewrite
        the host portion of the DSN. This is why the URL is not an f-string of
        raw parts."""
        settings = build(monkeypatch, POSTGRES_PASSWORD="p@ss/w:rd")

        assert settings.database_url == (
            "postgresql+asyncpg://whalewatch:p%40ss%2Fw%3Ard@db:5432/whalewatch"
        )
        # The real check: the host survives intact.
        assert "@db:5432/" in settings.database_url


class TestSecretsDoNotLeak:
    def test_password_is_hidden_in_repr_and_dump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Covers the three ways a settings object reaches a log or a traceback:
        repr (exception context), str, and model_dump (structured logging)."""
        settings = build(monkeypatch, POSTGRES_PASSWORD="hunter2")

        assert "hunter2" not in repr(settings)
        assert "hunter2" not in str(settings)
        assert "hunter2" not in str(settings.model_dump())

    def test_database_url_is_not_serialized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """database_url is a plain property rather than a computed_field on
        purpose: a computed_field lands in model_dump() with the password in
        cleartext, which would undo SecretStr."""
        settings = build(monkeypatch, POSTGRES_PASSWORD="hunter2")

        assert "database_url" not in settings.model_dump()

    def test_secret_is_still_readable_deliberately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch, POSTGRES_PASSWORD="hunter2")

        assert settings.postgres_password.get_secret_value() == "hunter2"


class TestValidation:
    def test_rate_limit_above_sec_ceiling_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValidationError):
            build(monkeypatch, SEC_RATE_LIMIT_PER_SECOND="80")

    def test_unknown_environment_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'prod' is not 'production'. Catching that here beats discovering it
        via a branch that silently never ran."""
        with pytest.raises(ValidationError):
            build(monkeypatch, ENVIRONMENT="prod")

    def test_compose_only_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """.env carries DB_PORT/API_PORT for compose substitution. Settings must
        tolerate them rather than fail on extras."""
        settings = build(monkeypatch, DB_PORT="5433", API_PORT="8000")

        assert not hasattr(settings, "db_port")

    def test_user_agent_carries_the_contact_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = build(monkeypatch, APP_NAME="WhaleWatch", SEC_CONTACT_EMAIL="ops@whalewatch.io")

        assert settings.sec_user_agent == "WhaleWatch ops@whalewatch.io"


class TestEnvFile:
    def test_env_file_is_read_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Local dev gets its values from .env with no exported variables."""
        for key in (*REQUIRED, "ENVIRONMENT"):
            monkeypatch.delenv(key, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "POSTGRES_PASSWORD=from-file\nSEC_CONTACT_EMAIL=file@whalewatch.io\nENVIRONMENT=test\n"
        )

        settings = Settings(_env_file=env_file)

        assert settings.environment == "test"
        assert settings.postgres_password.get_secret_value() == "from-file"

    def test_real_environment_wins_over_env_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Production injects real variables over a baked-in .env; that only
        works if the environment has precedence."""
        env_file = tmp_path / ".env"
        env_file.write_text("POSTGRES_PASSWORD=from-file\nSEC_CONTACT_EMAIL=file@whalewatch.io\n")
        monkeypatch.setenv("POSTGRES_PASSWORD", "from-environment")
        monkeypatch.setenv("SEC_CONTACT_EMAIL", "env@whalewatch.io")

        settings = Settings(_env_file=env_file)

        assert settings.postgres_password.get_secret_value() == "from-environment"


class TestGetSettings:
    def test_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One instance per process, so overriding it in a test overrides it
        everywhere."""
        for key, value in REQUIRED.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        assert get_settings() is get_settings()

        get_settings.cache_clear()
