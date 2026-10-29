"""Alerts fired from the pipeline, against a real Postgres: ALR-5.

Each test ingests made-up 13Fs the way a backfill does (``write_13f``: load,
then publish, in one transaction), commits, and then runs what the
``emit_alerts`` task runs on the pairs the commit sent it
(``tests/conftest.py``'s ``alert_queue`` keeps them instead of a broker).

The quarters are 2026's, filed in August, and the alerts are read as of
:data:`NOW`, a week later. Every rule is made at :data:`RULES_FROM`, before any
of it, unless the test is about a rule made after.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import entitlements
from app.accounts.delivery import courier_for
from app.alerts import dispatch
from app.alerts.events import read_events
from app.alerts.firing import MAX_CHANGES_PER_FILING, Told, deliver, fire
from app.core.config import Settings
from app.db.models import (
    AlertKind,
    AlertRule,
    AppUser,
    Filer,
    FilerCik,
    Notification,
    PositionChange,
    Security,
    SecuritySplit,
)
from app.derived.recompute import recompute
from app.derived.scope import Pair, Scope
from app.ingestion.normalisation import normalise_filing
from app.ingestion.parsers.thirteen_f import parse_information_table, parse_primary_doc
from app.jobs.thirteen_f import Parsed, write_13f
from tests.fake_redis import FakeRedis
from tests.integration.accounts import Outbox, make_user, subscribe
from tests.integration.conftest import in_session

D = Decimal

Q1 = date(2026, 3, 31)
Q2 = date(2026, 6, 30)
Q3 = date(2026, 9, 30)

#: When each quarter's 13Fs are filed, give or take the hour each filing adds.
FILED = {
    Q1: datetime(2026, 5, 11, 20, tzinfo=UTC),
    Q2: datetime(2026, 8, 10, 20, tzinfo=UTC),
    Q3: datetime(2026, 8, 12, 20, tzinfo=UTC),
}
NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
RULES_FROM = datetime(2026, 1, 1, tzinfo=UTC)

ALPHA = "11111A101"
BRAVO = "22222B202"

_numbers = count(1)

Row = tuple[str, int, Decimal]
"""``(cusip, shares, price)``: one line of an information table."""


# --- the world ----------------------------------------------------------------


@pytest.fixture(autouse=True)
async def catalog(db_session: AsyncSession) -> AsyncIterator[None]:
    """The plans, as the job loads them: which kinds a rule's owner is sent."""
    entitlements.install(await entitlements.load_catalog(db_session))
    yield
    entitlements.install(None)


@pytest.fixture
async def stocks(db_session: AsyncSession) -> dict[str, int]:
    """ALPHA and BRAVO, with tickers, before any filing names them."""
    ids = {}
    for cusip, ticker in ((ALPHA, "ALPH"), (BRAVO, "BRAV")):
        security_id = await db_session.scalar(
            insert(Security)
            .values(cusip=cusip, ticker=ticker, name=f"{ticker} INC", resolution_source="manual")
            .returning(Security.id)
        )
        assert security_id is not None
        ids[cusip] = security_id
    return ids


@pytest.fixture
async def pro(db_session: AsyncSession) -> int:
    """A Pro account: sent every kind."""
    user_id = await make_user(db_session, "pro@example.com")
    await subscribe(db_session, user_id, "pro")
    return user_id


async def _fund(session: AsyncSession, slug: str) -> tuple[int, str]:
    """A filer with one CIK: its id and the CIK."""
    cik = f"{next(_numbers):010d}"
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    return filer_id, cik


async def _rule(
    session: AsyncSession,
    user_id: int,
    kind: AlertKind,
    *,
    filer_id: int | None = None,
    security_id: int | None = None,
    min_value: Decimal | None = None,
    channels: tuple[str, ...] = ("in_app",),
    created_at: datetime = RULES_FROM,
) -> int:
    rule_id = await session.scalar(
        insert(AlertRule)
        .values(
            user_id=user_id,
            kind=str(kind),
            filer_id=filer_id,
            security_id=security_id,
            min_value=min_value,
            channels=list(channels),
            created_at=created_at,
        )
        .returning(AlertRule.id)
    )
    assert rule_id is not None
    return rule_id


# --- ingesting ----------------------------------------------------------------


