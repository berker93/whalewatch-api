"""Models for ``/v1/saved-filters``: a user's named screens.

A saved filter is ``GET /v1/screener``'s filters, sort and order, under a
name. Not its period, which is chosen each time it is run, so one filter is
read again every quarter. Paging is not part of one either.

The filters are the vocabulary's keys and values as a client sends them
(:data:`app.api.screener.VOCABULARY`), checked against it when saved, a 422
for any it does not take, and read through it again every time one is read,
since the vocabulary may have changed in between. What no longer reads is
left out and named in ``ignored``, never a 500.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.routers.portfolio import SortOrder
from app.api.schemas.envelope import Envelope
from app.api.schemas.screener import AppliedFilter, ScreenerMeta, ScreenerRow
from app.api.screener import ScreenSort
from app.db.models.account import MAX_FILTER_NAME


def _name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= MAX_FILTER_NAME:
        raise ValueError(f"a name is 1 to {MAX_FILTER_NAME} characters")
    return name


_NAME = f"What to call it: 1 to {MAX_FILTER_NAME} characters, trimmed."


class SavedFilterParams(BaseModel):
    """``GET /v1/screener``'s filters, sort and order, without the period."""

    model_config = ConfigDict(extra="forbid")

    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Each filter's key, from `GET /v1/screener/filters`, and its values as "
            "`GET /v1/screener` takes them: one for most, several for `choices` and "
            "`slugs`. Not `period`. Empty is every position held, as the screener's is."
        ),
        examples=[{"action": ["new", "add"], "min_value": ["1000000"]}],
    )
    sort: ScreenSort = Field(
        default=ScreenSort.VALUE,
        description="`GET /v1/screener`'s `sort`: `meta.sorts` of `GET /v1/screener/filters`.",
        examples=["value"],
    )
    order: SortOrder = Field(
        default=SortOrder.DESC, description="`asc` or `desc`.", examples=["desc"]
    )


class NewSavedFilter(BaseModel):
    """A screen to save."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description=_NAME, examples=["Activists opening big stakes"])
    params: SavedFilterParams = Field(description="The screen.")

    _named = field_validator("name")(_name)


class SavedFilterChange(BaseModel):
    """What to change: its name, its screen, or both. What is left out stays."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description=_NAME, examples=["Activist stakes"])
    params: SavedFilterParams | None = Field(
        default=None,
        description="The whole screen, replacing the one saved: what it ignored goes with it.",
    )

    @field_validator("name")
    @classmethod
    def _named(cls, name: str | None) -> str | None:
        return None if name is None else _name(name)

    @model_validator(mode="after")
    def _something(self) -> "SavedFilterChange":
        if self.name is None and self.params is None:
            raise ValueError("give a name, params or both")
        return self


class SavedFilterCopy(BaseModel):
    """What to call the copy."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        description=f'{_NAME} Left out: the original\'s, with " (copy)" after it.',
        examples=["Activists opening big stakes (copy)"],
    )

    @field_validator("name")
    @classmethod
    def _named(cls, name: str | None) -> str | None:
        return None if name is None else _name(name)


class IgnoredParam(BaseModel):
    """Something saved that the screener does not take now, and so is left out."""

    key: str = Field(description="The filter's key, or `sort` or `order`.", examples=["min_wieght"])
    reason: str = Field(description="Why, for a person.", examples=["not a filter"])


class SavedFilter(BaseModel):
    """One saved filter, read through the screener's vocabulary as it is now."""

    id: str = Field(description="Its id, for `/v1/saved-filters/{id}`.", examples=["12"])
    name: str = Field(description="What the user called it.", examples=["Activist stakes"])
    params: SavedFilterParams = Field(
        description=(
            "What it screens for: every filter, value and sort that still reads, as the "
            "screener would echo them. What does not is in `ignored`, and is not applied."
        )
    )
    filters: list[AppliedFilter] = Field(
        description=(
            "`params.filters`, labelled, in the order `GET /v1/screener/filters` lists them."
        )
    )
    query: str = Field(
        description=(
            "`params` as `GET /v1/screener`'s query string, without `period`: add the "
            "quarter to open it in the builder."
        ),
        examples=["action=add%2Cnew&min_value=1000000&sort=value&order=desc"],
    )
    ignored: list[IgnoredParam] = Field(
        description=(
            "What was saved that the screener no longer takes: a filter renamed or "
            "removed, a value out of its range, an investor whose slug has gone. Left out "
            "of `params` and of every run, so the screen is broader than when saved. "
            "Empty when everything reads. Saving its `params` again clears it."
        )
    )
    created_at: datetime = Field(
        description="When it was saved.", examples=["2026-10-25T09:30:00Z"]
    )
    alert_rule_id: str | None = Field(
        description=(
            "The `saved_filter_match` alert rule about it, on or off "
            "(`GET /v1/me/alert-rules`): told when a 13F arrives with positions it passes. "
            "Null when there is none: `POST /v1/me/alert-rules` with `kind: "
            "saved_filter_match` and this filter's `id` as `saved_filter_id` makes one."
        ),
        examples=["31"],
    )
    last_run_at: datetime | None = Field(
        description=(
            "When `GET /v1/saved-filters/{id}/screen` last read it. Null if never: opening "
            "its `query` in `GET /v1/screener` does not count."
        ),
        examples=["2026-11-02T08:15:00Z"],
    )


class SavedFilters(BaseModel):
    """Every filter the user saved, oldest first."""

    data: list[SavedFilter] = Field(description="Oldest first. Empty when none are saved.")


class SavedFilterScreenMeta(ScreenerMeta):
    """A screen's ``meta``, with the saved filter it is."""

    saved_filter: SavedFilter = Field(
        description="The filter the rows are the screen of, `last_run_at` this run."
    )


class SavedFilterScreenEnvelope(Envelope[ScreenerRow]):
    meta: SavedFilterScreenMeta = Field(
        description="What the rows describe: the saved filter, the filters applied and the period."
    )
