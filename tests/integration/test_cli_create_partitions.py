"""``create-partitions``: insider_transaction's years, created through a year.

Against a real Postgres. The run commits, so each test drops what it created.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import IngestionRun
from tests.conftest import make_settings


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def drop_created(migrated_engine: AsyncEngine) -> Iterator[None]:
    yield
    _execute(
        migrated_engine,
        "DROP TABLE IF EXISTS insider_transaction_2028, insider_transaction_2029",
        "DELETE FROM ingestion_run WHERE job_name = 'create-partitions'",
    )


def _execute(engine: AsyncEngine, *statements: str) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))

    asyncio.run(run())


def _last_run(engine: AsyncEngine) -> tuple[Any, ...]:
    async def run() -> tuple[Any, ...]:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(IngestionRun.status, IngestionRun.items_written, IngestionRun.metrics)
                    .where(IngestionRun.job_name == "create-partitions")
                    .order_by(IngestionRun.started_at.desc())
                    .limit(1)
                )
            ).one()
            return tuple(row)

    return asyncio.run(run())


def test_the_missing_years_are_created_and_recorded(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    result = runner.invoke(app, ["create-partitions", "--through", "2029"])

    assert result.exit_code == 0, result.output
    assert "create-partitions  2 partitions created" in result.output
    assert "2029" in result.output and "insider_transaction_2029" in result.output
    assert _last_run(migrated_engine) == (
        "success",
        2,
        {"partitions_created": {"insider_transaction_2028": 0, "insider_transaction_2029": 0}},
    )


def test_by_default_it_is_next_year_and_already_done(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, migrated_engine: AsyncEngine
) -> None:
    """The migration created through 2027, which is next year as it was written."""
    monkeypatch.setattr("app.cli.next_year", lambda: 2027)

    result = runner.invoke(app, ["create-partitions"])

    assert result.exit_code == 0, result.output
    assert "every year through 2027 has a partition" in result.output
    assert _last_run(migrated_engine)[:2] == ("success", 0)
