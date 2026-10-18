"""The account rules that need no database: hashing, the field rules, the links, the settings."""

import pytest
from pydantic import ValidationError

from app.accounts import rules
from app.accounts.mail import CONFIRM_EMAIL_PATH, email_change_confirmation, link
from app.accounts.passwords import (
    hash_password_sync,
    needs_rehash,
    verify_password_sync,
)
from app.accounts.sessions import digest, new_secret
from tests.conftest import make_settings

# --- passwords ------------------------------------------------------------------


def test_a_password_verifies_against_its_hash_and_nothing_else() -> None:
    stored = hash_password_sync("correct horse 42")

    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password_sync("correct horse 42", stored)
    assert not verify_password_sync("correct horse 43", stored)
    assert not needs_rehash(stored)


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hash_password_sync("same") != hash_password_sync("same")


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$x$y", "scrypt$a$b$c$d$e"])
def test_an_unreadable_hash_is_a_wrong_password_not_an_error(stored: str) -> None:
    assert not verify_password_sync("anything", stored)


def test_a_hash_with_other_parameters_is_rehashed() -> None:
    assert needs_rehash("scrypt$1024$8$1$c2FsdA==$a2V5")


# --- secrets --------------------------------------------------------------------


def test_a_secret_is_long_random_and_stored_as_its_hash() -> None:
    secret = new_secret()

    assert len(secret) >= 43
    assert secret != new_secret()
    assert len(digest(secret)) == 64
    assert digest(secret) == digest(secret)


# --- the field rules: the frontend's, or looser ----------------------------------


@pytest.mark.parametrize(
    ("name", "ok"), [("Al", True), ("A", False), ("", False), ("x" * 200, True), ("x" * 201, False)]
)
def test_a_name_is_two_characters_or_more(name: str, ok: bool) -> None:
    assert (rules.name_problem(rules.normalise_name(name)) is None) == ok


def test_a_name_is_trimmed_before_it_is_counted() -> None:
    assert rules.name_problem(rules.normalise_name("  A  ")) is not None


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("ada@example.com", True),
        ("a@b.co", True),
        ("ada@example", False),
        ("ada example@x.com", False),
        ("@example.com", False),
        ("ada@@example.com", False),
    ],
)
def test_an_email_is_the_frontends_pattern(email: str, ok: bool) -> None:
    assert (rules.email_problem(email) is None) == ok


def test_an_email_is_trimmed_and_lowercased() -> None:
    assert rules.normalise_email("  Ada@Example.COM ") == "ada@example.com"


@pytest.mark.parametrize(("password", "ok"), [("1234567890", True), ("123456789", False)])
def test_a_password_is_ten_characters_or_more(password: str, ok: bool) -> None:
    assert (rules.password_problem(password) is None) == ok


def test_spaces_count_in_a_password() -> None:
    assert rules.password_problem(" " * 10) is None


# --- links and settings ---------------------------------------------------------


def test_a_link_is_on_the_web_url_with_the_token_encoded() -> None:
    settings = make_settings(web_url="https://app.whalewatch.io/")

    assert link(settings, CONFIRM_EMAIL_PATH, "a+b/c") == (
        "https://app.whalewatch.io/account/confirm-email?token=a%2Bb%2Fc"
    )


def test_the_web_url_defaults_to_the_frontends_dev_app_surface() -> None:
    message = email_change_confirmation(make_settings(), to="ada@x.io", token="t", hours=24)

    assert "http://app.localhost:5173/account/confirm-email?token=t" in message.body


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_deployed_api_needs_a_web_url_and_real_mail(environment: str) -> None:
    deployed = {
        "environment": environment,
        "raw_store_backend": "s3",
        "raw_store_s3_bucket": "b",
        "cors_origins": ["https://whalewatch.io"],
    }
    with pytest.raises(ValidationError, match="WEB_URL must be set"):
        make_settings(**deployed, web_url=None, email_backend="smtp", smtp_host="smtp.x")
    with pytest.raises(ValidationError, match="EMAIL_BACKEND=log"):
        make_settings(**deployed, web_url="https://app.whalewatch.io", email_backend="log")


def test_smtp_needs_a_host() -> None:
    with pytest.raises(ValidationError, match="SMTP_HOST"):
        make_settings(email_backend="smtp")


def test_the_session_cookie_is_secure_only_where_deployed() -> None:
    assert not make_settings().session_cookie_secure
    assert make_settings(environment="production").session_cookie_secure
