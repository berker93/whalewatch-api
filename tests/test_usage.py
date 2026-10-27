"""The quota month, without Redis or Postgres: when a month starts, when it
resets, and at what count it warns. The counting is run by a real Redis in
tests/integration/test_quota_metering.py."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.accounts.entitlements import Grant
from app.accounts.sessions import SessionRecord
from app.accounts.usage import (
    COUNTER_PREFIX,
    QuotaPeriod,
    Spent,
    _parse_counter,
    counter_key,
    quota_period,
    warn_threshold,
)
from app.api.metering import quota_headers


def at(day: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


@pytest.mark.parametrize(
    ("anchor", "now", "start", "end"),
    [
        # On the anniversary, a new month; the day before, the last one's end.
        ("2026-03-14", "2026-10-14", "2026-10-14", "2026-11-14"),
        ("2026-03-14", "2026-10-13", "2026-09-14", "2026-10-14"),
        ("2026-03-14", "2026-10-25", "2026-10-14", "2026-11-14"),
        # Across a year.
        ("2026-03-14", "2027-01-02", "2026-12-14", "2027-01-14"),
        # The first month is the subscription's own.
        ("2026-10-25", "2026-10-25", "2026-10-25", "2026-11-25"),
        # The 31st, in months without one: their last day.
        ("2026-01-31", "2026-02-28", "2026-02-28", "2026-03-31"),
        ("2026-01-31", "2026-02-27", "2026-01-31", "2026-02-28"),
        ("2026-01-31", "2026-04-30", "2026-04-30", "2026-05-31"),
        ("2026-01-31", "2028-02-29", "2028-02-29", "2028-03-31"),
        # The 29th in a leap year's February, the 28th otherwise.
        ("2024-01-29", "2027-02-28", "2027-02-28", "2027-03-29"),
    ],
)
def test_a_month_runs_from_one_anniversary_to_the_next(
    anchor: str, now: str, start: str, end: str
) -> None:
    period = quota_period(at(anchor), at(now))

    assert period == QuotaPeriod(start=date.fromisoformat(start), end=date.fromisoformat(end))


def test_the_month_starts_at_midnight_utc() -> None:
    period = quota_period(at("2026-03-14", hour=18), at("2026-10-14", hour=0))

    assert period.start == date(2026, 10, 14)
    assert period.resets_at == datetime(2026, 11, 14, tzinfo=UTC)


def test_days_are_utcs_whatever_zone_the_moments_are_in() -> None:
    """23:30 on the 13th in New York is the 14th in UTC: the anniversary is the 14th."""
    new_york = timezone(timedelta(hours=-4))
    anchor = datetime(2026, 3, 13, 23, 30, tzinfo=new_york)
    now = datetime(2026, 10, 13, 22, 0, tzinfo=new_york)  # 02:00 UTC on the 14th

    assert quota_period(anchor, now).start == date(2026, 10, 14)


def test_seconds_left_is_never_zero() -> None:
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))

    assert period.seconds_left(datetime(2026, 11, 13, 23, 0, tzinfo=UTC)) == 3600
    assert period.seconds_left(period.resets_at) == 1


@pytest.mark.parametrize(
    ("limit", "threshold"),
    [(10_000, 8_000), (5, 4), (3, 3), (1, 1), (0, None), (None, None)],
)
def test_the_warning_is_at_eighty_percent_rounded_up(
    limit: int | None, threshold: int | None
) -> None:
    assert warn_threshold(limit) == threshold


def test_a_counter_key_names_the_user_the_quota_and_the_month() -> None:
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))
    key = counter_key(42, "api_calls_month", period)

    assert key == f"{COUNTER_PREFIX}42:api_calls_month:2026-10-14"
    assert _parse_counter(key) == (42, "api_calls_month", date(2026, 10, 14))
    assert _parse_counter(f"{COUNTER_PREFIX}nonsense") is None


def test_the_headers_say_the_quota_what_is_left_and_the_reset_in_unix_seconds() -> None:
    period = QuotaPeriod(start=date(2026, 10, 14), end=date(2026, 11, 14))

    assert quota_headers(Spent(admitted=True, used=3, limit=5, period=period)) == {
        "x-ratelimit-limit": "5",
        "x-ratelimit-remaining": "2",
        "x-ratelimit-reset": str(int(datetime(2026, 11, 14, tzinfo=UTC).timestamp())),
    }
    # No limit, no quota to state.
    assert quota_headers(Spent(admitted=True, used=3, limit=None, period=period)) == {}


def test_a_session_keeping_a_grant_from_before_anniversaries_reads_it_again() -> None:
    """Sessions made before grants said when their subscription began would
    count months from the account's start, so they read the grant again."""
    kept_at = datetime.now(UTC)
    old = Grant(plan="api_pro", grandfathered=None, overrides={}, ends_at=None)
    record = SessionRecord(
        id="s", user_id=1, remember=False, expires_at=kept_at, grant=old, granted_at=kept_at
    )

    current = replace(record, grant=replace(old, started_at=kept_at))

    assert record.grant_is_stale(kept_at)
    assert not current.grant_is_stale(kept_at)
    assert not replace(record, grant=None).grant_is_stale(kept_at)
    # What a session stores says it, and a stored grant without it reads as one.
    assert SessionRecord.from_json("s", current.to_json()) == current
    assert SessionRecord.from_json("s", record.to_json()).grant == old
