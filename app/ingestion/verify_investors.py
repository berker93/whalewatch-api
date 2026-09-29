"""Check every CIK in the investor list against EDGAR.

The CIKs in ``data/investors.yaml`` are typed by hand, and a wrong one fails
silently: it validates, it seeds, and the filer's page shows somebody else's
portfolio or nothing at all. This turns that into a loud problem at a time of
our choosing, by asking EDGAR what each CIK actually is.

One ``submissions.json`` per CIK (plus its overflow pages, for old filers), and
per CIK the answers to four questions:

=================  ==========================================================
Flag               Meaning
=================  ==========================================================
``not_found``      EDGAR has no such CIK. Almost always a typo. **Hard.**
``no_13f``         The CIK exists but has never filed a 13F-HR — usually a
                   fund's operating company or its founder's personal CIK
                   instead of the filing manager's. **Hard.**
``stale``          The latest 13F-HR period is more than two quarters behind.
                   **Hard** on a CIK the filer currently files under; a
                   warning on a predecessor, which is expected to have
                   stopped (see :func:`_is_current`).
``name_mismatch``  EDGAR's name is not like ours. A warning only: EDGAR's
                   names are legal names and ours are brands, and a
                   predecessor entity is often called something else. It is
                   there for a person to glance at, not to reject on.
``fetch_failed``   EDGAR could not be read for this CIK. **Hard**, because
                   the CIK went unchecked.
=================  ==========================================================
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

import httpx

from app.db.models.filer import OverlapPolicy
from app.ingestion.edgar.client import EdgarClient, EdgarServerError
from app.ingestion.edgar.submissions import (
    FilerNotFoundError,
    FilingHistory,
    SubmissionMalformedError,
    list_filings,
)
from app.ingestion.investors import InvestorEntry

#: The form counted. Originals only: an amendment restates a period the
#: original already covers, so counting ``13F-HR/A`` would inflate the number
#: without adding a quarter. ``13F-NT`` is excluded because a filer whose only
#: 13Fs are notices has no holdings of its own to show.
THIRTEEN_F_HR: Final = "13F-HR"

#: Below this, EDGAR's name and ours are called a mismatch. Tuned for review,
#: not rejection: loose enough that "BERKSHIRE HATHAWAY INC" passes for
#: "Berkshire Hathaway", tight enough that an unrelated company does not.
NAME_SIMILARITY_THRESHOLD: Final = 0.6

#: A 13F is due 45 days after the quarter it reports. A quarter is not "missed"
#: until that window has closed.
FILING_WINDOW: Final = timedelta(days=45)

#: Words that say what kind of legal entity a name is and nothing about which
#: one. Dropped before comparing, or every "LLC" and "L.P." counts against a
#: match with a brand name that has neither.
_LEGAL_SUFFIXES: Final = frozenset(
    {"inc", "llc", "lp", "llp", "ltd", "limited", "co", "corp", "corporation", "plc", "sa", "ag"}
)


class Flag(StrEnum):
    NOT_FOUND = "not_found"
    NO_13F = "no_13f"
    STALE = "stale"
    NAME_MISMATCH = "name_mismatch"
    FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True, slots=True)
class CikCheck:
    """What EDGAR says about one CIK on one entry, and what is wrong with it."""

    slug: str
    cik: str
    """Zero-padded, as the database stores it."""

    listed_name: str
    """Our ``display_name`` for the entry."""

    current: bool
    """Whether the filer is expected to still file under this CIK."""

    edgar_name: str | None
    name_similarity: float | None
    thirteen_f_count: int
    earliest_period: date | None
    latest_period: date | None
    failures: tuple[Flag, ...]
    warnings: tuple[Flag, ...]
    error: str | None = None
    """Why the fetch failed, when it did."""

    @property
    def status(self) -> str:
        return "FAIL" if self.failures else "warn" if self.warnings else "ok"


async def verify_investors(
    edgar: EdgarClient, entries: tuple[InvestorEntry, ...], *, today: date
) -> tuple[CikCheck, ...]:
    """Check every CIK on every entry, in list order.

    Concurrent, because the client's rate limiter is what paces requests and
    it does so across tasks; running them one by one would add each request's
    latency to the run on top of the pacing. A task group rather than
    ``gather`` so that :class:`~app.ingestion.edgar.client.EdgarRateLimited` —
    which no amount of carrying on will fix — cancels the rest instead of
    leaving them to queue up behind the same block.
    """
    cutoff = stale_cutoff(today)
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(
                verify_cik(edgar, entry, cik, current=_is_current(entry, index), cutoff=cutoff)
            )
            for entry in entries
            for index, cik in enumerate(entry.padded_ciks)
        ]
    return tuple(task.result() for task in tasks)


async def verify_cik(
    edgar: EdgarClient, entry: InvestorEntry, cik: str, *, current: bool, cutoff: date
) -> CikCheck:
    """Fetch one CIK's filing history and judge it. Never raises for a problem
    with the CIK; rate limiting and bugs propagate."""
    try:
        history = await list_filings(edgar, cik=cik)
    except FilerNotFoundError:
        return _unchecked(entry, cik, current=current, flag=Flag.NOT_FOUND, error=None)
    except (
        SubmissionMalformedError,
        EdgarServerError,
        # A 404 on an overflow page, a 400, a dropped connection after retries.
        httpx.HTTPError,
    ) as failure:
        return _unchecked(entry, cik, current=current, flag=Flag.FETCH_FAILED, error=str(failure))
    return judge(entry, cik, history, current=current, cutoff=cutoff)


def judge(
    entry: InvestorEntry, cik: str, history: FilingHistory, *, current: bool, cutoff: date
) -> CikCheck:
    """The checks themselves, over a history already fetched."""
    periods = sorted(
        filing.report_date
        for filing in history.filings
        if filing.form_type == THIRTEEN_F_HR and filing.report_date is not None
    )
    count = sum(1 for filing in history.filings if filing.form_type == THIRTEEN_F_HR)
    similarity = (
        name_similarity(history.entity_name, entry.display_name, entry.manager_name)
        if history.entity_name is not None
        else None
    )

    failures: list[Flag] = []
    warnings: list[Flag] = []
    if count == 0:
        failures.append(Flag.NO_13F)
    elif not periods or periods[-1] < cutoff:
        (failures if current else warnings).append(Flag.STALE)
    if similarity is None or similarity < NAME_SIMILARITY_THRESHOLD:
        warnings.append(Flag.NAME_MISMATCH)

    return CikCheck(
        slug=entry.slug,
        cik=cik,
        listed_name=entry.display_name,
        current=current,
        edgar_name=history.entity_name,
        name_similarity=similarity,
        thirteen_f_count=count,
        earliest_period=periods[0] if periods else None,
        latest_period=periods[-1] if periods else None,
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def name_similarity(edgar_name: str, *ours: str) -> float:
    """The best :class:`~difflib.SequenceMatcher` ratio between EDGAR's name and
    any of ours, after both lose case, punctuation and legal suffixes.

    Against the manager's name too, because a family office often files under
    something that reads more like its founder than like its brand.
    """
    theirs = _comparable(edgar_name)
    return max(SequenceMatcher(None, theirs, _comparable(name)).ratio() for name in ours)


def _comparable(name: str) -> str:
    # Dots removed rather than spaced, so "L.P." becomes one word, "lp", that
    # the suffix list can drop.
    words = re.sub(r"[^0-9a-z]+", " ", name.casefold().replace(".", "")).split()
    return " ".join(word for word in words if word not in _LEGAL_SUFFIXES)


def stale_cutoff(today: date) -> date:
    """The oldest latest-period that is not stale, as of ``today``.

    The last quarter whose filing deadline has passed is the one a filer should
    have reported by now; allowing one quarter's slack behind it — a late
    filer, an amendment in progress — means a filer is stale once it has
    missed two due quarters in a row. As of 2026-09-30, Q2 2026 is due, so a
    latest period of 2026-03-31 passes and 2025-12-31 does not.
    """
    return _previous_quarter_end(_quarter_end_on_or_before(today - FILING_WINDOW))


def _is_current(entry: InvestorEntry, index: int) -> bool:
    """Whether the filer should still be filing under its ``index``-th CIK.

    Under ``successor`` the last-listed CIK is the live one and the others are
    predecessors, which stopped filing when they were succeeded. Under ``sum``
    every CIK is a separate book still being filed.
    """
    return entry.overlap is OverlapPolicy.SUM or index == len(entry.ciks) - 1


def _quarter_end_on_or_before(day: date) -> date:
    month = (day.month - 1) // 3 * 3 + 3
    end = _last_day_of(day.year, month)
    return end if end <= day else _previous_quarter_end(end)


def _previous_quarter_end(quarter_end: date) -> date:
    return date(quarter_end.year, quarter_end.month - 2, 1) - timedelta(days=1)


def _last_day_of(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def _unchecked(
    entry: InvestorEntry, cik: str, *, current: bool, flag: Flag, error: str | None
) -> CikCheck:
    return CikCheck(
        slug=entry.slug,
        cik=cik,
        listed_name=entry.display_name,
        current=current,
        edgar_name=None,
        name_similarity=None,
        thirteen_f_count=0,
        earliest_period=None,
        latest_period=None,
        failures=(flag,),
        warnings=(),
        error=error,
    )
