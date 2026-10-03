"""Response model for ``GET /v1/search``: investors and stocks, in two groups.

Not an :class:`~app.api.schemas.envelope.Envelope`. That is one list of one
thing, with a period and a page; this is two short lists of different things,
about no period, and never paged. Each group is already in the order to show it.
"""

from pydantic import BaseModel, Field

from app.api.schemas.stock import StockRef
from app.db.models.filer import FilerCategory


class InvestorMatch(BaseModel):
    """An investor, with enough to show it and to link to ``/v1/investors/{slug}``."""

    slug: str = Field(examples=["berkshire-hathaway"])
    display_name: str = Field(
        description="Our name for the institution, or the name on its latest cover page.",
        examples=["Berkshire Hathaway"],
    )
    manager_name: str | None = Field(default=None, examples=["Warren Buffett"])
    category: FilerCategory | None = None


class StockMatch(StockRef):
    """A stock. Link to ``/v1/stocks/{ticker}`` or, while ``ticker`` is null,
    ``/v1/stocks/{cusip}``."""


class SearchResults(BaseModel):
    """What matched, best first, in a group per kind of thing."""

    query: str = Field(
        description=(
            "What was searched for: `q` with its outer whitespace removed. For a "
            "client typing ahead to tell which of its requests this answers."
        ),
        examples=["berkshire"],
    )
    investors: list[InvestorMatch] = Field(
        description=(
            "Investors whose name or manager's name matches, at most `limit`. Names "
            "starting with `q` first, then the rest by how alike they are; ties by "
            "portfolio value in the investor's latest period."
        )
    )
    securities: list[StockMatch] = Field(
        description=(
            "Stocks whose ticker or issuer name matches, at most `limit`. An exact "
            "ticker first, then tickers starting with `q`, then names starting with "
            "it, then the rest by how alike they are; ties by dollars held in the "
            "latest published period."
        )
    )
