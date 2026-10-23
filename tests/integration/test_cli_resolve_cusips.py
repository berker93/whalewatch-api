"""``resolve-cusips``, ``resolve-cusip`` and ``cusip-coverage``, end to end.

Against a real Postgres, with a fake in place of the OpenFIGI client: the
client has its own tests (tests/test_openfigi_client.py), and what is under
test here is what is asked of it, in what order, and what is written from the
answers. The commands commit through their own sessions, so tables are
truncated around each test rather than rolled back, as in
test_cli_import_13f_list.
"""

import asyncio
import logging
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from types import TracebackType
from typing import Any, Final, Self

import pytest
from sqlalchemy import Executable, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from typer.testing import CliRunner

from app.api.cache import KEY_PREFIX
from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import (
    Filer,
    FilerCik,
    Filing,
    Holding,
    IngestionRun,
    ManualReview,
    Sec13FSecurity,
    Security,
    SecurityAlias,
)
from app.ingestion.enrichment.figi import FIGIResult
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

Q1: Final = date(2026, 3, 31)
Q2: Final = date(2026, 6, 30)

APPLE: Final = "037833100"
APPLE_OLD: Final = "037833902"
EXXON: Final = "30231G102"
BERKSHIRE_B: Final = "084670702"
OBSCURE: Final = "999999999"

_ids = count(1)


def _us(ticker: str, figi: str, name: str) -> dict[str, Any]:
    return {
        "data": [
            {
                "figi": figi,
                "ticker": ticker,
                "exchCode": "US",
                "marketSector": "Equity",
                "compositeFIGI": figi,
                "name": name,
            }
        ]
    }


MISS: Final = {"warning": "No identifier found."}

#: OpenFIGI as these tests know it. Anything else is a miss.
ANSWERS: Final = {
    APPLE: _us("AAPL", "BBG000B9XRY4", "APPLE INC"),
    BERKSHIRE_B: _us("BRK/B", "BBG000DWG505", "BERKSHIRE HATHAWAY INC-CL B"),
    # Only abroad, as OpenFIGI really has Exxon's own CUSIP.
    EXXON: {
        "data": [
            {
                "figi": "BBG00B8TC1V3",
                "ticker": "EXMOC",
                "exchCode": "CP",
                "marketSector": "Equity",
                "name": "EXXON MOBIL CORP",
            }
        ]
    },
}


class FakeOpenFIGI:
    """Stands in for ``OpenFIGIClient(settings, log)``.

    Answers from :data:`ANSWERS`, or :attr:`answers` if a test changes them.
    An answer not refreshed is dated :attr:`logged_at`'s entry for the CUSIP,
    as if it came from the response log; a refreshed one is dated now. Real
    now: the command's thirty days are counted from the clock.
    """

    def __init__(self) -> None:
        self.answers = dict(ANSWERS)
        self.logged_at: dict[str, datetime] = {}
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, settings: Settings, log: object) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def map_cusips(
        self, cusips: Sequence[str], *, refresh: bool = False
    ) -> dict[str, FIGIResult]:
        self.calls.append((list(cusips), refresh))
        now = datetime.now(UTC)
        return {
            cusip: FIGIResult.from_response(
                cusip,
                self.answers.get(cusip, MISS),
                now if refresh else self.logged_at.get(cusip, now),
            )
            for cusip in cusips
        }

    @property
    def asked(self) -> list[str]:
        return [cusip for cusips, _ in self.calls for cusip in cusips]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def figi(monkeypatch: pytest.MonkeyPatch) -> FakeOpenFIGI:
    fake = FakeOpenFIGI()
    monkeypatch.setattr("app.jobs.enrichment.OpenFIGIClient", fake)
    return fake


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.jobs.cache.create_redis", lambda settings: fake)
    return fake


