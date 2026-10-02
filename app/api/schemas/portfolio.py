"""Response models for one investor's portfolio, activity and history.

Every row is a position or a change *in a period*, never "now": the portfolio
states its period in ``meta``, and activity and history name one on each row.

A security is named as well as tickered on every row. No CUSIP resolves to a
ticker until enrichment runs, and some never will, so ``ticker`` is often null
and ``issuer_name``, the name the filing gave it, is what makes the row
readable. A row is never dropped or blanked for want of a ticker.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.api.schemas.types import Money, Percent, Quantity
from app.db.models.position_change import ChangeAction


class _Security(BaseModel):
    cusip: str = Field(examples=["037833100"])
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


class PortfolioPosition(_Security):
    """One security in the portfolio, and what changed in it since the filer's previous period.

    Common stock, unless ``put_call`` says otherwise. Option lines are rows
    only under ``?include_options=true``, and carry no weight and no change:
    their value is the notional of the underlying, so it is not a share of the
    portfolio, and the changes are only tracked for stock.
    """

    put_call: Literal["Put", "Call"] | None = Field(
        default=None, description="Null for common stock: the only kind of row by default."
    )
    shares: Quantity = Field(
        description="Shares, or for an option line the shares underlying it.",
        examples=["915560382.0000"],
    )
    value_usd: Money = Field(
        description=(
            "Whole dollars at the period end. An option line's is the notional of the underlying."
        ),
        examples=["174346048472.00"],
    )
    weight_pct: Percent | None = Field(
        default=None,
        description=(
            "Its share of the common-stock portfolio's value, in percent. Null on "
            "option lines, and on every row of a period whose positions are all "
            "worth nothing."
        ),
        examples=["44.682510"],
    )
    shares_delta: Quantity | None = Field(
        default=None,
        description=(
            "Shares now, less shares in the filer's previous published period: the "
            "whole position for a `new` one. A fall is not necessarily a sale."
        ),
        examples=["-10000000.0000"],
    )
    shares_delta_pct: Percent | None = Field(
        default=None,
        description=(
            "`shares_delta` as a percentage of the previous shares. Null for a `new` "
            "position, which grew from nothing, and on option lines."
        ),
        examples=["-1.080000"],
    )
    weight_delta: Percent | None = Field(
        default=None,
        description=(
            "`weight_pct` less the previous period's, in percentage points: the whole "
            "weight for a `new` position. Moves with the price, too."
        ),
        examples=["2.311400"],
    )
    action: ChangeAction | None = Field(
        default=None,
        description=(
            "Against the filer's previous published period: `new`, `add`, `trim` or "
            "`hold`, judged on shares alone. Every position in the first period we "
            "have for a filer is `new`. Null on option lines."
        ),
    )
    first_period: date | None = Field(
        default=None,
        description=(
            "The oldest quarter end we have published this filer holding this "
            "security in. Not reset by a quarter it went unheld. Null on option lines."
        ),
        examples=["2021-09-30"],
    )


class Activity(_Security):
    """One thing a filer did to one position, in one period."""

    period: date = Field(
        description="The quarter end the change is as of.", examples=["2026-06-30"]
    )
    prev_period: date = Field(
        description=(
            "The filer's previous published period, which the change is against. "
            "Usually the quarter before. Across a quarter with nothing published, "
            "the last one before the gap."
        ),
        examples=["2026-03-31"],
    )
    action: ChangeAction
    shares: Quantity = Field(description="Shares at `period`. Zero for an exit.")
    shares_delta: Quantity = Field(
        description="Shares at `period` less shares at `prev_period`. Not necessarily a sale."
    )
    shares_delta_pct: Percent | None = Field(
        default=None,
        description="`shares_delta` as a percentage of the previous shares. Null for `new`.",
    )
    value_usd: Money = Field(description="Whole dollars at `period`. Zero for an exit.")
    traded_value_usd: Money = Field(
        description=(
            "The shares bought or sold, at the period-end price; for an exit, at the "
            "price it was last held at. An estimate: a 13F has no other prices. Zero "
            "for a `hold`. What the rows are ordered by within a period."
        ),
    )
    weight_pct: Percent | None = Field(
        default=None, description="Its weight at `period`, in percent. Zero for an exit."
    )
    weight_delta: Percent | None = Field(
        default=None, description="`weight_pct` less the weight at `prev_period`, in points."
    )


class HistoryPoint(BaseModel):
    """One quarter of a filer's portfolio, in summary."""

    period: date = Field(examples=["2026-06-30"])
    portfolio_value_usd: Money | None = Field(
        default=None,
        description=(
            "Whole dollars, common stock only. Null for a quarter between the first "
            "and last we have published with nothing published for it: not filed, "
            "not loaded, or withheld."
        ),
        examples=["263479238410.00"],
    )
    position_count: int | None = Field(default=None, examples=[41])
    top10_weight_pct: Percent | None = Field(
        default=None,
        description="How much of the value is in the ten largest positions, in percent.",
        examples=["88.112045"],
    )
