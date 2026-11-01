"""Models for ``/v1/screener``: positions that pass a set of named filters, and the filters.

A row is one investor's position in one stock in the period ``meta`` names,
with what the investor did to it since its previous period, the stock's
holders and net flow across every investor, and the price estimate with its
confidence (:mod:`app.api.schemas.estimate`). ``meta.filters`` echoes what the
rows passed, so a page read on its own still says what it is a screen of.
"""

from datetime import date

from pydantic import BaseModel, Field

from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.estimate import Estimates
from app.api.schemas.stock import StockRef
from app.api.schemas.types import Money, Percent, Quantity
from app.api.screener import FilterType
from app.db.models.filer import FilerCategory
from app.db.models.position_change import ChangeAction


class _Position(StockRef):
    slug: str = Field(
        description="The investor's stable identifier: `/v1/investors/{slug}`.",
        examples=["berkshire-hathaway"],
    )
    display_name: str = Field(
        description="Our name for the institution, or the name on its latest cover page.",
        examples=["Berkshire Hathaway"],
    )
    category: FilerCategory | None = Field(
        default=None,
        description="The investment style the investor is filed under. Null when it has none.",
        examples=["value"],
    )
    prev_period: date | None = Field(
        default=None,
        description=(
            "The investor's previous published period, which the change is against. Null "
            "in its first period, where every position is `new` only because it is the "
            "first we have: not a change, and never a row under a change filter."
        ),
        examples=["2026-03-31"],
    )
    action: ChangeAction = Field(
        description="`new`, `add`, `trim`, `hold` or `exit` against `prev_period`, on shares.",
        examples=["new"],
    )
    shares: Quantity = Field(
        description="Shares at the period end. Zero for an exit.", examples=["1200000.0000"]
    )
    shares_delta: Quantity = Field(
        description="Shares now less shares at `prev_period`: the whole position when `new`.",
        examples=["1200000.0000"],
    )
    shares_delta_pct: Percent | None = Field(
        default=None,
        description="`shares_delta` as a percentage of the previous shares. Null for `new`.",
        examples=["25.000000"],
    )
    value_usd: Money = Field(
        description="Whole dollars at the period end. Zero for an exit.",
        examples=["240000000.00"],
    )
    weight_pct: Percent | None = Field(
        default=None,
        description=(
            "Its share of the investor's common-stock portfolio, in percent. Null only when "
            "every position in the portfolio is worth nothing."
        ),
        examples=["6.250000"],
    )
    weight_delta: Percent | None = Field(
        default=None,
        description=(
            "`weight_pct` less the weight at `prev_period`, in points. Moves with the price."
        ),
        examples=["6.250000"],
    )
    holder_count: int = Field(
        description="Investors holding the stock at the period end, this one included.",
        examples=[14],
    )
    net_value_usd: Money | None = Field(
        default=None,
        description=(
            "Dollars every investor bought of the stock less those sold, in the quarter, at "
            "the period-end price. Null when no investor with a previous period held or "
            "traded it."
        ),
        examples=["812000000.00"],
    )


# Estimates first so that they come last, as on every other position row.
class ScreenerRow(Estimates, _Position):
    """One investor's position in one stock, in the period ``meta`` names."""


class AppliedFilter(BaseModel):
    """A filter the rows passed, and the value it was given."""

    key: str = Field(description="The parameter.", examples=["min_weight"])
    label: str = Field(description="What it is called.", examples=["Share of their portfolio"])
    values: list[str] = Field(
        description="As sent, one per value: several for a list, sorted.", examples=[["5"]]
    )


class ScreenerMeta(Meta):
    """``meta``, with the filters the rows passed besides the period."""

    filters: list[AppliedFilter] = Field(
        description=(
            "Every filter but `period`, which is `meta.period`, in the order "
            "`GET /v1/screener/filters` lists them. Empty when only the period was given."
        ),
    )


class ScreenerEnvelope(Envelope[ScreenerRow]):
    meta: ScreenerMeta = Field(
        description="What the rows describe, including the filters they passed."
    )


class ScreenerFilter(BaseModel):
    """One filter of the vocabulary: what to send, and what it means."""

    key: str = Field(description="The query parameter.", examples=["min_weight"])
    label: str = Field(
        description="What to call it, for a person.", examples=["Share of their portfolio"]
    )
    type: FilterType = Field(
        description=(
            "The value: `period` (`2026Q1`), `choice` (one of `options`), `choices` (any "
            "of them, comma-separated), `integer`, `dollars`, `percent` (0 to 100), "
            "`points` (signed percentage points), `boolean` (`true` or `false`), or "
            "`slugs` (investors' slugs, comma-separated)."
        ),
        examples=["percent"],
    )
    description: str = Field(
        description="What it means, for a person.",
        examples=["Only positions at least this percentage of the investor's portfolio."],
    )
    required: bool = Field(description="Whether every screen needs it.", examples=[False])
    available: bool = Field(
        description="False for a filter with no data behind it yet: asking for it is a 422.",
        examples=[True],
    )
    options: list[str] | None = Field(
        default=None,
        description="For `choice` and `choices`, the values. Null for every other type.",
        examples=[["new", "add", "trim", "hold", "exit"]],
    )
    minimum: str | None = Field(
        default=None, description="The smallest value taken, as a decimal string.", examples=["0"]
    )
    maximum: str | None = Field(
        default=None, description="The largest value taken, as a decimal string.", examples=["100"]
    )


class ScreenerFilterEnvelope(Envelope[ScreenerFilter]):
    pass
