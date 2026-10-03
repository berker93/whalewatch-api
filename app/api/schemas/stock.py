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
from app.api.schemas.error import Problem
from app.api.schemas.types import Money, Percent, Quantity
from app.db.models.position_change import ChangeAction


class StockRef(BaseModel):
    """A security, named three ways, any of which may be what a reader knows it by."""

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


class StockOwner(BaseModel):
    """One filer's position in the stock in one period."""

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


class StockSuggestion(StockRef):
    """A stock the caller may have meant."""


class StockNotFound(Problem):
    """The 404 body when nothing is found by that ticker, alias or CUSIP."""

    suggestions: list[StockSuggestion] = Field(
        description=(
            "Up to five stocks whose ticker starts with what was asked for, or whose "
            "name has a word like it, most widely held first. Possibly empty."
        )
    )


class StockOwnerEnvelope(Envelope[StockOwner]):
    """Every tracked investor holding one stock in one period."""


class OwnershipPointEnvelope(Envelope[OwnershipPoint]):
    """One stock's tracked ownership, a quarter a row."""
