"""Signups, actives and plan mix over time: a row a day, and the series read from them.

The snapshot
------------
``snapshot-metrics`` (daily, just after midnight UTC) writes a ``metric_day``
row for each UTC day since the last one, through yesterday. Signups and
deletions are counted from ``app_user`` and ``account_deletion``; actives from
``user_activity_day`` (:mod:`app.accounts.activity`): ``dau`` that day, ``wau``
the 7 days ending it, ``mau`` the 30. Those three are null for a day before
any activity was recorded, which is every day before migration 0041.

The plan mix (accounts not deleted, by the plan entitlements resolve them to:
an override's, else a live subscription's, else Free) and the account count
are what they are when the job runs, so they are written only for the day
just ended. A day the job missed and caught up later has signups and actives
and no plan mix: a mix made today and filed under last Tuesday would be wrong
without saying so. Rows are written once and never rewritten: the purge
erases accounts, so a recount later would be lower.

The series
----------
:func:`series` reads the rows for a range of days, adds today as it stands so
far, and groups them by day, ISO week or calendar month. A group's signups and
deletions are its days' sums. Its actives are the actives of its last day, by
the group's width: a day's ``dau``, a week's ``wau``, a month's ``mau``. Its
plan mix and account count are its last day's that has one.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.users import accounts
from app.db.models import AccountDeletion, AppUser, MetricDay, UserActivityDay

Every = Literal["day", "week", "month"]

#: How far back the first snapshot reaches, when no row has been written yet.
FIRST_BACKFILL_DAYS: Final = 90

#: The widest series one request reads.
MAX_SERIES_DAYS: Final = 3 * 366


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _window(column: Any, day: date) -> Any:
    return (column >= _start(day)) & (column < _start(day + timedelta(days=1)))


async def _actives(session: AsyncSession, day: date, since: date | None) -> dict[str, int | None]:
    if since is None or since > day:
        return {"dau": None, "wau": None, "mau": None}

    def active(days: int) -> Any:
        return (
            select(func.count(func.distinct(UserActivityDay.user_id)))
            .where(UserActivityDay.day > day - timedelta(days=days), UserActivityDay.day <= day)
            .scalar_subquery()
        )

    dau, wau, mau = (await session.execute(select(active(1), active(7), active(30)))).one()
    return {"dau": dau, "wau": wau, "mau": mau}


async def plan_mix(session: AsyncSession) -> tuple[int, dict[str, int], int]:
    """Accounts not deleted now: how many, how many on each plan, and how
    many of those have it from an override."""
    rows = accounts()
    mix = await session.execute(
        select(rows.c.plan, func.count(), func.count(rows.c.override_plan))
        .where(rows.c.deleted_at.is_(None))
        .group_by(rows.c.plan)
        .order_by(rows.c.plan)
    )
    counts: dict[str, int] = {}
    comped = 0
    for plan, users, overridden in mix:
        counts[plan] = users
        comped += overridden
    return sum(counts.values()), counts, comped


async def _activity_since(session: AsyncSession) -> date | None:
    return await session.scalar(select(func.min(UserActivityDay.day)))


async def measure(
    session: AsyncSession, day: date, *, with_mix: bool, since: date | None
) -> dict[str, Any]:
    """``day``'s numbers, as a ``metric_day`` row's values. The plan mix only
    when ``with_mix``: it is now's, not the day's."""
    signups, deletions = (
        await session.execute(
            select(
                select(func.count())
                .select_from(AppUser)
                .where(_window(AppUser.created_at, day))
                .scalar_subquery(),
                select(func.count())
                .select_from(AccountDeletion)
                .where(_window(AccountDeletion.requested_at, day))
                .scalar_subquery(),
            )
        )
    ).one()
    values: dict[str, Any] = {
        "day": day,
        "signups": signups,
        "deletions": deletions,
        **await _actives(session, day, since),
        "accounts": None,
        "plan_mix": None,
        "comped": None,
    }
    if with_mix:
        values["accounts"], values["plan_mix"], values["comped"] = await plan_mix(session)
    return values


async def snapshot(session: AsyncSession, today: date) -> list[date]:
    """Write a row for every UTC day after the last one written, through the
    day before ``today``: the days written. Does not commit."""
    yesterday = today - timedelta(days=1)
    last = await session.scalar(select(func.max(MetricDay.day)))
    if last is not None:
        first = last + timedelta(days=1)
    else:
        earliest = await session.scalar(select(func.min(AppUser.created_at)))
        first = max(
            today - timedelta(days=FIRST_BACKFILL_DAYS),
            earliest.astimezone(UTC).date() if earliest else yesterday,
        )
    since = await _activity_since(session)
    written = []
    day = first
    while day <= yesterday:
        values = await measure(session, day, with_mix=day == yesterday, since=since)
        result = await session.execute(
            insert(MetricDay).values(**values).on_conflict_do_nothing(index_elements=["day"])
        )
        if result.rowcount:  # type: ignore[attr-defined]
            written.append(day)
        day += timedelta(days=1)
    return written


# --- the series ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Point:
    """One day, week or month of the series."""

    start: date
    end: date
    """The group's last day: today for the group still running."""
    signups: int
    deletions: int
    active: int | None
    """The group's actives: ``dau`` for a day, ``wau`` for a week, ``mau`` for a month."""
    accounts: int | None
    plan_mix: dict[str, int] | None
    comped: int | None
    partial: bool
    """Today is in it, or a day of it has no row: its numbers are not final."""


