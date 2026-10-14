"""Response models for one stock: what it is, who owns it, and how that has moved.

A *stock* here is one security, one CUSIP. A company's two share classes are
two stocks, with two sets of owners, because their shares are not the same
thing and adding them up is the mistake ``app.db.models.security`` is split to
prevent.

Every figure is for a period, which the response names: the detail in
``period``, the owners in ``meta``, the history on each row.
"""

from datetime import date

from pydantic import BaseModel, Field

from app.api.schemas.envelope import Coverage, Envelope
from app.api.schemas.error import Problem, ProblemError
from app.api.schemas.estimate import Estimates
from app.api.schemas.types import (
    CURRENT_TICKER,
    DELISTED_BY,
    TICKER_THEN,
    Money,
    Percent,
    Price,
    Quantity,
    Ratio,
)
from app.db.models.implied_price import PriceConfidence
from app.db.models.position_change import ChangeAction
from app.db.models.security_split import SplitConfidence


class StockRef(BaseModel):
    """A security, named three ways, any of which may be what a reader knows it by."""

    cusip: str = Field(
        description="The security's nine-character CUSIP, which identifies it everywhere here.",
        examples=["037833100"],
    )
    ticker: str | None = Field(default=None, description=TICKER_THEN, examples=["FB"])
    current_ticker: str | None = Field(default=None, description=CURRENT_TICKER, examples=["META"])
    delisted_by: date | None = Field(default=None, description=DELISTED_BY, examples=[None])
    issuer_name: str | None = Field(
        default=None,
        description=(
            "The issuer: OpenFIGI's name once resolved, else the SEC's 13(f) list's, else "
            "the name the first filing to report the CUSIP gave it."
        ),
        examples=["APPLE INC"],
    )


class StockDetail(StockRef):
    """One stock, and how the tracked filers held and traded it in the latest period."""

    sector: str | None = Field(
        default=None,
        description=(
            "Null for every stock for now. No source of sectors is loaded yet: a 13F "
            "does not carry one, and neither does the CUSIP-to-ticker resolution."
        ),
        examples=["Information Technology"],
    )
    period: date | None = Field(
        default=None,
        description=(
            "The latest quarter end published for any filer, which every figure below "
            "describes. Null only when nothing is published at all."
        ),
        examples=["2026-06-30"],
    )
    coverage: Coverage | None = Field(
        default=None,
        description="How many tracked filers `period` includes so far. Null exactly when it is.",
    )
    holder_count: int | None = Field(
        default=None,
        description="Filers holding it at `period`. Zero when none does.",
        examples=[21],
    )
    total_shares: Quantity | None = Field(
        default=None, description="Their shares, added up.", examples=["2390441842.0000"]
    )
    total_value_usd: Money | None = Field(
        default=None,
        description="Their positions' value at `period`, in whole dollars.",
        examples=["110777862687.00"],
    )
    net_shares: Quantity | None = Field(
        default=None,
        description=(
            "Net change in their shares in `period`, against each filer's previous "
            "published period. Filers in their first period are left out: every "
            "position is new to us there, not bought. Zero when nobody changed it."
        ),
        examples=["-31200000.0000"],
    )
    net_value_usd: Money | None = Field(
        default=None,
        description=(
            "Dollars bought less dollars sold in `period`, at its period-end prices. "
            "An estimate: a 13F has no other prices. A price move is not counted as "
            "buying."
        ),
        examples=["-6011544800.00"],
    )
    new_positions: int | None = Field(
        default=None, description="Filers that opened a position in `period`.", examples=[2]
    )
    exits: int | None = Field(
        default=None, description="Filers that held it before `period` and not at it.", examples=[1]
    )


