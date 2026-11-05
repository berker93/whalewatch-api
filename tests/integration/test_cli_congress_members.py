"""CON-5, against a real Postgres: the member registry, and filings matched to it.

``seed-congress-members --from-dir`` reads the fixture cut of the
congress-legislators dataset (tests/fixtures/congress_legislators/), so nothing
is downloaded; ``ingest-house-index`` reads ZIPs from a fake fetcher, as in
tests/integration/test_cli_ingest_house_index.py. What is asserted is what the
runs leave behind — the members, the filed names and their matches — and that
a person's match survives every later run.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Executable, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.db.models import (
    CongressFiledName,
    CongressFiling,
    CongressMember,
    CongressMemberCandidate,
    CongressMemberTerm,
)
from app.ingestion.congress.house_clerk import index_url
from app.ingestion.remote.fetcher import FetchRequest, FetchResponse
from app.ingestion.remote.handler import CookieRecord
from tests.house_index_fixtures import house_zip, member

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "congress_legislators"
TODAY: Final = date(2026, 11, 5)

PELOSI: Final = member("20033725", "Pelosi", "Nancy", office="CA11", filed="1/23/2026")
KHANNA: Final = member("20033801", "Khanna", "Rohit", office="CA17", filed="2/2/2026")
KHANNA_LATER: Final = member("20034410", "Khanna", "Rohit", office="CA17", filed="9/14/2026")
KEAN: Final = member("20033900", "Kean", "Thomas H.", office="NJ07", filed="3/4/2026", suffix="Jr")
KEAN_AGAIN: Final = member("20034000", "Kean", "Thomas H", office="NJ07", filed="4/1/2026")
STRANGER: Final = member("20034100", "Quartermaine", "Zebulon", office="WY00", filed="5/5/2026")


class FakeFetcher:
    """The House Clerk's 2026 ZIP, as :meth:`serve` last set it."""

    backend = "local"

    def __init__(self) -> None:
        self.answers: dict[str, bytes] = {}

    def serve(self, *members: dict[str, str]) -> None:
        self.answers[index_url(2026)] = house_zip(2026, *members)

    async def fetch(
        self, request: FetchRequest, cookies: Sequence[CookieRecord] = ()
    ) -> FetchResponse:
        body = self.answers.get(request.url)
        status = 200 if body is not None else 404
        return FetchResponse(status=status, url=request.url, headers=(), body=body or b"")


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    migrated_engine: AsyncEngine,
    fetcher: FakeFetcher,
) -> Iterator[Settings]:
    @asynccontextmanager
    async def open_fake(_: Settings) -> AsyncIterator[FakeFetcher]:
        yield fetcher

    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli.new_york_today", lambda: TODAY)
    monkeypatch.setattr("app.jobs.congress.open_fetcher", open_fake)
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
            "TRUNCATE ingestion_run, crawl_watermark, congress_filing, congress_filed_name, "
            "congress_member RESTART IDENTITY CASCADE"
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


def _seed(runner: CliRunner) -> str:
    result = runner.invoke(app, ["seed-congress-members", "--from-dir", str(FIXTURES)])
    assert result.exit_code == 0, result.output
    return result.output


def _ingest(runner: CliRunner) -> str:
    result = runner.invoke(app, ["ingest-house-index", "--year", "2026"])
    assert result.exit_code == 0, result.output
    return result.output


def _names(engine: AsyncEngine) -> dict[str, tuple[Any, ...]]:
    """Each filed name's key: status, method, the member's Bioguide id, filings."""
    filings = (
        select(func.count())
        .where(CongressFiling.filed_name_id == CongressFiledName.id)
        .scalar_subquery()
    )
    rows = _fetch(
        engine,
        select(
            CongressFiledName.name_key,
            CongressFiledName.status,
            CongressFiledName.method,
            CongressMember.bioguide_id,
            filings,
        ).outerjoin(CongressMember, CongressMember.id == CongressFiledName.member_id),
    )
    return {row[0]: row[1:] for row in rows}


# --- seeding -------------------------------------------------------------------------


def test_a_seed_writes_everyone_since_2008_with_their_terms(
    runner: CliRunner, migrated_engine: AsyncEngine
) -> None:
    output = _seed(runner)

    members = dict(
        _fetch(migrated_engine, select(CongressMember.bioguide_id, CongressMember.full_name))
    )
    assert len(members) == 13
    assert members["P000197"] == "Nancy Pelosi"
    assert "B000226" not in members  # last served in 1793
    pelosi = _fetch(
        migrated_engine,
        select(
            CongressMember.chamber,
            CongressMember.state,
            CongressMember.district,
            CongressMember.party,
            CongressMember.current,
        ).where(CongressMember.bioguide_id == "P000197"),
    )
    assert pelosi == [("house", "CA", 11, "Democrat", True)]
    terms = _fetch(
        migrated_engine,
        select(func.count())
        .select_from(CongressMemberTerm)
        .join(CongressMember)
        .where(CongressMember.bioguide_id == "P000197"),
    )
    assert terms[0][0] >= 20  # since 1987
    assert "14 listed" in output
    assert "13 since 2008" in output
    assert "13 new" in output