@pytest.fixture(autouse=True)
def cli_settings(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[Settings]:
    """The container's DSN; see test_cli_ingest_filing."""
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
        text(
            "TRUNCATE ingestion_run, manual_review, security_alias, holding, filing, "
            "security, filer_cik, filer, sec_13f_security RESTART IDENTITY CASCADE"
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


def _held(engine: AsyncEngine, **dollars: int) -> None:
    """One filer's Q2 13F holding each security (``cusip_or_alias=dollars``)."""
    positions = {_CUSIPS.get(key, key): value for key, value in dollars.items()}

    async def run() -> None:
        async with engine.begin() as connection:
            await _filing(connection, positions)

    asyncio.run(run())


async def _filing(connection: AsyncConnection, positions: dict[str, int]) -> None:
    n = next(_ids)
    filer_id = await connection.scalar(
        insert(Filer).values(name=f"FUND {n}", slug=f"fund-{n}").returning(Filer.id)
    )
    cik = f"{n:010d}"
    await connection.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=0))
    filing_id = await connection.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-26-{n:06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR",
            period_of_report=Q2,
            filed_at=datetime(2026, 8, 14, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    for cusip, value in positions.items():
        security_id = await connection.scalar(
            select(Security.id).where(Security.cusip == cusip)
        ) or await connection.scalar(
            insert(Security)
            .values(cusip=cusip, name=_FILED[cusip], filed_name=_FILED[cusip])
            .returning(Security.id)
        )
        await connection.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=security_id,
                filer_id=filer_id,
                period_of_report=Q2,
                cusip=cusip,
                value_usd=Decimal(value),
                shares=Decimal(value) / 10,
                sshprnamt_type="SH",
            )
        )


_CUSIPS: Final = {
    "apple": APPLE,
    "apple_old": APPLE_OLD,
    "exxon": EXXON,
    "berkshire_b": BERKSHIRE_B,
    "obscure": OBSCURE,
}
_FILED: Final = {
    APPLE: "APPLE INC",
    APPLE_OLD: "APPLE INC",
    EXXON: "EXXON MOBIL CORP",
    BERKSHIRE_B: "BERKSHIRE HATHAWAY INC DEL",
    OBSCURE: "OBSCURE CO - converted at IPO",
}


def _listed(engine: AsyncEngine, cusip: str, name: str, *, period: date = Q2) -> None:
    _execute(
        engine,
        insert(Sec13FSecurity).values(
            period_of_report=period,
            cusip=cusip,
            issuer_name=name,
            status="listed",
            has_listed_options=False,
        ),
    )