class _Owner(BaseModel):
    slug: str = Field(
        description="The investor's stable identifier: `/v1/investors/{slug}`.",
        examples=["berkshire-hathaway"],
    )
    display_name: str = Field(
        description="Our name for the institution, or the name on its latest cover page.",
        examples=["Berkshire Hathaway"],
    )
    shares: Quantity = Field(
        description="Shares held at the period end.", examples=["300000000.0000"]
    )
    value_usd: Money = Field(
        description="Whole dollars at the period end.", examples=["57039000000.00"]
    )
    weight_pct: Percent | None = Field(
        default=None,
        description=(
            "Its share of that filer's common-stock portfolio, in percent. Null only "
            "when every position in the portfolio is worth nothing."
        ),
        examples=["22.038267"],
    )
    shares_delta: Quantity | None = Field(
        default=None,
        description=(
            "Shares now, less shares in the filer's previous published period: the "
            "whole position for a `new` one. A fall is not necessarily a sale."
        ),
        examples=["-100000000.0000"],
    )
    shares_delta_pct: Percent | None = Field(
        default=None,
        description="`shares_delta` as a percentage of the previous shares. Null for `new`.",
        examples=["-25.000000"],
    )
    weight_delta: Percent | None = Field(
        default=None,
        description="`weight_pct` less the previous period's, in points. Moves with the price.",
        examples=["-6.103412"],
    )
    action: ChangeAction | None = Field(
        default=None,
        description=(
            "Against the filer's previous published period: `new`, `add`, `trim` or "
            "`hold`, judged on shares alone. Every position in a filer's first period "
            "is `new`."
        ),
        examples=["trim"],
    )


# Estimates first so that they come last, as on PortfolioPosition.
class StockOwner(Estimates, _Owner):
    """One filer's position in the stock in one period.

    The price estimate and the returns are the stock's, so the same on every
    row. ``is_split`` and ``split_ratio`` are the filer's: whether its change
    is across a split depends on which period it compares with.
    """


class HolderPoint(BaseModel):
    """One of the five named holders, in one quarter."""

    slug: str = Field(
        description="The investor's stable identifier: `/v1/investors/{slug}`.",
        examples=["berkshire-hathaway"],
    )
    display_name: str = Field(
        description="Our name for the institution, or the name on its latest cover page.",
        examples=["Berkshire Hathaway"],
    )
    shares: Quantity | None = Field(
        default=None,
        description=(
            "Zero when the filer published the quarter without the stock in it. Null "
            "when it published nothing for the quarter, so a chart shows a gap."
        ),
        examples=["300000000.0000"],
    )
    value_usd: Money | None = Field(
        default=None,
        description="Whole dollars at the quarter end. Null when `shares` is.",
        examples=["57039000000.00"],
    )


class OtherHolders(BaseModel):
    """Everyone holding the stock in a quarter who is not one of the five."""

    holder_count: int = Field(description="Filers holding it besides the five.", examples=[16])
    shares: Quantity = Field(description="Their shares, added up.", examples=["612440131.0000"])
    value_usd: Money = Field(
        description="Their positions' value, in whole dollars.", examples=["116441236095.00"]
    )


class OwnershipPoint(BaseModel):
    """The stock's tracked ownership in one quarter, in total and by its largest holders."""

    period: date = Field(description="The quarter end.", examples=["2026-06-30"])
    holder_count: int | None = Field(
        default=None,
        description=(
            "Filers holding it. Zero in a quarter that was published without anyone "
            "holding it. Null in one with nothing published at all."
        ),
        examples=[21],
    )
    total_shares: Quantity | None = Field(
        default=None,
        description="Every holder's shares, added up. Null where `holder_count` is.",
        examples=["2390441842.0000"],
    )
    total_value_usd: Money | None = Field(
        default=None,
        description="Their positions' value, in whole dollars. Null where `holder_count` is.",
        examples=["454441236095.00"],
    )
    top_holders: list[HolderPoint] = Field(
        description=(
            "The same filers, in the same order, on every row: the five largest "
            "holders by value in the latest quarter anyone held the stock. Fewer "
            "when fewer held it then."
        )
    )
    other: OtherHolders | None = Field(
        default=None,
        description=(
            "The total less the five. Null where the total is. The two always add up to the total."
        ),
    )


