"""CON-6, against a real Postgres: ``resolve-congress-assets``,
``congress-asset-coverage`` and ``congress-asset-override`` from the shell.

The lines are committed as a PTR read leaves them; what is asserted is what
each verb prints and leaves behind. The resolver's cases are
tests/integration/test_congress_asset_resolution.py's.
"""

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any, Final

import pytest
from sqlalchemy import Executable, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.db.models import AdminAction, CongressFiling, CongressTransaction, Security

#: A key no other test overrides: admin_action is append-only, so this run's
#: row outlives the test, and is told apart by it.
NAME: Final = "Ajinomoto Company, Inc. Ordinary Shares"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    yield settings


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text(
            "TRUNCATE ingestion_run, congress_asset_override, congress_filing, security_alias, "
            "security RESTART IDENTITY CASCADE"
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


@pytest.fixture
def lines(migrated_engine: AsyncEngine) -> None:
    """Microsoft, which resolves; Ajinomoto, which does not; a Treasury bill."""
    _execute(
        migrated_engine,
        insert(Security).values(
            [
                {
                    "cusip": "594918104",
                    "name": "MICROSOFT CORP",
                    "ticker": "MSFT",
                    "resolution_source": "openfigi",
                },
                # A CUSIP no list has named: nothing a name search can find.
                {
                    "cusip": "J00882126",
                    "name": None,
                    "ticker": None,
                    "resolution_source": None,
                },
            ]
        ),
        insert(CongressFiling).values(
            id=1,
            chamber="house",
            doc_id="20033725",
            report_type="ptr",
            last_name="Pelosi",
            filing_date=date(2026, 7, 8),
            year=2026,
            source_key="raw/house-clerk/index/2026/x.zip",
            parser_version=1,
        ),
        insert(CongressTransaction).values(
            [
                {
                    "filing_id": 1,
                    "line": number,
                    "parse_status": "parsed",
                    "extraction": "text",
                    "asset_name": name,
                    "ticker": ticker,
                    "asset_type": code,
                    "transaction_type": "purchase",
                    "transaction_date": date(2026, 6, 9),
                    "amount_low": 1_001,
                    "amount_high": 15_000,
                }
                for number, (name, ticker, code) in enumerate(
                    (
                        ("Microsoft Corporation - Common Stock", "MSFT", "ST"),
                        (NAME, "AJINF", "ST"),
                        ("U.S. Treasury Bills", None, "GS"),
                    ),
                    start=1,
                )
            ]
        ),
    )


def _invoke(runner: CliRunner, *args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, result.output
    return result.output


def test_resolve_then_report_coverage_below_the_gate(
    runner: CliRunner, lines: None, migrated_engine: AsyncEngine
) -> None:
    resolved = _invoke(runner, "resolve-congress-assets")
    gated = _invoke(runner, "congress-asset-coverage", code=1)
    passes = _invoke(runner, "congress-asset-coverage", "--threshold", "50")

    assert "3 lines resolved: 1 to a security (1 by ticker), 1 not, 1 not stocks" in resolved
    assert "1/2 stock lines (50.00%)" in resolved
    assert "BELOW for house the 95% gate" in gated
    assert "passes the 50% gate" in passes
    assert "2 equity, 1 treasury" in passes


def test_an_override_from_the_shell_resolves_the_key_and_is_logged(
    runner: CliRunner, lines: None, migrated_engine: AsyncEngine
) -> None:
    _invoke(runner, "resolve-congress-assets")
    [(line_id,)] = _fetch(
        migrated_engine,
        select(CongressTransaction.id).where(CongressTransaction.asset_name == NAME),
    )

    made = _invoke(
        runner,
        "congress-asset-override",
        "--line",
        str(line_id),
        "--class",
        "equity",
        "--cusip",
        "J00882126",
        "--note",
        "the ordinary",
    )
    covered = _invoke(runner, "congress-asset-coverage")
    removed = _invoke(runner, "congress-asset-override", "--line", str(line_id), "--remove")

    assert "equity  J00882126" in made and "1 resolved so" in made
    assert "2/2 stock lines (100.00%)" in covered
    assert "1 lines resolved by a run's rules again" in removed
    logged = _fetch(
        migrated_engine,
        select(AdminAction.action, AdminAction.source)
        .where(
            AdminAction.detail["asset_key"].astext
            == "ST|AJINF|ajinomoto company inc ordinary shares"
        )
        .order_by(AdminAction.id.desc())
        .limit(2),
    )
    assert logged == [("asset_override_removed", "cli"), ("asset_overridden", "cli")]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--class", "equity"], "give one of --line and --key"),
        (["--line", "1", "--key", "x", "--class", "other"], "give one of --line and --key"),
        (["--line", "1", "--class", "bonds"], "is not an asset class"),
        (["--line", "1", "--remove", "--class", "other"], "--remove takes no"),
        (["--line", "999", "--class", "other"], "No resolved PTR line 999"),
        (["--key", "ST|XX|nothing", "--remove"], "no override for"),
    ],
)
def test_an_override_from_the_shell_is_refused_in_one_line(
    runner: CliRunner, lines: None, args: list[str], message: str
) -> None:
    _invoke(runner, "resolve-congress-assets")

    result = runner.invoke(app, ["congress-asset-override", *args])

    assert result.exit_code == 1
    assert message in result.output
