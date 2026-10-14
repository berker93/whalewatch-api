"""What a position's stock was worth at the period end, and what it has done since.

Every field here is additive (SEC-8): a row that had none of them before has
all of them now, each nullable, and nothing it already had has changed.

The price is not market data. It is the quarter-end price the holders' own 13Fs
imply, a filing's value over its shares, the median across every holder
(``implied_price``, docs/estimated-return.md). So it is there for every
security anyone holds, in production too, and it says how many filings it is
from and how far they agree.

The returns are **since filing**, never since purchase: a 13F says what was
held at a quarter end, not when it was bought or at what price. Both are
against the period's implied price, adjusted for every split since.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.api.schemas.types import Percent, Price, Ratio
from app.db.models.implied_price import PriceConfidence


class PriceEstimate(BaseModel):
    """The stock's price at the row's period end, as its holders' filings imply it."""

    price: Price = Field(
        description=(
            "Dollars per share at the period end, in that period's shares: the median "
            "of value over shares across every holder's 13F. Not adjusted for a later "
            "split."
        ),
        examples=["184.12000000"],
    )
    period: str = Field(description="The quarter it is the price at.", examples=["2026Q1"])
    confidence: PriceConfidence = Field(
        description=(
            "How far the filings agree on it, within 2% of the median: `high` when three "
            "or more do and they are a majority, `medium` when two do and they are a "
            "majority, `low` otherwise. A `low` price is still the best guess there is, "
            "but one filer's mistake could be all of it."
        ),
        examples=["high"],
    )
    observation_count: int = Field(
        description=(
            "Filings it is estimated from: every tracked holder of the stock in the "
            "period, one each, those that valued it at $0 included."
        ),
        examples=[43],
    )
    basis: Literal["implied_from_filings"] = Field(
        description=(
            "Where the price is from. Always `implied_from_filings`: no market data "
            "vendor is involved. `GET /v1/meta/data-sources` says what each source is."
        ),
        examples=["implied_from_filings"],
    )


class Estimates(BaseModel):
    """The price at the row's period, the returns since it, and whether a split is in between.

    Shared by every row that is one security in one period: a portfolio
    position, a stock's owner, an activity row.
    """

    estimated_return_pct: Percent | None = Field(
        default=None,
        description=(
            "Since filing, not since purchase: the stock's latest close against "
            "`price_estimate.price`, split-adjusted, in percent. Null when there is no "
            "close, which is always in production (`GET /v1/meta/data-sources`), and "
            "when there is no price estimate."
        ),
        examples=["22.40"],
    )
    estimated_return_as_of: date | None = Field(
        default=None,
        description="The trading day of the close `estimated_return_pct` is to. Null when it is.",
        examples=["2026-10-13"],
    )
    return_to_last_quarter_pct: Percent | None = Field(
        default=None,
        description=(
            "Since filing, from the filings alone: the stock's latest implied price "
            "after this period against `price_estimate.price`, split-adjusted, in "
            "percent. Null when this period is the stock's latest, and when there is "
            "no price estimate."
        ),
        examples=["8.15"],
    )
    last_quarter_period: str | None = Field(
        default=None,
        description=(
            "The quarter `return_to_last_quarter_pct` is to. Null when it is null for "
            "want of a later price."
        ),
        examples=["2026Q2"],
    )
    price_estimate: PriceEstimate | None = Field(
        default=None,
        description=(
            "Null when no holder's filing priced the stock in the period: every one "
            "reported it at $0, or it is an option line."
        ),
    )
    is_split: bool | None = Field(
        default=None,
        description=(
            "Whether the stock split between the filer's previous period and this one. "
            "The change is then against the previous shares times `split_ratio`, so a "
            "holder who held through a 10-for-1 is a `hold`, not an `add` of 900%. "
            "Null on option lines."
        ),
        examples=[False],
    )
    split_ratio: Ratio | None = Field(
        default=None,
        description=(
            "New shares per old across the split: `10` for a 10-for-1, `0.1` for a "
            "1-for-10, and the product of several. Null exactly when `is_split` is not "
            "true."
        ),
        examples=[None],
    )