def _history(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    alias = SecurityAlias
    return _fetch(
        engine,
        select(Security.cusip, alias.ticker, alias.valid_from, alias.valid_to, alias.source)
        .join(Security, Security.id == alias.security_id)
        .order_by(Security.cusip, alias.valid_from.nulls_first()),
    )


def _securities(engine: AsyncEngine) -> dict[str, tuple[Any, ...]]:
    rows = _fetch(
        engine,
        select(
            Security.cusip,
            Security.ticker,
            Security.figi,
            Security.name,
            Security.resolution_source,
            Security.resolved_at.is_not(None),
        ),
    )
    return {row[0]: row[1:] for row in rows}


def _reviews(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return _fetch(
        engine,
        select(
            Security.cusip,
            ManualReview.suggested_ticker,
            ManualReview.status,
            ManualReview.closed_at.is_not(None),
        )
        .join(Security, Security.id == ManualReview.security_id)
        .order_by(ManualReview.id),
    )


def _run(runner: CliRunner, *args: str, code: int = 0) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, result.output
    return result.stdout


# --- resolve-cusips ---------------------------------------------------------------


def test_securities_are_taken_highest_value_first_and_limit_takes_the_top(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, obscure=5_000, apple=1_000, exxon=3_000)
    _held(migrated_engine, apple=4_000)  # Apple: 5,000 in all, level with OBSCURE

    _run(runner, "resolve-cusips", "--limit", "2")

    # Equal value goes by CUSIP, so --limit is repeatable.
    assert figi.asked == [APPLE, OBSCURE]
    assert _securities(migrated_engine)[EXXON][3] is None  # not reached


def test_each_source_is_recorded_as_the_one_that_won(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100, exxon=90, berkshire_b=80, obscure=70)
    _listed(migrated_engine, EXXON, "EXXON MOBIL CORP")
    _listed(migrated_engine, APPLE, "APPLE INC (THE LIST)")

    output = _run(runner, "resolve-cusips")

    assert _securities(migrated_engine) == {
        APPLE: ("AAPL", "BBG000B9XRY4", "APPLE INC", "openfigi", True),
        BERKSHIRE_B: ("BRK.B", "BBG000DWG505", "BERKSHIRE HATHAWAY INC-CL B", "openfigi", True),
        # OpenFIGI knows it only abroad: the list names it, and no ticker.
        EXXON: (None, None, "EXXON MOBIL CORP", "13f_list", True),
        # Nothing named it: the filing's name stays, and nothing won.
        OBSCURE: (None, None, "OBSCURE CO - converted at IPO", None, False),
    }
    # Each first ticker opens a range as far back as we know.
    assert _history(migrated_engine) == [
        (APPLE, "AAPL", None, None, "openfigi"),
        (BERKSHIRE_B, "BRK.B", None, None, "openfigi"),
    ]
    assert "openfigi    2 given a ticker" in output
    assert "13(f) list  1 named, no ticker" in output
    assert "coverage    52.94% by value, 50.00% by row" in output  # 180 of 340


def test_the_filed_name_survives_a_resolution(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, berkshire_b=80)

    _run(runner, "resolve-cusips")

    assert _fetch(migrated_engine, select(Security.filed_name)) == [("BERKSHIRE HATHAWAY INC DEL",)]


def test_a_resolved_security_is_not_asked_about_again_without_force(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100, obscure=10)
    _run(runner, "resolve-cusips")
    figi.calls.clear()

    output = _run(runner, "resolve-cusips")

    assert figi.calls == [([OBSCURE], False)]
    assert "changed     0 rows" in output


def test_force_asks_again_and_keeps_a_ticker_openfigi_no_longer_lists(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """A delisting looks like this from OpenFIGI. A ticker is history, and is
    never taken away; the 13(f) lists say whether it is still listed."""
    _held(migrated_engine, apple=100, obscure=10)
    _run(runner, "resolve-cusips")
    figi.calls.clear()
    del figi.answers[APPLE]

    output = _run(runner, "resolve-cusips", "--force")

    assert figi.calls == [([APPLE, OBSCURE], True)]
    assert _securities(migrated_engine)[APPLE] == (
        "AAPL",
        "BBG000B9XRY4",
        "APPLE INC",
        "openfigi",
        True,
    )
    assert "kept        1 OpenFIGI no longer lists keep the ticker they had" in output


def test_a_new_ticker_is_a_rename_in_the_history(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, berkshire_b=100)
    _run(runner, "resolve-cusips")
    figi.answers[BERKSHIRE_B] = _us("BRK/C", "BBG000DWG505", "BERKSHIRE HATHAWAY INC-CL C")

    output = _run(runner, "resolve-cusips", "--force")

    [(today,)] = _fetch(migrated_engine, select(func.current_date()))
    assert _history(migrated_engine) == [
        (BERKSHIRE_B, "BRK.B", None, today, "openfigi"),
        (BERKSHIRE_B, "BRK.C", today, None, "openfigi"),
    ]
    assert "renamed     1 under a new ticker" in output


def test_a_security_off_the_latest_list_is_delisted_and_its_ticker_range_ends(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """Exxon is on the 2026Q1 list and not the 2026Q2 one; Apple is on both."""
    _held(migrated_engine, apple=100, exxon=50)
    for period in (Q1, Q2):
        _listed(migrated_engine, APPLE, "APPLE INC", period=period)
    _listed(migrated_engine, EXXON, "EXXON MOBIL CORP", period=Q1)
    _run(runner, "resolve-cusip", EXXON, "--ticker", "XOM")

    output = _run(runner, "resolve-cusips")

    delisted = dict(_fetch(migrated_engine, select(Security.cusip, Security.delisted_by)))
    assert delisted == {APPLE: None, EXXON: Q2}
    assert (EXXON, "XOM", None, Q2, "manual") in _history(migrated_engine)
    assert "delisted    1 security no longer on the 13(f) list" in output


def test_a_ticker_two_securities_hold_at_once_is_a_collision_until_one_is_delisted(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """OpenFIGI maps Apple's old CUSIP to today's ticker too."""
    _held(migrated_engine, apple=100, apple_old=50)
    figi.answers[APPLE_OLD] = _us("AAPL", "BBG000B9XRY4", "APPLE INC")
    _run(runner, "resolve-cusips")

    colliding = _run(runner, "cusip-coverage")

    assert "collisions  1 ticker held by two securities at once" in colliding
    assert f"    AAPL      {APPLE} APPLE INC […, …); {APPLE_OLD} APPLE INC […, …)" in colliding

    # The old CUSIP left the list after 2026Q1: its range ends, and so does the clash.
    for period in (Q1, Q2):
        _listed(migrated_engine, APPLE, "APPLE INC", period=period)
    _listed(migrated_engine, APPLE_OLD, "APPLE INC", period=Q1)
    _run(runner, "resolve-cusips")

    settled = _run(runner, "cusip-coverage")
    assert "collisions  none" in settled


def test_a_chain_of_cusip_changes_hands_the_ticker_along_the_chain(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """Two reverse splits: Apple's oldest CUSIP left the list after 2026Q1, the
    next after 2026Q2, and OpenFIGI maps all three to AAPL."""
    q3 = date(2026, 9, 30)
    _held(migrated_engine, apple=100, apple_old=50, berkshire_b=10)
    for cusip in (APPLE_OLD, BERKSHIRE_B):
        figi.answers[cusip] = _us("AAPL", "BBG000B9XRY4", "APPLE INC")
    _listed(migrated_engine, APPLE_OLD, "APPLE INC", period=Q1)
    for period in (Q1, Q2):
        _listed(migrated_engine, BERKSHIRE_B, "APPLE INC", period=period)
    for period in (Q1, Q2, q3):
        _listed(migrated_engine, APPLE, "APPLE INC", period=period)

    _run(runner, "resolve-cusips")
    _run(runner, "resolve-cusips")  # and again changes nothing

    assert _history(migrated_engine) == [
        (APPLE, "AAPL", q3, None, "openfigi"),
        (APPLE_OLD, "AAPL", None, Q2, "openfigi"),
        (BERKSHIRE_B, "AAPL", Q2, q3, "openfigi"),
    ]
    assert "collisions  none" in _run(runner, "cusip-coverage")


# --- ticker-alias -------------------------------------------------------------------


def test_a_former_ticker_trims_the_current_range_to_meet_it(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, berkshire_b=100)
    _run(runner, "resolve-cusips")

    output = _run(
        runner,
        "ticker-alias",
        BERKSHIRE_B,
        "--ticker",
        "brk/a",
        "--from",
        "2010-01-01",
        "--to",
        "2020-01-01",
        "--note",
        "made up for the test",
    )
    # Not the middle of BRK.B's range now: that would cut it in two.
    split = runner.invoke(
        app,
        [
            "ticker-alias",
            BERKSHIRE_B,
            "--ticker",
            "X",
            "--from",
            "2021-01-01",
            "--to",
            "2022-01-01",
        ],
    )

    assert _history(migrated_engine) == [
        (BERKSHIRE_B, "BRK.A", date(2010, 1, 1), date(2020, 1, 1), "manual"),
        (BERKSHIRE_B, "BRK.B", date(2020, 1, 1), None, "openfigi"),
    ]
    assert "trimmed     BRK.B […, …) to [2020-01-01, …)" in output
    assert split.exit_code == 1
    assert "would cut BRK.B [2020-01-01, …) in two" in split.stderr


def test_a_former_ticker_cannot_swallow_a_range(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, berkshire_b=100)
    _run(runner, "resolve-cusips")
    _run(runner, "ticker-alias", BERKSHIRE_B, "--ticker", "OLD", "--to", "2020-01-01")

    result = runner.invoke(
        app, ["ticker-alias", BERKSHIRE_B, "--ticker", "OLDER", "--to", "2021-01-01"]
    )

    assert result.exit_code == 1
    assert "OLD […, 2020-01-01) lies wholly inside it" in result.stderr


def test_a_miss_older_than_thirty_days_is_asked_again(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100, obscure=10, exxon=5)
    figi.answers.pop(APPLE)
    now = datetime.now(UTC)
    figi.logged_at = {APPLE: now - timedelta(days=31), OBSCURE: now - timedelta(days=29)}

    _run(runner, "resolve-cusips")

    assert figi.calls == [([APPLE, OBSCURE, EXXON], False), ([APPLE], True)]


def test_a_fuzzy_match_is_queued_for_review_and_never_applied(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """Apple's old CUSIP is named like Apple: a suggestion, not a ticker."""
    _held(migrated_engine, apple=100, apple_old=50, obscure=10)

    output = _run(runner, "resolve-cusips")
    _run(runner, "resolve-cusips")  # and a second run suggests nothing new

    assert _securities(migrated_engine)[APPLE_OLD][0] is None
    assert _reviews(migrated_engine) == [(APPLE_OLD, "AAPL", "pending", False)]
    assert "1 fuzzy match queued for 1 security" in output


def test_resolving_a_security_closes_its_pending_reviews(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100, apple_old=50)
    _run(runner, "resolve-cusips")
    figi.answers[APPLE_OLD] = _us("AAPL", "BBG000B9XRY4", "APPLE INC")

    _run(runner, "resolve-cusips")

    assert _reviews(migrated_engine) == [(APPLE_OLD, "AAPL", "superseded", True)]


def test_resolving_drops_the_apis_cache(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    redis.values = {f"{KEY_PREFIX}stocks.read_stock:0f0f": "{}"}
    _held(migrated_engine, apple=100)

    _run(runner, "resolve-cusips")

    assert redis.values == {}


# --- resolve-cusip ------------------------------------------------------------------


def test_a_manual_resolution_survives_a_forced_re_run(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, exxon=100, apple=50)
    _listed(migrated_engine, EXXON, "EXXON MOBIL CORP")

    output = _run(
        runner, "resolve-cusip", EXXON.lower(), "--ticker", "xom", "--note", "foreign venues only"
    )
    figi.answers[EXXON] = _us("WRONG", "BBG000000000", "SOMETHING ELSE")
    _run(runner, "resolve-cusips", "--force")

    assert figi.asked == [APPLE]
    assert _securities(migrated_engine)[EXXON] == (
        "XOM",
        None,
        "EXXON MOBIL CORP",
        "manual",
        True,
    )
    [(note,)] = _fetch(
        migrated_engine, select(Security.resolution_note).where(Security.cusip == EXXON)
    )
    assert note == "foreign venues only"
    assert "XOM  (was none)" in output


def test_a_manual_resolution_accepts_the_suggestion_it_agrees_with(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100, apple_old=50)
    _run(runner, "resolve-cusips")

    output = _run(runner, "resolve-cusip", APPLE_OLD, "--ticker", "aapl")

    assert _reviews(migrated_engine) == [(APPLE_OLD, "AAPL", "accepted", True)]
    assert "1 accepted, 0 superseded" in output


def test_pinning_a_ticker_keeps_its_figi(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=100)
    _run(runner, "resolve-cusips")

    _run(runner, "resolve-cusip", APPLE, "--ticker", "AAPL")

    assert _securities(migrated_engine)[APPLE][:2] == ("AAPL", "BBG000B9XRY4")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([OBSCURE, "--ticker", "OBS"], "no filing has reported 999999999"),
        (["12345", "--ticker", "OBS"], "is not a CUSIP"),
        ([APPLE, "--ticker", "A B"], "is not a ticker"),
    ],
)
def test_resolve_cusip_refuses_what_it_cannot_resolve(
    runner: CliRunner,
    migrated_engine: AsyncEngine,
    redis: FakeRedis,
    args: list[str],
    message: str,
) -> None:
    _held(migrated_engine, apple=100)

    result = runner.invoke(app, ["resolve-cusip", *args])

    assert result.exit_code == 1
    assert message in result.stderr
    [(status,)] = _fetch(
        migrated_engine,
        select(IngestionRun.status).where(IngestionRun.job_name == "resolve-cusip"),
    )
    assert status == "failed"


# --- cusip-coverage -----------------------------------------------------------------


def test_coverage_by_value_is_the_gate_and_by_row_is_reported_beside_it(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    """Apple is one security in two and 96% of the value."""
    _held(migrated_engine, apple=9_600, obscure=400)
    _run(runner, "resolve-cusips")

    passing = _run(runner, "cusip-coverage")
    failing = _run(runner, "cusip-coverage", "--threshold", "97", code=1)

    headline = "cusip-coverage  96.00% resolved by value — "
    assert passing.splitlines()[0] == headline + "passes the 95% gate"
    assert "by row      1 of 2 securities  50.00%" in passing
    assert failing.splitlines()[0] == headline + "BELOW the 97% gate"
    [metrics, _] = [
        row[0]
        for row in _fetch(
            migrated_engine,
            select(IngestionRun.metrics)
            .where(IngestionRun.job_name == "cusip-coverage")
            .order_by(IngestionRun.started_at),
        )
    ]
    assert metrics["value_pct"] == 96.0
    assert metrics["passes"] is True


def test_coverage_lists_the_unresolved_by_value_with_their_name_and_suggestions(
    runner: CliRunner, migrated_engine: AsyncEngine, figi: FakeOpenFIGI, redis: FakeRedis
) -> None:
    _held(migrated_engine, apple=1_000, apple_old=500, exxon=700, obscure=10)
    _listed(migrated_engine, EXXON, "EXXON MOBIL CORP")
    _run(runner, "resolve-cusips")

    output = _run(runner, "cusip-coverage", code=1)

    unresolved = output.split("unresolved, highest value first:\n")[1].splitlines()
    assert [line.split()[0] for line in unresolved] == [EXXON, APPLE_OLD, OBSCURE]
    assert "EXXON MOBIL CORP" in unresolved[0]
    assert "list" in unresolved[0]
    assert "review: AAPL (1.00)" in unresolved[1]
    assert "OBSCURE CO - converted at IPO" in unresolved[2]


def test_nothing_held_passes(runner: CliRunner, migrated_engine: AsyncEngine) -> None:
    output = _run(runner, "cusip-coverage")

    assert output.splitlines()[0] == "cusip-coverage  n/a resolved by value — passes the 95% gate"