def _primary_doc(cik: str, period: date, rows: tuple[Row, ...], *, entry_total: int) -> bytes:
    total = sum(shares * price for _, shares, price in rows)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <submissionType>13F-HR</submissionType>
    <filerInfo>
      <filer><credentials><cik>{cik}</cik></credentials></filer>
      <periodOfReport>{period.month:02d}-{period.day:02d}-{period.year}</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <filingManager><name>FILER {cik}</name></filingManager>
      <reportType>13F HOLDINGS REPORT</reportType>
    </coverPage>
    <summaryPage>
      <otherIncludedManagersCount>0</otherIncludedManagersCount>
      <tableEntryTotal>{entry_total}</tableEntryTotal>
      <tableValueTotal>{total:.0f}</tableValueTotal>
    </summaryPage>
  </formData>
</edgarSubmission>
""".encode()


def _info_table(rows: tuple[Row, ...]) -> bytes:
    lines = "".join(
        f"""
  <infoTable>
    <nameOfIssuer>ISSUER {cusip}</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>{cusip}</cusip>
    <value>{shares * price:.0f}</value>
    <shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>{shares}</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>"""
        for cusip, shares, price in rows
    )
    return (
        '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf'
        f'/informationtable">{lines}\n</informationTable>'
    ).encode()


async def _load(
    session: AsyncSession, cik: str, period: date, *rows: Row, suspect: bool = False
) -> None:
    """A 13F of ``rows`` for ``period``, loaded and published as a backfill does,
    not committed. Suspect when its cover page counts one row too many."""
    number = next(_numbers)
    accession = f"{cik}-26-{number:06d}"
    filed_at = FILED[period] + timedelta(hours=number % 12)
    cover = parse_primary_doc(
        _primary_doc(cik, period, rows, entry_total=len(rows) + (1 if suspect else 0))
    )
    table = parse_information_table(_info_table(rows))
    parsed = Parsed(cover, table, normalise_filing(filed_at=filed_at, cover=cover, table=table))
    assert parsed.normalised.parse_status == ("suspect" if suspect else "ok")
    await write_13f(
        session,
        accession=accession,
        filed_at=filed_at,
        parsed=parsed,
        raw_prefix=f"raw/13f/{cik}/{accession}/",
        source_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}.txt",
    )


async def _ingest(
    session: AsyncSession, cik: str, period: date, *rows: Row, suspect: bool = False
) -> None:
    """:func:`_load`, committed: what sends the pairs to the alerts task."""
    await _load(session, cik, period, *rows, suspect=suspect)
    await session.commit()


async def _emit(
    session: AsyncSession, settings: Settings, queue: list[frozenset[Pair] | None]
) -> list[Told]:
    """What the task does with every message sent so far, which it takes off the queue."""
    told: list[Told] = []
    while queue:
        pairs = queue.pop(0)
        written = await fire(session, await read_events(session, pairs, now=NOW))
        await session.commit()
        courier = courier_for(settings, in_session(session))
        await deliver(settings, written, courier=courier, redis=FakeRedis())  # type: ignore[arg-type]
        told.extend(written)
    return told


async def _told(session: AsyncSession, *kinds: AlertKind) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(
            Notification.user_id,
            Notification.kind,
            Notification.title,
            Notification.body,
            Notification.payload,
            Notification.dedupe_key,
        )
        .where(*([Notification.kind.in_([str(kind) for kind in kinds])] if kinds else []))
        .order_by(Notification.id)
    )
    return [dict(row._mapping) for row in rows]


# --- when it is sent ----------------------------------------------------------


async def test_the_pairs_go_to_the_alerts_task_when_the_load_commits_and_not_before(
    db_session: AsyncSession, stocks: dict[str, int], alert_queue: list[Any]
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    await _load(db_session, cik, Q1, (ALPHA, 100, D(100)))
    assert alert_queue == []

    await db_session.commit()

    assert alert_queue == [frozenset({(filer_id, Q1)})]


async def test_a_load_that_rolls_back_sends_nothing_then_or_later(
    db_session: AsyncSession, stocks: dict[str, int], alert_queue: list[Any]
) -> None:
    _, cik = await _fund(db_session, "fund-a")
    await db_session.commit()
    await _load(db_session, cik, Q1, (ALPHA, 100, D(100)))
    await db_session.rollback()
    assert alert_queue == []

    other_id, other = await _fund(db_session, "fund-b")
    await _ingest(db_session, other, Q1, (ALPHA, 100, D(100)))

    assert alert_queue == [frozenset({(other_id, Q1)})]


async def test_a_broker_that_cannot_take_the_message_fails_nothing(
    db_session: AsyncSession,
    stocks: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(pairs: frozenset[Pair] | None) -> None:
        raise ConnectionRefusedError("broker down")

    monkeypatch.setattr(dispatch, "enqueue", refused)
    filer_id, cik = await _fund(db_session, "fund-a")

    await _ingest(db_session, cik, Q1, (ALPHA, 100, D(100)))

    published = await db_session.scalar(
        select(PositionChange.shares).where(PositionChange.filer_id == filer_id)
    )
    assert published == 100


async def test_a_recompute_of_everything_sends_everything(
    db_session: AsyncSession, stocks: dict[str, int], alert_queue: list[Any]
) -> None:
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 100, D(100)))
    alert_queue.clear()

    await recompute(db_session, Scope())
    await db_session.commit()

    assert alert_queue == [None]


# --- what is told -------------------------------------------------------------


async def test_a_filing_and_its_changes_are_told_to_those_who_follow_the_filer(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
    outbox: Outbox,
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    await _rule(
        db_session, pro, AlertKind.FILER_FILED, filer_id=filer_id, channels=("in_app", "email")
    )
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id)
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_500, D(110)), (BRAVO, 2_000, D(50)))

    told = await _emit(db_session, settings, alert_queue)

    # Q1 was filed in May: three months is not news.
    # Newest first, as the bell reads them: the filing, then the largest change.
    assert [
        (one["kind"], one["title"], one["body"]) for one in reversed(await _told(db_session))
    ] == [
        (
            "filer_filed",
            "FUND-A filed its 13F for 2026Q2",
            "2 positions worth $265,000. Opened 1 and added to 1.",
        ),
        (
            "filer_position_change",
            "FUND-A opened a position in BRAV",
            "2,000 shares, about $100,000 at the 2026Q2 price.",
        ),
        (
            "filer_position_change",
            "FUND-A added to ALPH",
            "+500 shares (+50.0%), about $55,000 at the 2026Q2 price.",
        ),
    ]
    [filed] = await _told(db_session, AlertKind.FILER_FILED)
    assert filed["dedupe_key"] == "filer_filed:fund-a:2026Q2"
    assert filed["payload"] == {
        "ticker": None,
        "filer_slug": "fund-a",
        "period": "2026Q2",
        "delta": None,
        "confidence": None,
        "as_of": "2026-08-10",
        "positions": 2,
        "value_usd": "265000.00",
    }
    # Only the rule that emails, emails.
    assert [message.subject for message in outbox.sent] == ["FUND-A filed its 13F for 2026Q2"]
    assert len(told) == 3


async def test_an_event_told_once_is_not_told_again(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
    outbox: Outbox,
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    await _rule(
        db_session, pro, AlertKind.FILER_FILED, filer_id=filer_id, channels=("in_app", "email")
    )
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)))
    await _emit(db_session, settings, alert_queue)

    # A restatement of the quarter, and a rebuild by hand.
    await _ingest(db_session, cik, Q2, (ALPHA, 1_200, D(100)))
    await recompute(db_session, Scope.of([(filer_id, Q2)]))
    await db_session.commit()
    again = await _emit(db_session, settings, alert_queue)

    assert again == []
    assert len(await _told(db_session)) == 1
    assert len(outbox.sent) == 1


# --- splits: the AC's test ----------------------------------------------------


async def test_a_split_quarter_fires_no_position_change_alerts(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    """ALPHA splits 10-for-1 in Q2, and rose 5%: $100 a share, then $10.50.
    Three holders held straight through, and file Q2 one at a time."""
    holders = []
    for name, shares in (("fund-a", 100), ("fund-b", 200), ("fund-c", 300)):
        filer_id, cik = await _fund(db_session, name)
        await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id)
        await _ingest(db_session, cik, Q1, (ALPHA, shares, D(100)))
        holders.append((filer_id, cik, shares))
    await _rule(db_session, pro, AlertKind.STOCK_OWNERSHIP_CHANGE, security_id=stocks[ALPHA])
    await _emit(db_session, settings, alert_queue)

    first_id, first, shares = holders[0]
    await _ingest(db_session, first, Q2, (ALPHA, shares * 10, D("10.5")))
    # One holder is no split: its 900% add is published as filed, and only
    # the emitter's guard keeps it from being told.
    assert (
        await db_session.scalar(
            select(PositionChange.action).where(
                PositionChange.filer_id == first_id, PositionChange.period_of_report == Q2
            )
        )
        == "add"
    )
    await _emit(db_session, settings, alert_queue)

    for _, cik, shares in holders[1:]:
        await _ingest(db_session, cik, Q2, (ALPHA, shares * 10, D("10.5")))
        await _emit(db_session, settings, alert_queue)

    split = await db_session.scalar(
        select(SecuritySplit.ratio).where(SecuritySplit.security_id == stocks[ALPHA])
    )
    assert split == 10
    actions = await db_session.scalars(
        select(PositionChange.action).where(PositionChange.period_of_report == Q2)
    )
    assert set(actions) == {"hold"}
    assert (
        await _told(db_session, AlertKind.FILER_POSITION_CHANGE, AlertKind.STOCK_OWNERSHIP_CHANGE)
        == []
    )


async def test_a_buy_across_a_split_is_told_once_detected_in_todays_shares(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    """The first to file held through the 10-for-1 and bought half as much
    again: 100 shares became 1,500. Nothing is told until the split is
    detected, and then the 500 shares it bought."""
    holders = []
    for name, before, after in (
        ("fund-a", 100, 1_500),
        ("fund-b", 200, 2_000),
        ("fund-c", 300, 3_000),
    ):
        filer_id, cik = await _fund(db_session, name)
        await _ingest(db_session, cik, Q1, (ALPHA, before, D(100)))
        holders.append((filer_id, cik, after))
    buyer_id = holders[0][0]
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=buyer_id)
    await _emit(db_session, settings, alert_queue)

    told_after = []
    for _, cik, after in holders:
        await _ingest(db_session, cik, Q2, (ALPHA, after, D("10.5")))
        await _emit(db_session, settings, alert_queue)
        told_after.append(len(await _told(db_session)))

    # Two holders agreeing is a split, and fund-b alone agrees with 10-for-1.
    assert told_after == [0, 0, 1]
    [told] = await _told(db_session)
    assert told["title"] == "FUND-A added to ALPH"
    assert told["body"] == (
        "+500 shares (+50.0%), about $5,250 at the 2026Q2 price. "
        "Counted in shares after ALPH's 10-for-1 split. "
        "ALPH's implied price at the quarter end: $10.50 a share (high confidence)."
    )
    payload = told["payload"]
    assert (payload["delta"], payload["shares_delta"], payload["split_ratio"]) == (
        "5250.00",
        "500",
        "10",
    )
    assert (payload["implied_price"], payload["confidence"]) == ("10.5", "high")


async def test_a_sale_across_an_undetected_split_is_not_told_as_an_add(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    """The first to file sold most of the position across the 10-for-1: its
    1,000 shares became 1,560, at a tenth of the price. As filed, an add of
    56%; in today's shares, a trim of 84%, told once the split is."""
    holders = []
    for name, before, after in (
        ("fund-a", 1_000, 1_560),
        ("fund-b", 200, 2_000),
        ("fund-c", 300, 3_000),
    ):
        filer_id, cik = await _fund(db_session, name)
        await _ingest(db_session, cik, Q1, (ALPHA, before, D(100)))
        holders.append((filer_id, cik, after))
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=holders[0][0])
    await _emit(db_session, settings, alert_queue)

    titles = []
    for _, cik, after in holders:
        await _ingest(db_session, cik, Q2, (ALPHA, after, D("10.5")))
        await _emit(db_session, settings, alert_queue)
        titles.append([one["title"] for one in await _told(db_session)])

    assert titles == [[], [], ["FUND-A trimmed ALPH"]]
    [told] = await _told(db_session)
    assert told["body"].startswith("-8,440 shares (-84.4%), about $88,620 at the 2026Q2 price.")


