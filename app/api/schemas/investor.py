"""Response models for ``GET /v1/investors`` and ``GET /v1/investors/{slug}``.

Every number here is from a filer's **latest published period**, which each
row names in ``latest_period``. Investors file on different days and some are
withheld, so the latest period is not the same for every row of a list, and a
row never stands for "now".

A filer we track but have published nothing for is still listed, with every
period-derived field null. That is a real state: not loaded yet, or every
filing withheld as suspect. Leaving it out would make the universe look
smaller than it is.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.api.schemas.envelope import Envelope
from app.api.schemas.types import Money, Percent
from app.db.models.filer import FilerCategory

#: How many quarters ``sparkline`` covers.
SPARKLINE_QUARTERS = 8


class TopHolding(BaseModel):
    """The largest position, by dollars, in the filer's latest period.

    Named as well as tickered. No CUSIP is resolved to a ticker until
    enrichment runs, and a ticker is null for good for some, so a ticker
    alone would leave this unreadable on most rows.
    """

    cusip: str = Field(
        description="The security's nine-character CUSIP, which identifies it everywhere here.",
        examples=["037833100"],
    )
    ticker: str | None = Field(
        default=None,
        description="Null while the CUSIP is unresolved, and for good for some.",
        examples=["AAPL"],
    )
    issuer_name: str | None = Field(
        default=None,
        description="The issuer, as the filing that first reported the CUSIP named it.",
        examples=["APPLE INC"],
    )
    weight_pct: Percent | None = Field(
        default=None,
        description="Its share of the portfolio's value, in percent.",
        examples=["21.453170"],
    )


class InvestorSummary(BaseModel):
    """One filer, as a row of the list."""

    slug: str = Field(
        description="The investor's stable identifier: `/v1/investors/{slug}`.",
        examples=["berkshire-hathaway"],
    )
    display_name: str = Field(
        description="Our name for the institution, or the name on its latest cover page.",
        examples=["Berkshire Hathaway"],
    )
    manager_name: str | None = Field(
        default=None,
        description="The person the institution is known by, where there is one.",
        examples=["Warren Buffett"],
    )
    category: FilerCategory | None = Field(
        default=None,
        description="The investment style we file it under. Null when we have not chosen one.",
        examples=["value"],
    )

    latest_period: date | None = Field(
        default=None,
        description=(
            "The newest quarter end we have published for this filer, which every "
            "figure below describes. Null when nothing is published yet."
        ),
        examples=["2026-06-30"],
    )
    last_filed_at: datetime | None = Field(
        default=None,
        description=(
            "When the most recently filed of the filings behind `latest_period` "
            "reached EDGAR: a later amendment, if one counts."
        ),
        examples=["2026-08-14T16:32:05Z"],
    )
    portfolio_value_usd: Money | None = Field(
        default=None,
        description="Whole dollars, common stock only: no options, no principal amounts.",
        examples=["263479238410"],
    )
    position_count: int | None = Field(
        default=None,
        description="Common-stock positions in `latest_period`.",
        examples=[41],
    )
    top_holding: TopHolding | None = Field(
        default=None,
        description="Null when `latest_period` is, and when the portfolio is empty.",
    )
    sparkline: list[Money | None] = Field(
        description=(
            f"Portfolio value for the {SPARKLINE_QUARTERS} quarters ending at "
            "`latest_period`, oldest first, so the last entry is "
            "`portfolio_value_usd`. Null for a quarter with nothing published. "
            "Empty when `latest_period` is null."
        ),
        examples=[["231000000000", None, "240000000000", "263479238410"]],
    )


class InvestorDetail(InvestorSummary):
    """One filer, with what the list leaves out."""

    first_period: date | None = Field(
        default=None,
        description="The oldest quarter end we have published for this filer.",
        examples=["2021-09-30"],
    )
    top10_weight_pct: Percent | None = Field(
        default=None,
        description="How much of `latest_period`'s value is in its ten largest positions, in %.",
        examples=["88.112045"],
    )
    turnover_pct: Percent | None = Field(
        default=None,
        description=(
            "Shares bought and sold in `latest_period`, valued at its prices, halved, "
            "as a percentage of the previous period's value. Null in the filer's "
            "first period, which has no previous one."
        ),
        examples=["4.381002"],
    )
    ciks: list[str] = Field(
        description="Every CIK the filer files under, oldest entity first.",
        examples=[["0001067983"]],
    )


class InvestorSummaryEnvelope(Envelope[InvestorSummary]):
    """Every tracked investor, a row each."""
