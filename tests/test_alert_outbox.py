"""When alert email goes (app/alerts/outbox.py): the rules, on a made-up clock.

Every account here is in New York with its quiet hours at their defaults,
22:00 to 08:00, on a Pro plan, verified, with nothing sent today, unless the
test says otherwise. The clock is 2026-08-12, a Wednesday inside the August
filing window; wall-clock times are New York's and ``now`` the same moment in
UTC (EDT is UTC-4).
"""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import count
from typing import Any

import pytest

from app.alerts.outbox import (
    BATCH_WINDOW,
    DIGEST_AT,
    EMAILS_PER_DAY,
    Account,
    Waiting,
    decide,
    digest_subject,
    filed_when,
    is_quiet,
    last_digest_time,
)
from app.db.models import AlertKind, Channel, Route

DAY = date(2026, 8, 12)
EDT = timedelta(hours=4)

_ids = count(1)


def local(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute))


def item(
    queued: datetime,
    *,
    route: Route = Route.INSTANT,
    rule_kind: AlertKind = AlertKind.FILER_FILED,
    channels: tuple[Channel, ...] = (Channel.IN_APP, Channel.EMAIL),
    active: bool = True,
    rule_id: int = 1,
) -> Waiting:
    """A notification queued at ``queued``, a New York wall clock."""
    season = rule_kind is AlertKind.SEASON_DIGEST
    return Waiting(
        id=next(_ids),
        rule_id=rule_id,
        route=Route.DIGEST if season else route,
        kind=AlertKind.FILER_FILED if season else rule_kind,
        title="FUND-A filed its 13F for 2026Q2",
        body="",
        payload={"filer_slug": "fund-a", "period": "2026Q2"},
        queued_at=(queued + EDT).replace(tzinfo=UTC),
        local_queued_at=queued,
        local_filed_at=queued if season else None,
        rule_kind=rule_kind,
        rule_active=active,
        rule_channels=frozenset(channels),
    )


def season(queued: datetime, **kwargs: Any) -> Waiting:
    return item(queued, rule_kind=AlertKind.SEASON_DIGEST, rule_id=99, **kwargs)


def account(at: datetime, *waiting: Waiting, **settings: Any) -> Account:
    """The account as it is at ``at``, a New York wall clock."""
    return replace(
        Account(
            now=(at + EDT).replace(tzinfo=UTC),
            local_now=at,
            quiet_start=time(22),
            quiet_end=time(8),
            verified=True,
            sent=frozenset(AlertKind),
            emailed_today=0,
            last_digest=None,
            digest_emails=False,
            waiting=waiting,
        ),
        **settings,
    )


# --- the clock ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clock", "start", "end", "quiet"),
    [
        (time(23), time(22), time(8), True),
        (time(3), time(22), time(8), True),
        (time(8), time(22), time(8), False),
        (time(7, 59), time(22), time(8), True),
        (time(21, 59), time(22), time(8), False),
        (time(13), time(12), time(14), True),
        (time(14), time(12), time(14), False),
        (time(3), time(0), time(0), False),
    ],
)
def test_quiet_hours_run_past_midnight_when_they_start_late(
    clock: time, start: time, end: time, quiet: bool
) -> None:
    assert is_quiet(clock, start, end) is quiet


def test_the_digest_due_is_the_last_one_on_the_clock() -> None:
    assert last_digest_time(local(19)) == local(19)
    assert last_digest_time(local(23)) == local(19)
    assert last_digest_time(local(8)) == local(19, day=DAY - timedelta(days=1))


def test_when_the_filings_were_filed_is_said_from_today() -> None:
    yesterday, monday = DAY - timedelta(days=1), DAY - timedelta(days=2)
    assert filed_when({DAY}, DAY) == "today"
    assert filed_when({yesterday}, DAY) == "yesterday"
    assert filed_when({yesterday, DAY}, DAY) == "since yesterday"
    assert filed_when({monday, DAY}, DAY) == "since Monday"
    assert filed_when({DAY - timedelta(days=9)}, DAY) == "recently"


def test_the_digest_subject_carries_the_number() -> None:
    assert digest_subject(7, "today", 0) == "7 of your investors filed today"
    assert digest_subject(1, "today", 1) == "1 of your investors filed today, and 1 more alert"
    assert digest_subject(0, "today", 4) == "4 more alerts"


# --- batches -----------------------------------------------------------------------


def test_a_batch_waits_for_its_window() -> None:
    first, second = item(local(10)), item(local(10, 9))

    assert decide(account(local(10, 14), first, second)).batch == []
    assert decide(account(local(10, 15), first, second)).batch == [first, second]
    assert timedelta(minutes=15) == BATCH_WINDOW


