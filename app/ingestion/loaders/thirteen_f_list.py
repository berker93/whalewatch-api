"""Writing one quarter's 13(f) securities list into ``sec_13f_security``.

A quarter is replaced whole: its rows deleted and the parsed list inserted, in
the caller's transaction. Re-importing a quarter therefore leaves exactly what
one import leaves, and a CUSIP the SEC removed from a corrected file does not
linger. Other quarters are never touched, which is what keeps the history.

Before it is written, a list is checked against the quarter before it
(:func:`check_additions`). A CUSIP the list marks ``*A*`` should not have been on
the previous list, and on every real pair of lists checked (2024Q2 to 2026Q3)
none was. The SEC's 2025Q3 text file, which is really its 2025Q1 file, has 559
of 584. The check is the only thing that can tell those apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.periods import quarters_ending
from app.db.models.sec_13f_security import ListStatus, Sec13FSecurity
from app.ingestion.parsers.thirteen_f_list import ThirteenFList

#: The share of a list's additions that may already be on the previous list
#: before the list is refused. Zero in every real list so far; not zero here so
#: that one stray re-add in a genuine list does not block a quarter.
MAX_READDED_SHARE: Final = 0.1


@dataclass(frozen=True, slots=True)
class AdditionsCheck:
    """A list's ``*A*`` rows against the quarter before it."""

    previous: date
    previous_imported: bool
    """Whether there was a previous list to check against. If not, nothing was checked."""

    added: int
    already_listed: int
    """Additions that the previous list already had, not marked deleted."""

    @property
    def passed(self) -> bool:
        if not self.previous_imported or not self.added:
            return True
        return self.already_listed <= self.added * MAX_READDED_SHARE


async def check_additions(
    session: AsyncSession, period: date, parsed: ThirteenFList
) -> AdditionsCheck:
    """Compare ``parsed``'s additions with the list for the quarter before ``period``."""
    previous = quarters_ending(period, 2)[0]
    table = Sec13FSecurity
    imported = await session.scalar(
        select(func.count()).select_from(table).where(table.period_of_report == previous)
    )
    added = [s.cusip for s in parsed.securities if s.status is ListStatus.ADDED]
    already_listed = 0
    if imported and added:
        already_listed = (
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(
                    table.period_of_report == previous,
                    table.status != ListStatus.DELETED.value,
                    table.cusip.in_(added),
                )
            )
        ) or 0
    return AdditionsCheck(
        previous=previous,
        previous_imported=bool(imported),
        added=len(added),
        already_listed=already_listed,
    )


async def load_13f_list(session: AsyncSession, period: date, parsed: ThirteenFList) -> int:
    """Make ``period``'s rows exactly ``parsed``. Returns how many rows it replaced.

    Does not commit: the caller's transaction is the unit, so a failure part
    way through leaves the quarter as it was.
    """
    table = Sec13FSecurity
    this_quarter = table.period_of_report == period
    replaced = await session.scalar(select(func.count()).select_from(table).where(this_quarter))
    await session.execute(delete(table).where(this_quarter))
    await session.execute(
        insert(table),
        [
            {
                "period_of_report": period,
                "cusip": security.cusip,
                "issuer_name": security.issuer_name,
                "issuer_description": security.issuer_description,
                "status": security.status.value,
                "has_listed_options": security.has_listed_options,
            }
            for security in parsed.securities
        ],
    )
    return replaced or 0
