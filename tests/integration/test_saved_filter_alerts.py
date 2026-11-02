"""Saved filters as alert rules, fired from the pipeline, against a real Postgres.

A ``saved_filter_match`` rule is told when a 13F arrives with positions its
saved filter passes: one notification per filter and filing, naming the
largest. The world is ``test_alert_firing.py``'s: made-up 13Fs ingested as a
backfill does, and the task run on the pairs the commit sent.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AlertKind, AlertRule, SavedFilter
from tests.integration.accounts import make_user, subscribe
from tests.integration.test_alert_firing import (
    ALPHA,
    BRAVO,
    FILED,
    Q1,
    Q2,
    RULES_FROM,
    D,
    _emit,
    _fund,
    _ingest,
    _queued,
    _told,
    catalog,  # noqa: F401  (autouse: the plans, as the job loads them)
    pro,  # noqa: F401
    stocks,  # noqa: F401
)

NEW_BUYS: dict[str, Any] = {"filters": {"action": ["new"]}, "sort": "value", "order": "desc"}


async def _saved(
    session: AsyncSession, user_id: int, params: dict[str, Any], name: str = "New buys"
) -> int:
    filter_id = await session.scalar(
        insert(SavedFilter)
        .values(user_id=user_id, name=name, params=params)
        .returning(SavedFilter.id)
    )
    assert filter_id is not None
    return filter_id


async def _alert(
    session: AsyncSession,
    user_id: int,
    filter_id: int,
    *,
    channels: tuple[str, ...] = ("in_app",),
    created_at: datetime = RULES_FROM,
) -> None:
    await session.execute(
        insert(AlertRule).values(
            user_id=user_id,
            kind=AlertKind.SAVED_FILTER_MATCH.value,
            saved_filter_id=filter_id,
            channels=list(channels),
            created_at=created_at,
        )
    )


async def test_a_filing_with_positions_the_filter_passes_is_told_once(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    filter_id = await _saved(db_session, pro, NEW_BUYS)
    await _alert(db_session, pro, filter_id, channels=("in_app", "email"))
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_500, D(110)), (BRAVO, 2_000, D(50)))

    told = await _emit(db_session, settings, alert_queue)

    # Q1 was filed in May, and is the fund's first period: nothing is new in it.
    [match] = await _told(db_session, AlertKind.SAVED_FILTER_MATCH)
    assert match["title"] == "FUND-A: 1 position passes “New buys”"
    assert match["body"] == "In its 13F for 2026Q2: BRAV (opened, $100,000)."
    assert match["dedupe_key"] == f"saved_filter_match:{filter_id}:fund-a:2026Q2"
    # The filing's EDGAR day: the helpers file it some hours after 16:00 New York.
    assert match["payload"].pop("as_of") in ("2026-08-10", "2026-08-11")
    assert match["payload"] == {
        "ticker": None,
        "filer_slug": "fund-a",
        "period": "2026Q2",
        "delta": None,
        "confidence": None,
        "filer_name": "FUND-A",
        "saved_filter_id": str(filter_id),
        "saved_filter_name": "New buys",
        "query": "action=new&sort=value&order=desc",
        "matches": 1,
        "tickers": ["BRAV"],
        "ignored": 0,
    }
    assert [(kind, title) for _, kind, title, _ in await _queued(db_session)] == [
        ("instant", "FUND-A: 1 position passes “New buys”")
    ]
    assert len(told) == 1

    # Seen again, as a restatement or a rebuild would: nothing new.
    await _ingest(db_session, cik, Q2, (ALPHA, 1_500, D(110)), (BRAVO, 2_100, D(50)))
    assert await _emit(db_session, settings, alert_queue) == []


async def test_a_filing_the_filter_passes_nothing_of_is_not_told(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    await _alert(db_session, pro, await _saved(db_session, pro, NEW_BUYS))
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    # Only an add: not new.
    await _ingest(db_session, cik, Q2, (ALPHA, 1_500, D(110)))

    assert await _emit(db_session, settings, alert_queue) == []


async def test_the_largest_are_named_and_the_rest_counted(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    await _alert(db_session, pro, await _saved(db_session, pro, NEW_BUYS))
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    opened = [(f"9999{n}Z10{n}", 100 * (n + 1), D(10)) for n in range(7)]
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), *opened)

    await _emit(db_session, settings, alert_queue)

    [match] = await _told(db_session, AlertKind.SAVED_FILTER_MATCH)
    assert match["title"] == "FUND-A: 7 positions pass “New buys”"
    # The largest five, by the 13F's names: these CUSIPs have no ticker.
    assert match["body"].startswith("In its 13F for 2026Q2: ISSUER 99996Z106 (opened, $7,000)")
    assert match["body"].endswith(", and 2 more.")
    assert match["payload"]["matches"] == 7
    assert len(match["payload"]["tickers"]) == 5


async def test_the_filter_is_run_as_it_reads_now(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    """A filter saved before the vocabulary changed still fires on what reads,
    and says it passes more than it did."""
    params = {"filters": {"action": ["new"], "min_wieght": ["5"]}}
    await _alert(db_session, pro, await _saved(db_session, pro, params))
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), (BRAVO, 10, D(50)))

    await _emit(db_session, settings, alert_queue)

    [match] = await _told(db_session, AlertKind.SAVED_FILTER_MATCH)
    assert match["body"] == (
        "In its 13F for 2026Q2: BRAV (opened, $500). 1 of the filter's settings no longer "
        "applies, so it passes more than when it was saved: open it to review."
    )
    assert match["payload"]["ignored"] == 1


async def test_only_rules_that_are_on_sent_and_older_than_the_filing_hear_it(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    free = await make_user(db_session, "free@example.com")
    late = await make_user(db_session, "late@example.com")
    await subscribe(db_session, late, "pro")
    # A rule kept from Pro on a Free account, and one made after the filing.
    await _alert(db_session, free, await _saved(db_session, free, NEW_BUYS))
    await _alert(
        db_session,
        late,
        await _saved(db_session, late, NEW_BUYS),
        created_at=FILED[Q2] + timedelta(days=1),
    )
    await _alert(db_session, pro, await _saved(db_session, pro, NEW_BUYS))
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), (BRAVO, 2_000, D(50)))

    await _emit(db_session, settings, alert_queue)

    assert [one["user_id"] for one in await _told(db_session)] == [pro]


async def test_a_filter_with_no_rule_on_runs_nothing(
    db_session: AsyncSession,
    settings: Settings,
    stocks: dict[str, int],  # noqa: F811
    pro: int,  # noqa: F811
    alert_queue: list[Any],
) -> None:
    await _saved(db_session, pro, NEW_BUYS)
    _, cik = await _fund(db_session, "fund-a")
    await _ingest(db_session, cik, Q1, (ALPHA, 1_000, D(100)))
    await _ingest(db_session, cik, Q2, (ALPHA, 1_000, D(100)), (BRAVO, 2_000, D(50)))

    assert await _emit(db_session, settings, alert_queue) == []
