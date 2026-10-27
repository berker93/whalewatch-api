"""The account verbs, committing to the container: ``create-user`` carrying a signup
request's answers over, and ``delete-user``, ``restore-user`` and ``purge-accounts``.

These commit, unlike the endpoint tests, so the tables are emptied around each.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Executable, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import AccountDeletion, AppUser, ConsentRecord, IngestionRun, SignupRequest
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis
from tests.integration.accounts import PASSWORD, Outbox


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.create_redis", lambda settings: FakeRedis())
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr("app.accounts.delivery.mailer_for", lambda settings: box)
    return box


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE app_user, signup_request, account_deletion, ingestion_run "
            "RESTART IDENTITY CASCADE"
        ),
    )


def _execute(engine: AsyncEngine, *statements: Executable) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)

    asyncio.run(run())


def _fetch(engine: AsyncEngine, statement: Executable) -> list[tuple[Any, ...]]:
    async def run() -> list[tuple[Any, ...]]:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(statement)).all()]

    return asyncio.run(run())


def _create(runner: CliRunner, email: str = "ada@example.com") -> Any:
    return runner.invoke(
        app,
        ["create-user", "--email", email, "--name", "Ada Lovelace", "--password-stdin"],
        input=PASSWORD + "\n",
    )


def _ledger(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            ConsentRecord.purpose,
            ConsentRecord.granted,
            ConsentRecord.terms_version,
            ConsentRecord.source,
        ).order_by(ConsentRecord.id),
    )


def test_create_user_carries_a_signup_requests_answers_over_and_forgets_it(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _execute(
        migrated_engine,
        insert(SignupRequest).values(
            email="ada@example.com",
            name="Ada",
            terms_version="2026-01-01",
            privacy_version="2026-01-01",
            marketing_email=True,
        ),
    )

    result = _create(runner)

    assert result.exit_code == 0, result.output
    assert _ledger(migrated_engine) == [
        ("account", True, "2026-01-01", "create_user"),
        ("product_email", True, None, "create_user"),
        ("marketing_email", True, None, "create_user"),
    ]
    assert _fetch(migrated_engine, select(SignupRequest.email)) == []


def test_create_user_with_no_request_says_no_terms_are_on_file(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    result = _create(runner)

    assert result.exit_code == 0, result.output
    assert "no terms accepted on file" in result.output
    assert _ledger(migrated_engine) == [
        ("account", True, None, "create_user"),
        ("product_email", True, None, "create_user"),
        ("marketing_email", False, None, "create_user"),
    ]


def test_delete_then_restore_then_delete_and_purge(
    runner: CliRunner, migrated_engine: AsyncEngine, outbox: Outbox
) -> None:
    _create(runner)

    deleted = runner.invoke(app, ["delete-user", "--email", "ADA@example.com"])

    assert deleted.exit_code == 0, deleted.output
    assert [m.subject for m in outbox.sent] == ["Your WhaleWatch account is deleted"]
    [(requested_by,)] = _fetch(migrated_engine, select(AccountDeletion.requested_by))
    assert requested_by == "operator"

    restored = runner.invoke(app, ["restore-user", "--email", "ada@example.com"])

    assert restored.exit_code == 0, restored.output
    assert _fetch(migrated_engine, select(AppUser.deleted_at)) == [(None,)]

    runner.invoke(app, ["delete-user", "--email", "ada@example.com"])
    _execute(
        migrated_engine,
        update(AccountDeletion)
        .where(AccountDeletion.restored_at.is_(None))
        .values(purge_after=AccountDeletion.requested_at - timedelta(seconds=1)),
    )

    purged = runner.invoke(app, ["purge-accounts"])

    assert purged.exit_code == 0, purged.output
    assert "1 account erased" in purged.output
    assert _fetch(migrated_engine, select(AppUser.id)) == []
    assert _ledger(migrated_engine) == []
    # Both deletions are on record: the restored one and the purged one.
    deletions = _fetch(
        migrated_engine,
        select(
            AccountDeletion.restored_at.is_not(None), AccountDeletion.purged_at.is_not(None)
        ).order_by(AccountDeletion.id),
    )
    assert deletions == [(True, False), (False, True)]
    [(context, metrics)] = _fetch(
        migrated_engine,
        select(IngestionRun.context, IngestionRun.metrics).where(
            IngestionRun.job_name == "purge-accounts"
        ),
    )
    # Counts, never who.
    assert "ada" not in str(context) + str(metrics)
    assert metrics["erased"]["app_user"] == 1


def test_deleting_an_account_that_is_not_there_says_so(runner: CliRunner) -> None:
    result = runner.invoke(app, ["delete-user", "--email", "nobody@example.com"])

    assert result.exit_code == 1
    assert "no account has the login nobody@example.com" in result.output


def test_restoring_past_the_purge_is_refused(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    _create(runner)
    runner.invoke(app, ["delete-user", "--email", "ada@example.com"])
    _execute(
        migrated_engine,
        update(AccountDeletion).values(purge_after=AccountDeletion.requested_at),
    )
    runner.invoke(app, ["purge-accounts"])

    result = runner.invoke(app, ["restore-user", "--email", "ada@example.com"])

    assert result.exit_code == 1
    assert "no deleted account waiting for its purge" in result.output
