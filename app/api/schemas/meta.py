"""Response models for ``/v1/meta``: which quarters there are, how full each is, and how old.

The rows here are about the data rather than of it. A client reads them to
decide what to ask for and how to present it: which quarters to offer, how
much of the tracked universe each one's figures are made of, and whether a
quarter is still filling in.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PeriodCoverage(BaseModel):
    """One quarter with published data, and how far it has filled in."""

    period: str = Field(description="The quarter.", examples=["2026Q2"])
    period_end: date = Field(
        description="The quarter end `period` names: the day the holdings describe.",
        examples=["2026-06-30"],
    )
    filing_deadline: date = Field(
        description=(
            "When the quarter's 13Fs are due: 45 calendar days after `period_end`. "
            "Approximate: the real deadline moves to the next business day when that "
            "is a weekend or a federal holiday, and this one does not, so it can be up "
            "to three days early."
        ),
        examples=["2026-08-14"],
    )
    filers_reported: int = Field(
        description=(
            "Filers with a published portfolio for this quarter. A filer whose "
            "filing was withheld as suspect has not reported, for this count. The "
            "same number as `meta.coverage.filers_reported` on the quarter's other "
            "endpoints."
        ),
        examples=[69],
    )
    filers_tracked: int = Field(
        description=(
            "Every filer this service tracks today, whether or not it has filed. Not "
            "the filers that existed in the quarter: a fund tracked now that did not "
            "file then counts against an old quarter too."
        ),
        examples=[100],
    )
    is_complete: bool = Field(
        description=(
            "The filing deadline has passed, in New York, and at least 95% of the "
            "tracked filers have reported. A quarter can be complete with filings "
            "still to come, since late filings and amendments arrive for months."
        ),
        examples=[False],
    )
    first_filed_at: datetime | None = Field(
        default=None,
        description=(
            "When the earliest of the quarter's published filings reached EDGAR. Of "
            "those the figures are built from: an original replaced by a restatement "
            "is not one. Null only when the published filings changed since the "
            "aggregates were last refreshed, and none of them is published any more."
        ),
    )
    last_filed_at: datetime | None = Field(
        default=None,
        description=(
            "When the latest of the same filings reached EDGAR: "
            "`meta.latest_filing_at` on the quarter's other endpoints. Null when "
            "`first_filed_at` is."
        ),
    )


class FreshnessKind(StrEnum):
    VIEW = "view"
    JOB = "job"


class Freshness(BaseModel):
    """When one materialised view was last refreshed, or one job last succeeded."""

    kind: FreshnessKind = Field(
        description=(
            "`view`: a materialised view the aggregate endpoints read. `job`: a job "
            "that records its runs, such as `backfill_13f` or `refresh-views`."
        ),
        examples=["view"],
    )
    name: str = Field(examples=["mv_filer_summary"])
    last_success_at: datetime | None = Field(
        default=None,
        description=(
            "For a view, when its last refresh finished: what its rows are as of. For "
            "a job, when its latest run that finished `success` did; a `partial` run, "
            "which finished with some items undone, is not one. Null when a view's "
            "last refresh is unrecorded, or a job has never succeeded."
        ),
    )
    run_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The run that did it: the `run_id` on its log lines. For a view, the "
            "`refresh-views` run. Null when `last_success_at` is, and for a view "
            "refreshed outside a tracked run, which only tests do."
        ),
    )