# --- suspect filings, and low-confidence prices --------------------------------


async def test_a_suspect_filing_emits_nothing(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    for kind in (AlertKind.FILER_FILED, AlertKind.FILER_POSITION_CHANGE):
        await _rule(db_session, pro, kind, filer_id=filer_id)
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 5_000, D(100)), suspect=True)
    await _emit(db_session, settings, alert_queue)
    assert await _told(db_session) == []

    # Published anyway, marked suspect: still nothing.
    await recompute(db_session, Scope.of([(filer_id, Q2)]), include_suspect=True)
    await db_session.commit()
    await _emit(db_session, settings, alert_queue)
    assert await _told(db_session) == []


async def test_a_change_across_a_quarter_withheld_as_suspect_emits_nothing(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    """Q2 is withheld, so Q3's changes are against Q1: what they say Q2's
    suspect filing would have said. The Q3 filing itself is news."""
    filer_id, cik = await _fund(db_session, "fund-a")
    for kind in (AlertKind.FILER_FILED, AlertKind.FILER_POSITION_CHANGE):
        await _rule(db_session, pro, kind, filer_id=filer_id)
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 5_000, D(100)), suspect=True)
    await _ingest(db_session, cik, Q3, (ALPHA, 5_000, D(100)), (BRAVO, 100, D(50)))

    await _emit(db_session, settings, alert_queue)

    assert [one["title"] for one in await _told(db_session)] == ["FUND-A filed its 13F for 2026Q3"]