class ImpliedPricePoint(BaseModel):
    """The stock's price at one quarter end, as its holders' 13Fs imply it."""

    period: date = Field(description="The quarter end.", examples=["2026-03-31"])
    price: Price | None = Field(
        default=None,
        description=(
            "Dollars per share, in that quarter's shares: the median of value over "
            "shares across every holder's 13F. Null when every holder reported it at "
            "$0, and in a quarter nobody held it, which is a row of nulls so a chart "
            "shows the gap."
        ),
        examples=["184.12000000"],
    )
    split_adjusted_price: Price | None = Field(
        default=None,
        description=(
            "`price` in today's shares: divided by every split detected after the "
            "quarter (`/splits`), so the series is one line across them. The same as "
            "`price` when there is none since."
        ),
        examples=["18.41200000"],
    )
    confidence: PriceConfidence | None = Field(
        default=None,
        description=(
            "How far the filings agree on the price, within 2%: `high` when three or "
            "more do and they are a majority, `medium` when two do and they are a "
            "majority, `low` otherwise. Null exactly when `price` is."
        ),
        examples=["high"],
    )
    observation_count: int | None = Field(
        default=None,
        description=(
            "Filings it is estimated from, one per holder, those at $0 included. "
            "Null in a quarter nobody held it."
        ),
        examples=[43],
    )
    unpriced_count: int | None = Field(
        default=None,
        description=(
            "Of `observation_count`, the filings that valued it at $0, left out of the median."
        ),
        examples=[0],
    )
    agreeing_count: int | None = Field(
        default=None,
        description="Of the priced filings, those within 2% of the median.",
        examples=[41],
    )


class StockSplit(BaseModel):
    """A split of the stock, as its holders' share counts show it."""

    period: date = Field(
        description=(
            "The first quarter end after the split: it happened in the quarter that "
            "ends here, and the quarter before is from before it. The filings cannot "
            "date it closer."
        ),
        examples=["2024-06-30"],
    )
    numerator: int = Field(description="New shares per `denominator` old.", examples=[10])
    denominator: int = Field(description="Old shares per `numerator` new.", examples=[1])
    ratio: Ratio = Field(
        description=(
            "`numerator` over `denominator`: above 1 for a split, below 1 for a reverse split."
        ),
        examples=["10.0000000000"],
    )
    confidence: SplitConfidence = Field(
        description=(
            "Every split has at least two holders' share counts moving by the ratio and "
            "the implied price moving the other way. `high`: three or more holders "
            "agree and one of them is exact to the share. `medium`: three or more, or "
            "one exact. `low`: two, neither exact, only ever at 4-for-1 or more "
            "or 1-for-4 or less, which ordinary trading does not imitate."
        ),
        examples=["high"],
    )
    holders: int = Field(
        description="Holders with shares in both quarters, whose share counts could be compared.",
        examples=[22],
    )
    holders_agreeing: int = Field(
        description=(
            "Of `holders`, those whose share count moved by the ratio, give or take trading."
        ),
        examples=[19],
    )
    holders_exact: int = Field(
        description="Of `holders_agreeing`, those exact to the share: holders that did not trade.",
        examples=[6],
    )


class StockSuggestion(StockRef):
    """A stock the caller may have meant."""


class StockNotFoundDetail(BaseModel):
    """The stocks a caller asking for one we do not have may have meant."""

    suggestions: list[StockSuggestion] = Field(
        description=(
            "Up to five stocks whose ticker starts with what was asked for, or whose "
            "name has a word like it, most widely held first. Possibly empty."
        )
    )


class StockNotFoundError(ProblemError):
    """Nothing by that ticker, alias or CUSIP."""

    detail: StockNotFoundDetail = Field(  # type: ignore[assignment]
        description="What the caller may have meant.",
    )


class StockNotFound(Problem):
    """The 404 body when nothing is found by that ticker, alias or CUSIP."""

    error: StockNotFoundError = Field(
        description="What went wrong, with suggestions in `detail.suggestions`.",
    )


class StockOwnerEnvelope(Envelope[StockOwner]):
    """Every tracked investor holding one stock in one period."""


class OwnershipPointEnvelope(Envelope[OwnershipPoint]):
    """One stock's tracked ownership, a quarter a row."""


class ImpliedPricePointEnvelope(Envelope[ImpliedPricePoint]):
    """One stock's implied quarter-end prices, a quarter a row."""


class StockSplitEnvelope(Envelope[StockSplit]):
    """One stock's detected splits, oldest first."""