def test_nothing_goes_in_quiet_hours_and_what_waited_goes_at_their_end() -> None:
    late, later = item(local(22, 30)), item(local(23, 59))
    tomorrow = DAY + timedelta(days=1)

    assert decide(account(local(23, 50), late, later)).batch == []
    assert decide(account(local(7, 59, day=tomorrow), late, later)).batch == []
    assert decide(account(local(8, day=tomorrow), late, later)).batch == [late, later]


def test_quiet_hours_set_equal_are_none() -> None:
    late = item(local(23))
    at = account(local(23, 15), late, quiet_start=time(0), quiet_end=time(0))
    assert decide(at).batch == [late]


# --- the cap -----------------------------------------------------------------------


def test_past_the_days_emails_a_batch_moves_to_the_digest() -> None:
    late = item(local(20))

    decision = decide(account(local(20, 15), late, emailed_today=EMAILS_PER_DAY))

    assert decision.batch == []
    assert decision.overflowed == [late]


def test_the_days_last_email_is_kept_for_a_digest_that_will_email() -> None:
    morning = item(local(9))
    at = local(9, 15)
    two = EMAILS_PER_DAY - 1

    # The season's digest emails, and it is a window day: the third is its.
    assert decide(account(at, morning, emailed_today=two, digest_emails=True)).overflowed == [
        morning
    ]
    # No digest that emails: the third is a batch's.
    assert decide(account(at, morning, emailed_today=two)).batch == [morning]
    # Overflow already waiting will make a digest whatever the season.
    waiting = replace(item(local(8, 30)), route=Route.DIGEST)
    assert decide(account(at, morning, waiting, emailed_today=two)).overflowed == [morning]
    # After the digest's hour, nothing is held back for it.
    evening = item(local(20))
    assert decide(account(local(20, 15), evening, emailed_today=two, digest_emails=True)).batch == [
        evening
    ]


# --- the digest ---------------------------------------------------------------------


def test_the_digest_goes_at_its_hour_with_what_was_queued_before_it() -> None:
    filed = season(local(14))
    overflow = replace(item(local(15)), route=Route.DIGEST)
    after = season(local(19, 30))

    assert decide(account(local(18, 59), filed, overflow)).digest is None
    digest = decide(account(local(19), filed, overflow, after)).digest

    assert digest is not None
    assert digest.filed == [filed]
    assert digest.overflow == [overflow]
    assert digest.emailed == [filed, overflow]
    assert digest.rings is True
    assert digest.at == local(19)
    assert time(19) == DIGEST_AT


def test_one_digest_an_evening() -> None:
    filed = season(local(19, 30))
    assert decide(account(local(20), filed, last_digest=local(19))).digest is None
    tomorrow = local(19, day=DAY + timedelta(days=1))
    assert decide(account(tomorrow, filed, last_digest=local(19))).digest is not None


def test_a_digest_with_no_room_goes_as_the_next_mornings_first_email() -> None:
    filed = season(local(14))
    full = {"emailed_today": EMAILS_PER_DAY}

    assert decide(account(local(19), filed, **full)).digest is None
    morning = decide(account(local(8, day=DAY + timedelta(days=1)), filed))
    assert morning.digest is not None
    assert morning.digest.filed == [filed]


def test_a_bell_only_digest_needs_no_room() -> None:
    filed = season(local(14), channels=(Channel.IN_APP,))

    digest = decide(account(local(19), filed, emailed_today=EMAILS_PER_DAY)).digest

    assert digest is not None
    assert digest.emailed == []
    assert digest.rings is True


def test_a_digest_held_by_quiet_hours_goes_at_their_end() -> None:
    """Quiet hours from 18:00 cover the digest's hour: it goes at 08:00."""
    filed = season(local(14))
    quiet = {"quiet_start": time(18), "quiet_end": time(8)}

    assert decide(account(local(19), filed, **quiet)).digest is None
    digest = decide(account(local(8, day=DAY + timedelta(days=1)), filed, **quiet)).digest
    assert digest is not None
    assert digest.at == local(19)


# --- what may no longer go ---------------------------------------------------------------


def test_an_item_whose_rule_no_longer_wants_it_is_dropped() -> None:
    off = item(local(10), active=False)
    bell_only = item(local(10), channels=(Channel.IN_APP,))
    # A filing alert on a plan that has dropped to Free, which sends the digest only.
    unsent = item(local(10))
    free = frozenset({AlertKind.SEASON_DIGEST})

    decision = decide(account(local(10, 15), off, bell_only, unsent, sent=free))

    assert decision.dropped == [off, bell_only, unsent]
    assert decision.batch == []


def test_an_unverified_address_is_emailed_nothing_and_the_bell_still_rings() -> None:
    filed = season(local(14))
    batch = item(local(10))

    decision = decide(account(local(19), filed, batch, verified=False))

    assert decision.dropped == [batch]
    assert decision.digest is not None
    assert decision.digest.emailed == []
    assert decision.digest.rings is True
