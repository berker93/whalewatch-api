"""Models for ``/v1/screener``: positions that pass a set of named filters, the filters,
and the presets.

A row is one investor's position in one stock in the period ``meta`` names,
with what the investor did to it since its previous period, the stock's
holders and net flow across every investor, and the price estimate with its
confidence (:mod:`app.api.schemas.estimate`). ``meta.filters`` echoes what the
rows passed, so a page read on its own still says what it is a screen of. A
preset's page says which preset in ``meta.preset`` too.
"""

from datetime import date

from pydantic import BaseModel, Field

from app.api.routers.portfolio import SortOrder
from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.estimate import Estimates
from app.api.schemas.stock import StockRef
from app.api.schemas.types import Money, Percent, Quantity
from app.api.screener import FilterSubject, FilterType, ScreenSort
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
    traded_value_usd: Money | None = Field(
        default=None,
        description=(
            "Dollars the change bought or sold, at the period-end price, as the flows count "
            "them: the whole value of a `new` position, the whole previous value of an "
            "`exit`, nothing for a `hold`. Null in the investor's first period."
        ),
        examples=["2000000.00"],
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


class ScreenerMatches(BaseModel):
    """How much passed the filters: every page's rows, not this page's."""

    positions: int = Field(
        description='Rows: what walking every page returns. The "218 matches".',
        examples=[218],
    )
    stocks: int = Field(description="Distinct stocks among them.", examples=[64])
    investors: int = Field(description="Distinct investors among them.", examples=[41])


class ScreenerMeta(Meta):
    """``meta``, with the filters the rows passed besides the period, and how many did."""

    filters: list[AppliedFilter] = Field(
        description=(
            "Every filter but `period`, which is `meta.period`, in the order "
            "`GET /v1/screener/filters` lists them. Empty when only the period was given."
        ),
    )
    matches: ScreenerMatches = Field(
        description=(
            "How many rows pass, on every page, whatever the sort: the count is the "
            "screen's, kept once for all its pages and sorts."
        ),
    )


class ScreenerEnvelope(Envelope[ScreenerRow]):
    meta: ScreenerMeta = Field(
        description="What the rows describe, including the filters they passed."
    )


class ScreenerOption(BaseModel):
    """One value a `choice` or `choices` filter takes."""

    value: str = Field(description="What to send.", examples=["new"])
    label: str = Field(description="What to call it, for a person.", examples=["Opened"])


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
    about: FilterSubject = Field(
        description=(
            "What it is about, to group the controls by: `period`, the `position`, its "
            "`investor`, or its `stock` across every investor."
        ),
        examples=["position"],
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
    insider_trades: bool = Field(
        description=(
            "Whether it reads Form 4 trades: the quarter must then start inside the "
            "plan's insider window (`insider_months`), or the screen is a `402`."
        ),
        examples=[False],
    )
    options: list[ScreenerOption] | None = Field(
        default=None,
        description=(
            "For `choice` and `choices`, the values and their labels, in the order to "
            "list them. Null for every other type."
        ),
        examples=[[{"value": "new", "label": "Opened"}, {"value": "add", "label": "Added to"}]],
    )
    minimum: str | None = Field(
        default=None, description="The smallest value taken, as a decimal string.", examples=["0"]
    )
    maximum: str | None = Field(
        default=None, description="The largest value taken, as a decimal string.", examples=["100"]
    )


class ScreenerSortOption(BaseModel):
    """One column of a row the screen can be sorted on."""

    key: ScreenSort = Field(description="What to send as `sort`.", examples=["holder_count"])
    column: str = Field(
        description=(
            "The row's field it sorts on. `price_estimate` sorts on its `price`; `action` "
            "from `new` to `exit`, the most bought first."
        ),
        examples=["holder_count"],
    )
    label: str = Field(
        description="What to call the column, for a heading.", examples=["Investors holding it"]
    )


class ScreenerFiltersMeta(Meta):
    """``meta``, with what the rows can be sorted on."""

    sorts: list[ScreenerSortOption] = Field(
        description=(
            "Every `sort`: one per field of a row. A row with no value for it is last "
            "either way; ties go to the larger position."
        ),
    )
    default_sort: ScreenSort = Field(description="`sort` left out.", examples=["value"])
    default_order: SortOrder = Field(description="`order` left out.", examples=["desc"])


class ScreenerFilterEnvelope(Envelope[ScreenerFilter]):
    meta: ScreenerFiltersMeta = Field(
        description="What the filters are for, and what the rows can be sorted on."
    )


class ScreenerPreset(BaseModel):
    """A screen chosen by hand: its name, what it shows, and the filters it is."""

    slug: str = Field(
        description=(
            "Its stable identifier: `/v1/screener/presets/{preset}`. Never renamed, so a "
            "shared link keeps working."
        ),
        examples=["quiet-accumulation"],
    )
    name: str = Field(description="What it is called.", examples=["Quiet accumulation"])
    explanation: str = Field(
        description="One sentence saying what the results are, to show above them.",
        examples=[
            "Positions an investor has added shares to three quarters running, a stake "
            "built steadily rather than bought all at once."
        ],
    )
    filters: list[AppliedFilter] = Field(
        description=(
            "Its filters, in the order `GET /v1/screener/filters` lists them: what the "
            "builder opens with."
        ),
    )
    sort: ScreenSort = Field(description="`GET /v1/screener`'s `sort`.", examples=["value"])
    order: SortOrder = Field(description="`GET /v1/screener`'s `order`.", examples=["desc"])
    query: str = Field(
        description=(
            "`filters`, `sort` and `order` as `GET /v1/screener`'s query string, without "
            "`period`: add the quarter, and it is the same screen."
        ),
        examples=["added_quarters=3&sort=value&order=desc"],
    )


class ScreenerPresetEnvelope(Envelope[ScreenerPreset]):
    pass


class ScreenerPresetMeta(ScreenerMeta):
    """A screen's ``meta``, with the preset it is."""

    preset: ScreenerPreset = Field(description="The preset the rows are the screen of.")


class ScreenerPresetResultEnvelope(Envelope[ScreenerRow]):
    meta: ScreenerPresetMeta = Field(
        description="What the rows describe: the preset, its filters and the period."
    )