def test_a_second_seed_changes_nothing(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    _seed(runner)
    before = _fetch(migrated_engine, select(CongressMember.id, CongressMember.updated_at))

    output = _seed(runner)

    assert _fetch(migrated_engine, select(CongressMember.id, CongressMember.updated_at)) == before
    assert "0 new  0 changed" in output


def test_a_seed_archives_each_file_under_its_checksum(
    runner: CliRunner, settings: Settings
) -> None:
    _seed(runner)

    archived = sorted(
        path.relative_to(settings.raw_store_local_root).as_posix()
        for path in Path(settings.raw_store_local_root, "raw", "congress-legislators").rglob(
            "*.json"
        )
    )
    assert [key.split("/")[2] for key in archived] == [
        "legislators-current",
        "legislators-historical",
    ]


def test_a_seed_of_files_that_are_not_the_dataset_writes_nothing(
    runner: CliRunner, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    (tmp_path / "legislators-current.json").write_text("[]")
    (tmp_path / "legislators-historical.json").write_text("{not json")

    result = runner.invoke(app, ["seed-congress-members", "--from-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "not JSON" in result.output
    assert _fetch(migrated_engine, select(func.count()).select_from(CongressMember)) == [(0,)]


# --- matching -------------------------------------------------------------------------


def test_an_index_run_links_its_filings_and_matches_the_new_names(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _seed(runner)
    fetcher.serve(PELOSI, KHANNA, KHANNA_LATER, KEAN, KEAN_AGAIN, STRANGER)

    output = _ingest(runner)

    assert _names(migrated_engine) == {
        "nancy pelosi": ("matched", "exact", "P000197", 1),
        # Two spellings, one name: the suffix is out, and so is the full stop.
        "thomas h kean": ("matched", "exact", "K000398", 2),
        # Ro Khanna, as the index has him: trigram, so a person decides.
        "rohit khanna": ("review", None, None, 2),
        "zebulon quartermaine": ("unmatched", None, None, 1),
    }
    candidates = _fetch(
        migrated_engine,
        select(CongressMember.bioguide_id, CongressMemberCandidate.method)
        .join(CongressMember)
        .where(CongressMemberCandidate.status == "pending"),
    )
    assert candidates == [("K000389", "trigram")]
    assert "6 filings linked, 4 new names" in output
    assert "2 matched (2 exact), 1 to review, 1 unmatched" in output


def test_filings_ingested_before_the_seed_are_matched_by_it(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    fetcher.serve(PELOSI, STRANGER)
    output = _ingest(runner)
    assert "none matched: no members seeded" in output
    assert _names(migrated_engine)["nancy pelosi"][:1] == ("unmatched",)

    _seed(runner)

    assert _names(migrated_engine)["nancy pelosi"] == ("matched", "exact", "P000197", 1)


def test_a_persons_match_survives_reingestion_and_reseeding(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    """As a manual CUSIP resolution does (SEC-3): no run changes it."""
    _seed(runner)
    fetcher.serve(PELOSI, KHANNA)
    _ingest(runner)
    # What the console's accept does (tests/integration/test_admin_congress_members.py).
    _execute(
        migrated_engine,
        update(CongressFiledName)
        .where(CongressFiledName.name_key == "rohit khanna")
        .values(
            status="matched",
            member_id=select(CongressMember.id)
            .where(CongressMember.bioguide_id == "K000389")
            .scalar_subquery(),
            method="manual",
            confidence=1.0,
            reviewed_at=func.now(),
        ),
    )

    # The Clerk corrects the filing date: the row changes, is unlinked, is relinked.
    fetcher.serve(PELOSI, {**KHANNA, "FilingDate": "2/3/2026"}, KHANNA_LATER)
    _ingest(runner)
    _seed(runner)
    runner.invoke(app, ["match-congress-members"])

    assert _names(migrated_engine)["rohit khanna"] == ("matched", "manual", "K000389", 2)


def test_a_filing_whose_name_the_index_corrects_moves_to_the_new_name(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    _seed(runner)
    fetcher.serve({**PELOSI, "First": "Nanci"})
    _ingest(runner)
    assert _names(migrated_engine)["nanci pelosi"][0] == "review"

    fetcher.serve(PELOSI)
    _ingest(runner)

    names = _names(migrated_engine)
    assert names["nancy pelosi"] == ("matched", "exact", "P000197", 1)
    assert names["nanci pelosi"][-1] == 0  # left with no filings, and out of the queue


def test_match_congress_members_rematches_after_a_registry_change(
    runner: CliRunner, migrated_engine: AsyncEngine, fetcher: FakeFetcher
) -> None:
    """A member removed from the registry and seeded again: the name is unmatched,
    then matched once they are back."""
    _seed(runner)
    fetcher.serve(STRANGER)
    _ingest(runner)
    assert _names(migrated_engine)["zebulon quartermaine"][0] == "unmatched"
    _execute(
        migrated_engine,
        update(CongressMember)
        .where(CongressMember.bioguide_id == "B001236")
        .values(first_name="Zebulon", last_name="Quartermaine", full_name="Zebulon Quartermaine"),
        update(CongressMemberTerm)
        .where(
            CongressMemberTerm.member_id
            == select(CongressMember.id)
            .where(CongressMember.bioguide_id == "B001236")
            .scalar_subquery()
        )
        .values(chamber="house", state="WY", district=0),
    )

    result = runner.invoke(app, ["match-congress-members"])

    assert result.exit_code == 0, result.output
    assert _names(migrated_engine)["zebulon quartermaine"] == ("matched", "exact", "B001236", 1)
    assert "1 changed" in result.output
