"""``seed-investors``, end to end: a YAML file in, ``filer`` and ``filer_cik`` out.

The properties worth a real database are the ones about a *second* run, because
the seed is run on every deploy and by hand whenever the list changes: running
it again changes nothing, an edit to an entry lands on the existing row, and a
renamed slug is refused rather than quietly minting a second page for one fund.

Like ``test_cli_ingest_filing``, the command commits through ``session_scope``,
so tables are truncated around each test instead of rolled back.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Executable, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import Filer, FilerCik, Filing, Holding, Security
from app.ingestion.investors import DEFAULT_INVESTORS_PATH, load_investors
from tests.conftest import make_settings

_BERKSHIRE = """
- slug: berkshire-hathaway
  display_name: Berkshire Hathaway
  manager_name: Warren Buffett
  category: value
  country: US
  ciks: [1067983]
  notes: Files late in the window.
"""

_PERSHING = """
- slug: pershing-square
  display_name: Pershing Square Capital Management
  manager_name: Bill Ackman
  category: activist
  country: US
  ciks: [1336528, 2026053]
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Settings]:
    """Point the command at the test container. See test_cli_ingest_filing."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    yield settings
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


@pytest.fixture(autouse=True)
def clean_tables(migrated_engine: AsyncEngine) -> Iterator[None]:
    _truncate(migrated_engine)
    yield
    _truncate(migrated_engine)


def _truncate(engine: AsyncEngine) -> None:
    _execute(
        engine,
        text("TRUNCATE holding, filing, security, filer_cik, filer RESTART IDENTITY CASCADE"),
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


def _write(tmp_path: Path, *entries: str) -> Path:
    path = tmp_path / "investors.yaml"
    path.write_text("".join(entries), encoding="utf-8")
    return path


def _seed(runner: CliRunner, path: Path, *args: str) -> Any:
    return runner.invoke(app, ["seed-investors", "--file", str(path), *args])


def _filers(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            Filer.slug,
            Filer.name,
            Filer.display_name,
            Filer.manager_name,
            Filer.category,
            Filer.country,
            Filer.notes,
        ).order_by(Filer.slug),
    )


def _ciks(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(Filer.slug, FilerCik.cik)
        .join(Filer, Filer.id == FilerCik.filer_id)
        .order_by(FilerCik.cik),
    )


# --- the happy path ----------------------------------------------------------


def test_the_list_is_written_to_filer_and_filer_cik(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    result = _seed(runner, _write(tmp_path, _BERKSHIRE, _PERSHING))

    assert result.exit_code == 0, result.output
    assert _filers(migrated_engine) == [
        (
            "berkshire-hathaway",
            "Berkshire Hathaway",
            "Berkshire Hathaway",
            "Warren Buffett",
            "value",
            "US",
            "Files late in the window.",
        ),
        (
            "pershing-square",
            "Pershing Square Capital Management",
            "Pershing Square Capital Management",
            "Bill Ackman",
            "activist",
            "US",
            None,
        ),
    ]
    assert _ciks(migrated_engine) == [
        ("berkshire-hathaway", "0001067983"),
        ("pershing-square", "0001336528"),
        ("pershing-square", "0002026053"),
    ]
    assert "2 listed: 2 created, 0 updated, 0 unchanged" in result.stdout
    assert "3 listed: 3 added" in result.stdout


def test_a_second_run_changes_nothing_and_says_so(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    path = _write(tmp_path, _BERKSHIRE, _PERSHING)
    _seed(runner, path)
    filers, ciks = _filers(migrated_engine), _ciks(migrated_engine)

    again = _seed(runner, path)

    assert again.exit_code == 0, again.output
    assert _filers(migrated_engine) == filers
    assert _ciks(migrated_engine) == ciks
    assert "2 listed: 0 created, 0 updated, 2 unchanged" in again.stdout
    assert "3 listed: 0 added" in again.stdout


def test_the_committed_list_seeds_cleanly(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    """The real file against the real schema: every CHECK and CHAR width, and the
    unique CIK constraint across all hundred entries at once."""
    entries = load_investors()

    result = runner.invoke(app, ["seed-investors"])

    assert result.exit_code == 0, result.output
    assert len(_filers(migrated_engine)) == len(entries)
    assert len(_ciks(migrated_engine)) == sum(len(entry.ciks) for entry in entries)
    assert DEFAULT_INVESTORS_PATH.name in result.stdout


# --- edits -------------------------------------------------------------------


def test_an_edited_entry_updates_the_curated_columns_and_not_the_name(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """``name`` is the cover page's, and ingestion's to maintain. A seed that
    rewrote it would undo the loader every time the list was touched."""
    _seed(runner, _write(tmp_path, _BERKSHIRE))
    _execute(
        migrated_engine,
        update(Filer).values(name="BERKSHIRE HATHAWAY INC"),
    )
    edited = _BERKSHIRE.replace("Warren Buffett", "Greg Abel").replace(
        "  notes: Files late in the window.\n", ""
    )

    result = _seed(runner, _write(tmp_path, edited))

    assert result.exit_code == 0, result.output
    assert _filers(migrated_engine) == [
        (
            "berkshire-hathaway",
            "BERKSHIRE HATHAWAY INC",
            "Berkshire Hathaway",
            "Greg Abel",
            "value",
            "US",
            None,
        )
    ]
    assert "1 listed: 0 created, 1 updated, 0 unchanged" in result.stdout
    assert "updated     berkshire-hathaway" in result.stdout


def test_a_cik_added_to_an_entry_is_mapped(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    _seed(runner, _write(tmp_path, _BERKSHIRE))

    result = _seed(runner, _write(tmp_path, _BERKSHIRE.replace("[1067983]", "[1067983, 42]")))

    assert result.exit_code == 0, result.output
    assert _ciks(migrated_engine) == [
        ("berkshire-hathaway", "0000000042"),
        ("berkshire-hathaway", "0001067983"),
    ]
    assert "2 listed: 1 added" in result.stdout


def test_a_cik_removed_from_an_entry_is_kept_and_reported(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Removing a CIK from the file is not removing it from the database: it
    still resolves that CIK's filings to this filer. The output says so."""
    _seed(runner, _write(tmp_path, _PERSHING))

    result = _seed(runner, _write(tmp_path, _PERSHING.replace("[1336528, 2026053]", "[2026053]")))

    assert result.exit_code == 0, result.output
    assert len(_ciks(migrated_engine)) == 2
    assert "CIK 0001336528 on pershing-square is not in the list" in result.stdout


