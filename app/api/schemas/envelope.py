"""The one shape every collection endpoint answers in: ``{data, meta, page}``.

Set once, here, so that the client has one set of types to handle rather than
one per endpoint. ``data`` is always a list, even for an endpoint that happens
to return one row today; ``meta`` says what the rows are *of*; ``page`` says how
to get the next ones, and is null for a collection that is not paginated.

``meta`` is the presentation rule in ``docs/product-spec.md`` made structural:
nothing is "current", so every 13F-derived payload states its period at the top
level, and a client cannot render the rows without having been handed it.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


class Coverage(BaseModel):
    """How much of the tracked universe a period's numbers are made of.

    13Fs are due 45 days after the quarter end and arrive across those 45 days,
    so a period's aggregates are partial for weeks. The ratio is what tells a
    client whether "most owned" means most owned, or most owned by the funds
    that happen to have filed so far.
    """

    filers_reported: int = Field(
        description=(
            "Filers with a published portfolio for this period. A filer whose "
            "filing was withheld as suspect has not reported, for this count."
        ),
        examples=[69],
    )
    filers_tracked: int = Field(
        description="Every filer this service tracks, whether or not it has filed yet.",
        examples=[100],
    )


class Meta(BaseModel):
    """What the rows in ``data`` describe, and when this answer was produced."""

    period: str | None = Field(
        default=None,
        description=(
            "The quarter the data belongs to. Null only for a collection that "
            "is not about a period, such as a list of filers."
        ),
        examples=["2026Q2"],
    )
    period_end: date | None = Field(
        default=None,
        description="The quarter end `period` names: the day the holdings describe.",
        examples=["2026-06-30"],
    )
    latest_filing_at: datetime | None = Field(
        default=None,
        description=(
            "When the most recently filed of this period's published filings "
            "reached EDGAR. Null when nothing for the period has been published."
        ),
    )
    coverage: Coverage | None = Field(default=None, description="Null exactly when `period` is.")
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Why the rows may be wrong although every rule for reading the filings "
            "was followed: an amendment left out because its cover page does not say "
            "whether it restates or adds to the period, additions counting with no "
            "original loaded, two originals filed for one period, or an amendment "
            "number missing from those loaded. One sentence each, for a person to "
            "read. Empty when nothing is known to be wrong, and on endpoints that do "
            "not check."
        ),
        examples=[
            [
                "0000909661-21-000003 is a 13F-HR/A with no amendmentType, so it is "
                "left out: if it restates or adds to the period, the period is wrong"
            ]
        ],
    )
    generated_at: datetime = Field(description="When this response was built.")


class Page(BaseModel):
    """Where this page ends, as an opaque token to send back for the next one."""

    limit: int = Field(description="The page size this response was built with.", examples=[50])
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Pass as `?cursor=` for the next page, with the same filters and "
            "sort. Null on the last page. Opaque: its contents are not a contract."
        ),
    )


class Envelope[T](BaseModel):
    """``data``, what it describes, and how to page through it."""

    data: list[T]
    meta: Meta
    page: Page | None = None