async def test_a_low_confidence_implied_price_is_left_out_of_the_body(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    """One holder of BRAVO is a price nobody agrees with: ``low``."""
    filer_id, cik = await _fund(db_session, "fund-a")
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id)
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), (BRAVO, 2_000, D(50)))

    await _emit(db_session, settings, alert_queue)

    [told] = await _told(db_session)
    assert told["body"] == "2,000 shares, about $100,000 at the 2026Q2 price."
    assert (told["payload"]["confidence"], told["payload"]["implied_price"]) == ("low", None)


# --- who hears it ---------------------------------------------------------------


async def test_a_rule_hears_what_it_asked_for_and_nothing_else(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    # Free, with a rule kept from Pro; a rule made after the filing; one off;
    # a deleted account; and a threshold above BRAVO's $100,000.
    free = await make_user(db_session, "free@example.com")
    await _rule(db_session, free, AlertKind.FILER_FILED, filer_id=filer_id)
    late = await make_user(db_session, "late@example.com")
    await subscribe(db_session, late, "pro")
    await _rule(db_session, late, AlertKind.FILER_FILED, filer_id=filer_id, created_at=NOW)
    off = await make_user(db_session, "off@example.com")
    await subscribe(db_session, off, "pro")
    rule_off = await _rule(db_session, off, AlertKind.FILER_FILED, filer_id=filer_id)
    await db_session.execute(
        update(AlertRule).where(AlertRule.id == rule_off).values(is_active=False)
    )
    gone = await make_user(db_session, "gone@example.com")
    await subscribe(db_session, gone, "pro")
    await _rule(db_session, gone, AlertKind.FILER_FILED, filer_id=filer_id)
    await db_session.execute(update(AppUser).where(AppUser.id == gone).values(deleted_at=NOW))
    await _rule(
        db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id, min_value=D(150_000)
    )
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 3_000, D(100)), (BRAVO, 2_000, D(50)))

    await _emit(db_session, settings, alert_queue)

    assert [(one["user_id"], one["title"]) for one in await _told(db_session)] == [
        (pro, "FUND-A added to ALPH"),
    ]