# --- refusals ----------------------------------------------------------------


def test_a_renamed_slug_is_refused_and_nothing_is_written(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The new slug would claim the old one's CIK. Refused, whole-run: the
    other entry in the file does not get written either."""
    _seed(runner, _write(tmp_path, _BERKSHIRE))
    renamed = _BERKSHIRE.replace("slug: berkshire-hathaway", "slug: berkshire")

    result = _seed(runner, _write(tmp_path, renamed, _PERSHING))

    assert result.exit_code == 1
    assert "CIK 0001067983 is listed under 'berkshire' but belongs to 'berkshire-hathaway'" in (
        result.stderr
    )
    assert [row[0] for row in _filers(migrated_engine)] == ["berkshire-hathaway"]


def test_a_cik_owned_by_a_filer_outside_the_list_is_refused(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    _execute(
        migrated_engine,
        insert(Filer).values(id=1, name="Somebody Else", slug="somebody-else"),
        insert(FilerCik).values(filer_id=1, cik="0001067983"),
    )

    result = _seed(runner, _write(tmp_path, _BERKSHIRE))

    assert result.exit_code == 1
    assert "belongs to 'somebody-else'" in result.stderr
    assert _ciks(migrated_engine) == [("somebody-else", "0001067983")]


def test_an_invalid_list_fails_before_touching_the_database(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    result = _seed(runner, _write(tmp_path, _BERKSHIRE.replace("value", "event_driven")))

    assert result.exit_code == 1
    assert "berkshire-hathaway.category" in result.stderr
    assert _filers(migrated_engine) == []


# --- dry run -----------------------------------------------------------------


def test_a_dry_run_reports_the_real_summary_and_writes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    path = _write(tmp_path, _BERKSHIRE, _PERSHING)

    result = _seed(runner, path, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "dry run, nothing written" in result.stdout
    assert "2 listed: 2 created, 0 updated, 0 unchanged" in result.stdout
    assert _filers(migrated_engine) == []
    assert _ciks(migrated_engine) == []


# --- overlap policy and CIK priority -----------------------------------------


def _priorities(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(engine, select(FilerCik.cik, FilerCik.priority).order_by(FilerCik.cik))


def test_cik_priority_follows_list_order(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The order in the file is who wins an overlap, so it has to reach the row."""
    _seed(runner, _write(tmp_path, _PERSHING))

    assert _priorities(migrated_engine) == [("0001336528", 0), ("0002026053", 1)]


def test_reordering_an_entrys_ciks_is_written_and_reported(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    _seed(runner, _write(tmp_path, _PERSHING))

    result = _seed(
        runner, _write(tmp_path, _PERSHING.replace("[1336528, 2026053]", "[2026053, 1336528]"))
    )

    assert result.exit_code == 0, result.output
    assert _priorities(migrated_engine) == [("0001336528", 1), ("0002026053", 0)]
    assert "2 listed: 0 added, 2 reprioritised" in result.stdout


def test_the_overlap_policy_is_written_and_defaults_to_successor(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    summed = _PERSHING.replace(
        "  ciks: [1336528, 2026053]", "  ciks: [1336528, 2026053]\n  overlap: sum"
    )

    result = _seed(runner, _write(tmp_path, _BERKSHIRE, summed))

    assert result.exit_code == 0, result.output
    assert _fetch(migrated_engine, select(Filer.slug, Filer.overlap).order_by(Filer.slug)) == [
        ("berkshire-hathaway", "successor"),
        ("pershing-square", "sum"),
    ]
    assert "sum: pershing-square" in result.stdout


# --- audit-overlaps ----------------------------------------------------------


def test_audit_overlaps_with_nothing_loaded_says_so(runner: CliRunner) -> None:
    result = runner.invoke(app, ["audit-overlaps"])

    assert result.exit_code == 0, result.output
    assert "no overlapping periods" in result.stdout


def test_audit_overlaps_reports_a_duplicated_book(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """End to end through the seed: priorities from the file decide the primary."""
    _seed(runner, _write(tmp_path, _PERSHING))
    filed = "2025-11-14 21:00:00+00"
    _execute(
        migrated_engine,
        insert(Security).values(id=1, cusip="037833100"),
        *(
            insert(Filing).values(
                id=index,
                accession_no=f"{cik}-25-000001",
                cik=cik,
                filer_id=1,
                form_type="13F-HR",
                period_of_report=date(2025, 9, 30),
                filed_at=text(f"'{filed}'::timestamptz"),
                value_multiplier=1,
                parse_status="ok",
            )
            for index, cik in ((1, "0001336528"), (2, "0002026053"))
        ),
        *(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=1,
                filer_id=1,
                period_of_report=date(2025, 9, 30),
                cusip="037833100",
                shares=Decimal(1_000),
                value_usd=Decimal(250_000),
                sshprnamt_type="SH",
            )
            for filing_id in (1, 2)
        ),
    )

    result = runner.invoke(app, ["audit-overlaps", "--filer", "pershing-square"])

    assert result.exit_code == 0, result.output
    assert "1 overlapping periods across 1 filers, 0 disagreeing" in result.stdout
    assert "pershing-square  2025-09-30  0002026053 vs 0001336528" in result.stdout
    assert "100% identical -> same book" in result.stdout
    assert "policy successor: agrees" in result.stdout
