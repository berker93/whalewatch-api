"""Models for ``/v1/saved-filters``: a user's named screener filters.

A filter is ``GET /v1/flows``'s parameters, the same names and the same
values, so the screener applies one by putting it in the query string. Paging
(``limit``, ``cursor``) is not part of a filter, and neither is ``sector``,
which ``/v1/flows`` refuses until sectors are loaded.

The enums are the screener's own, imported from its router: a filter saved
with a sort the screener does not have would be one that cannot be applied.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
)

from app.api.routers.market import Direction, FlowSort, PeriodType
from app.api.routers.portfolio import SortOrder
from app.core.periods import parse_period, quarter_label
from app.db.models.account import MAX_FILTER_NAME


def _label(value: object) -> str:
    """A period as ``/v1/flows`` takes it, kept as its label: ``2026Q1``."""
    if not isinstance(value, str):
        raise ValueError("a period is a string: 2026Q1 or 2026-03-31")
    return quarter_label(parse_period(value))


#: Dollars, a decimal string both ways: a filter is sent back as it was saved,
#: so one schema serves the request and the answer.
Dollars = Annotated[
    Decimal,
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "decimal", "examples": ["1000000"]}),
]


class ScreenerQuery(BaseModel):
    """``GET /v1/flows``'s parameters. Each one left out is the screener's default."""

    model_config = ConfigDict(extra="forbid")

    period: Annotated[str | None, AfterValidator(lambda v: None if v is None else _label(v))] = (
        Field(
            default=None,
            description=(
                "`period`, as `2026Q1` or `2026-03-31`, kept as `2026Q1`. Left out: the "
                "latest quarter, whenever the filter is applied."
            ),
            examples=["2026Q2"],
        )
    )
    period_type: PeriodType | None = Field(
        default=None, description="`period_type`: `quarter` or `year`.", examples=["quarter"]
    )
    min_investors: int | None = Field(
        default=None, ge=0, description="`min_investors`.", examples=[5]
    )
    min_value: Dollars | None = Field(
        default=None, ge=0, description="`min_value`, in dollars.", examples=["1000000"]
    )
    direction: Direction | None = Field(
        default=None, description="`direction`: `buy` or `sell`.", examples=["buy"]
    )
    sort: FlowSort | None = Field(default=None, description="`sort`.", examples=["buyers"])
    order: SortOrder | None = Field(
        default=None, description="`order`: `asc` or `desc`.", examples=["desc"]
    )


class NewSavedFilter(BaseModel):
    """A filter to save."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description=f"What to call it: 1 to {MAX_FILTER_NAME} characters, trimmed.",
        examples=["Crowded buys"],
    )
    query: ScreenerQuery = Field(description="The screener's parameters.")

    @field_validator("name")
    @classmethod
    def _named(cls, name: str) -> str:
        name = name.strip()
        if not 1 <= len(name) <= MAX_FILTER_NAME:
            raise ValueError(f"a name is 1 to {MAX_FILTER_NAME} characters")
        return name


class SavedFilter(BaseModel):
    """One saved filter."""

    id: str = Field(description="Its id, for `DELETE /v1/saved-filters/{id}`.", examples=["12"])
    name: str = Field(description="What the user called it.", examples=["Crowded buys"])
    query: ScreenerQuery = Field(description="The screener's parameters, defaults left out.")
    created_at: datetime = Field(
        description="When it was saved.", examples=["2026-10-25T09:30:00Z"]
    )


class SavedFilters(BaseModel):
    """Every filter the user saved, oldest first."""

    data: list[SavedFilter] = Field(description="Oldest first. Empty when none are saved.")