async def test_a_change_heard_about_the_filer_and_the_stock_is_told_once(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    _, other = await _fund(db_session, "fund-b")
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id)
    await _rule(db_session, pro, AlertKind.STOCK_OWNERSHIP_CHANGE, security_id=stocks[BRAVO])
    for cik_ in (cik, other):
        await _ingest(db_session, cik_, Q1, (ALPHA, 1_000, D(100)))
        await _ingest(db_session, cik_, Q2, (ALPHA, 1_000, D(100)), (BRAVO, 2_000, D(50)))

    await _emit(db_session, settings, alert_queue)

    assert [(one["kind"], one["dedupe_key"]) for one in await _told(db_session)] == [
        ("filer_position_change", f"filer_position_change:fund-a:2026Q2:{BRAVO}"),
        ("stock_ownership_change", f"stock_ownership_change:{BRAVO}:2026Q2:fund-b"),
    ]


async def test_a_filing_is_told_in_its_largest_changes_only(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],
    pro: int,
    alert_queue: list[Any],
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    await _rule(db_session, pro, AlertKind.FILER_POSITION_CHANGE, filer_id=filer_id)
    opened = [(f"{index:05d}X{index:03d}", 100 * index, D(10)) for index in range(1, 13)]
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), *opened)

    await _emit(db_session, settings, alert_queue)

    told = await _told(db_session)
    assert len(told) == MAX_CHANGES_PER_FILING
    assert {one["payload"]["cusip"] for one in told} == {cusip for cusip, _, _ in opened[2:]}


async def test_everything_is_every_followed_pair_filed_for_recently(
    db_session: AsyncSession, stocks: dict[str, int], pro: int
) -> None:
    filer_id, cik = await _fund(db_session, "fund-a")
    _, other = await _fund(db_session, "fund-b")
    await _rule(db_session, pro, AlertKind.FILER_FILED, filer_id=filer_id)
    for cik_ in (cik, other):
        await _ingest(db_session, cik_, Q1, (ALPHA, 1_000, D(100)))
        await _ingest(db_session, cik_, Q2, (ALPHA, 1_000, D(100)))

    events = await read_events(db_session, None, now=NOW)

    assert [(event.kind, event.dedupe_key) for event in events] == [
        (AlertKind.FILER_FILED, "filer_filed:fund-a:2026Q2"),
    ]
