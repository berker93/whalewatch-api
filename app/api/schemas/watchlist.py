"""Models for ``/v1/watchlist``: what a user follows, and the refs that name each thing.

A :class:`WatchRef` names a stock by CUSIP, never by ticker (tickers change),
an investor by slug, and an insider by CIK, never by name (names are spelt
however the latest filing spells them). The timeline takes the same refs.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.api.schemas.insider import Role
from app.api.schemas.search import InvestorMatch, StockMatch
from app.api.schemas.types import CURRENT_TICKER


class WatchStock(BaseModel):
    """A stock, by CUSIP."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["stock"] = Field(description="A stock.", examples=["stock"])
    cusip: str = Field(
        description="Nine characters, case-insensitive.",
        examples=["037833100"],
    )


class WatchInvestor(BaseModel):
    """An investor, by slug."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["investor"] = Field(description="An investor.", examples=["investor"])
    slug: str = Field(
        description="As `/v1/investors/{slug}` takes it.", examples=["berkshire-hathaway"]
    )


class WatchInsider(BaseModel):
    """An insider, by CIK."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["insider"] = Field(description="An insider.", examples=["insider"])
    insider_cik: str = Field(
        description="Their CIK, with or without the leading zeros.",
        examples=["0001197647"],
    )


class WatchRef(
    RootModel[Annotated[WatchStock | WatchInvestor | WatchInsider, Field(discriminator="kind")]]
):
    """Something that can be followed: a stock, an investor or an insider, by `kind`."""


class WatchlistItemRequest(BaseModel):
    """One thing to follow or unfollow."""

    model_config = ConfigDict(extra="forbid")

    ref: WatchRef = Field(description="What to follow or unfollow.")


class InsiderMatch(BaseModel):
    """A followed insider, as their latest Form 4 gives them."""

    insider_cik: str = Field(description="Their CIK, ten digits.", examples=["0001197647"])
    insider_name: str = Field(
        description="As their latest filing spells it, surname first.",
        examples=["HUANG JEN HSUN"],
    )
    roles: list[Role] = Field(
        description="What they checked on their latest filing.",
        examples=[["director", "officer"]],
    )
    officer_title: str | None = Field(
        default=None,
        description="As filed on their latest filing. Null when none was given.",
        examples=["President and CEO"],
    )
    issuer_name: str = Field(
        description="The issuer of their latest filing, as it gives its name.",
        examples=["NVIDIA CORP"],
    )
    current_ticker: str | None = Field(
        default=None,
        description=(
            f"The ticker of the security their latest filing's lines are linked to. "
            f"{CURRENT_TICKER} Null when no line could be linked to one."
        ),
        examples=["NVDA"],
    )


class Watchlist(BaseModel):
    """What the user follows, in three lists, each in the order it was added, oldest first.

    Every list is always present, empty when nothing of its kind is followed.
    """

    securities: list[StockMatch] = Field(description="Followed stocks.")
    investors: list[InvestorMatch] = Field(description="Followed investors.")
    insiders: list[InsiderMatch] = Field(description="Followed insiders.")