def _group_start(day: date, every: Every) -> date:
    if every == "week":
        return day - timedelta(days=day.weekday())
    if every == "month":
        return day.replace(day=1)
    return day


def _group_end(start: date, every: Every) -> date:
    if every == "week":
        return start + timedelta(days=6)
    if every == "month":
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return following - timedelta(days=1)
    return start


def group(
    days: Iterable[dict[str, Any]], every: Every, *, start: date, end: date, today: date
) -> list[Point]:
    """``days`` (``metric_day`` values, today's among them) grouped by ``every``,
    each group cut to ``start``..``end``."""
    by_day = {row["day"]: row for row in days}
    active_of = {"day": "dau", "week": "wau", "month": "mau"}[every]
    points = []
    cursor = _group_start(start, every)
    while cursor <= end:
        first, last = max(cursor, start), min(_group_end(cursor, every), end)
        rows = [by_day[d] for d in _days(first, last) if d in by_day]
        snapshots = [row for row in rows if row["plan_mix"] is not None]
        closing = rows[-1] if rows else None
        latest = snapshots[-1] if snapshots else None
        points.append(
            Point(
                start=first,
                end=last,
                signups=sum(row["signups"] for row in rows),
                deletions=sum(row["deletions"] for row in rows),
                active=closing[active_of] if closing and closing["day"] == last else None,
                accounts=latest["accounts"] if latest else None,
                plan_mix=latest["plan_mix"] if latest else None,
                comped=latest["comped"] if latest else None,
                partial=last >= today or len(rows) < (last - first).days + 1,
            )
        )
        cursor = _group_end(cursor, every) + timedelta(days=1)
    return points


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


async def series(
    session: AsyncSession, *, start: date, end: date, every: Every, today: date
) -> list[Point]:
    """``start``..``end`` (UTC days, inclusive) by ``every``: the snapshots, and
    today as it stands so far when the range reaches it."""
    end = min(end, today)
    stored = await session.scalars(
        select(MetricDay)
        .where(MetricDay.day >= start, MetricDay.day <= end)
        .order_by(MetricDay.day)
    )
    days: list[dict[str, Any]] = [
        {
            "day": row.day,
            "signups": row.signups,
            "deletions": row.deletions,
            "dau": row.dau,
            "wau": row.wau,
            "mau": row.mau,
            "accounts": row.accounts,
            "plan_mix": row.plan_mix,
            "comped": row.comped,
        }
        for row in stored
    ]
    if start <= today <= end:
        days.append(
            await measure(session, today, with_mix=True, since=await _activity_since(session))
        )
    return group(days, every, start=start, end=end, today=today)


def utc_today() -> date:
    return datetime.now(UTC).date()
