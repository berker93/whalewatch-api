"""Response models for the market: what the tracked filers hold, buy and sell, together.

Every figure is the universe's, for the period ``meta`` names: one quarter, or
the four ending at it (``meta.quarters``). Dollars bought and sold are traded
dollars, the shares that changed hands at the period-end price, so a price
move is never counted as buying (:mod:`app.derived.views`).
"""

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.api.schemas.stock import StockRef
from app.api.schemas.types import Money, Percent, Quantity
from app.db.models.position_change import ChangeAction

_SECTOR = Field(
    default=None,
    description=(
        "Null for every stock for now. No source of sectors is loaded yet: a 13F does not "
        "carry one, and neither does the CUSIP-to-ticker resolution."
    ),
    examples=["Information Technology"],
)


class MarketHolding(StockRef):
    """One stock, and how widely and how heavily the tracked filers hold it."""

    sector: str | None = _SECTOR
    holder_count: int = Field(description="Filers holding it at the period end.", examples=[61])
    total_shares: Quantity = Field(description="Their shares, added up.")
    total_value_usd: Money = Field(description="Their positions' value, in whole dollars.")
    avg_weight_pct: Percent | None = Field(
        default=None,
        description=(
            "Its average weight in the portfolios that hold it, in percent. Null only "
            "when none of them has a weight."
        ),
        examples=["4.127035"],
    )
    median_weight_pct: Percent | None = Field(
        default=None,
        description=(
            "The median of the same weights. Well below the average when one holder "
            "means it and the rest hold a sliver."
        ),
        examples=["1.902114"],
    )
    value_rank: int = Field(
        description="1 for the most dollars held in the period. Ties share a rank.",
        examples=[1],
    )


class StockFlow(StockRef):
    """What the tracked filers bought and sold of one stock over the period.

    ``gross_bought_usd`` and ``net_value_usd`` answer different questions, and
    over a year they can be far apart: a position bought in Q1 and sold in Q3
    is all of its cost in ``gross_bought_usd`` and about nothing in
    ``net_value_usd``. The first ranks the biggest accumulations, the second
    the net flow.
    """

    sector: str | None = _SECTOR
    gross_bought_usd: Money = Field(
        description=(
            "Dollars of opening and adding, every quarter's added up: shares bought at "
            "each quarter-end price. Not reduced by anything sold."
        ),
        examples=["5210000000.00"],
    )
    gross_sold_usd: Money = Field(
        description=(
            "Dollars of trimming and exiting, as a positive number. An exit is valued "
            "at the price it was last held at."
        ),
        examples=["1890000000.00"],
    )
    net_value_usd: Money = Field(
        description="`gross_bought_usd` less `gross_sold_usd`: negative when more was sold.",
        examples=["3320000000.00"],
    )
    net_shares: Quantity = Field(
        description=(
            "How far the shares the filers hold moved, every change included. Over a "
            "year, the four quarters' added up."
        )
    )
    buyer_count: int = Field(
        description=(
            "Filers that opened or added to it. Over a year, each one once however "
            "many quarters it bought in."
        ),
        examples=[14],
    )
    seller_count: int = Field(
        description="Filers that trimmed or exited it, counted as `buyer_count` is.",
        examples=[6],
    )
    new_positions: int = Field(description="Filers that opened a position.", examples=[3])
    exits: int = Field(description="Filers that sold out of it.", examples=[1])
    holder_count: int = Field(
        description="Filers holding it at the period end. Zero when every holder exited.",
        examples=[48],
    )
    total_value_usd: Money = Field(
        description="Their positions' value at the period end, in whole dollars."
    )


class LargestChange(StockRef):
    """The largest trade in a filer's period, by traded dollars."""

    action: ChangeAction = Field(description="`new`, `add`, `trim` or `exit`. Never `hold`.")
    shares_delta: Quantity = Field(description="Shares now, less shares the period before.")
    traded_value_usd: Money = Field(
        description="The shares that changed hands, at the period-end price (an exit, its last)."
    )


class FeedFiling(BaseModel):
    """One filing, and what the period it was filed for now holds."""

    accession_no: str = Field(examples=["0001067983-26-000034"])
    form_type: str = Field(examples=["13F-HR"])
    period: date = Field(description="The quarter end the filing reports.", examples=["2026-06-30"])
    filed_at: datetime = Field(description="When it reached EDGAR.")
    slug: str = Field(examples=["berkshire-hathaway"])
    display_name: str = Field(examples=["Berkshire Hathaway"])
    position_count: int = Field(
        description=(
            "Positions in the period as published, of which this filing is one of the "
            "filings: an amendment adding holdings shows the period's count, not its own."
        ),
        examples=[41],
    )
    largest_change: LargestChange | None = Field(
        default=None,
        description=(
            "The period's largest trade. Null in the filer's first period, whose "
            "positions are all new only to us, and when it traded nothing."
        ),
    )
