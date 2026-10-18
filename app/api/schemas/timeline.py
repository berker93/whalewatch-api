"""Models for ``/v1/timeline``: Form 4 trades and 13F filings for what a user follows, in one feed.

Three kinds of event share the feed and never a shape, so a client branches on
``kind``:

``insider_trade``
    One Form 4 line, from a followed insider, or an open-market trade at a
    followed stock.
``holder_change``
    One holder's change in a followed stock, from its 13F: each quarter's four
    largest by traded dollars.
``filing_summary``
    One followed investor's 13F, summed up.

Every event is placed at ``at``, when its filing reached EDGAR: a 13F is never
dated by its period end. The rules are the frontend's ``buildTimeline()``
(``src/modules/Timeline/buildTimeline.ts`` in whalewatch-web), which stays the
mock and the oracle this endpoint is tested against.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel

from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.insider import InsiderTransaction
from app.api.schemas.market import LargestChange
from app.api.schemas.types import CURRENT_TICKER, TICKER_THEN, Money, Percent, Quantity
from app.api.schemas.watchlist import WatchInsider, WatchInvestor, WatchStock
from app.db.models.position_change import ChangeAction


class TimelineSide(StrEnum):
    """Which way an event moved shares. Form 4 codes `P`, `A` and `M` acquire, `S`
    and `G` dispose, every other code is `neutral`. A 13F change acquires when it
    opened or added, and disposes when it trimmed or exited. A filing summary
    follows the sign of its net."""

    ACQUIRE = "acquire"
    DISPOSE = "dispose"
    NEUTRAL = "neutral"


class InvestorRef(BaseModel):
    """An investor's name, linked by slug."""

    slug: str = Field(description="`/v1/investors/{slug}`.", examples=["berkshire-hathaway"])
    display_name: str = Field(
        description="Our name for the institution.", examples=["Berkshire Hathaway"]
    )


class _Event(BaseModel):
    id: str = Field(
        description=(
            "Stable however often the feed loads: the accession number, then the line "
            "(`insider_trade`) or the CUSIP (`holder_change`) where one filing gives "
            "several events."
        ),
        examples=["0001197647-26-000042:1"],
    )
    at: datetime = Field(
        description=(
            "When the event's filing reached EDGAR, in UTC, to the second: what the "
            "feed is ordered and windowed by."
        ),
        examples=["2026-08-14T21:05:00Z"],
    )
    side: TimelineSide = Field(description="Which way it moved shares.", examples=["dispose"])
    value_usd: Money | None = Field(
        description=(
            "Signed dollars to the cent: positive acquired, negative disposed. Null when "
            "the filing gives no price, as most grants and gifts do not."
        ),
        examples=["-70600.00"],
    )


class InsiderTradeEvent(_Event):
    """One Form 4 line, from a followed insider or at a followed stock."""

    kind: Literal["insider_trade"] = Field(description="A Form 4 line.", examples=["insider_trade"])
    source: Literal["form4"] = Field(description="From a Form 4.", examples=["form4"])
    ref: Annotated[WatchStock | WatchInsider, Field(discriminator="kind")] = Field(
        description=(
            "What the user followed that put it here: the insider when they are "
            "followed, even if the stock is too. One event, never two."
        )
    )
    trade: InsiderTransaction = Field(description="The line as filed.")


class _HoldingsEvent(_Event):
    source: Literal["13f"] = Field(description="From a 13F.", examples=["13f"])
    period: str = Field(description="The quarter the filing reports.", examples=["2026Q2"])
    as_of: date = Field(
        description="The quarter end the holdings are as of.", examples=["2026-06-30"]
    )
    lag_days: int = Field(
        description="Days from `as_of` to the filing reaching EDGAR, counted in UTC.",
        examples=[45],
    )
    accession_no: str = Field(
        description="The filing the event's figures are from.",
        examples=["0000950123-26-008841"],
    )
    investor: InvestorRef = Field(description="Who filed it.")


class HolderChange(BaseModel):
    """What one holder did with a followed stock in one quarter."""

    cusip: str = Field(description="The stock's CUSIP.", examples=["037833100"])
    ticker: str | None = Field(default=None, description=TICKER_THEN, examples=["AAPL"])
    current_ticker: str | None = Field(default=None, description=CURRENT_TICKER, examples=["AAPL"])
    issuer_name: str | None = Field(
        default=None, description="The issuer, as we name the security.", examples=["APPLE INC"]
    )
    action: ChangeAction = Field(
        description="`new`, `add`, `trim` or `exit`. Never `hold`.", examples=["trim"]
    )
    shares_delta: Quantity = Field(
        description=(
            "Shares now less shares the quarter before. For an exit, minus the whole "
            "previous position."
        ),
        examples=["-10000000.0000"],
    )
    shares_delta_pct: Percent | None = Field(
        default=None,
        description="`shares_delta` as a percentage of the previous shares. Null for `new`.",
        examples=["-1.092193"],
    )
    traded_value_usd: Money = Field(
        description=(
            "Unsigned: the shares that changed hands at the period-end price, as "
            "`/v1/investors/{slug}/activity` has it. `value_usd` is the same, signed."
        ),
        examples=["1893400000.00"],
    )


class HolderChangeEvent(_HoldingsEvent):
    """One holder's change in a followed stock, from its 13F."""

    kind: Literal["holder_change"] = Field(
        description="A holder's change in a followed stock.", examples=["holder_change"]
    )
    ref: WatchStock = Field(description="The followed stock.")
    change: HolderChange = Field(description="What the holder did.")


class ChangeCounts(BaseModel):
    """Positions opened, added to, trimmed and exited in one filing's quarter."""

    new: int = Field(description="Positions opened.", examples=[3])
    add: int = Field(description="Positions added to.", examples=[7])
    trim: int = Field(description="Positions trimmed.", examples=[5])
    exit: int = Field(description="Positions sold out of.", examples=[2])


class FilingSummaryEvent(_HoldingsEvent):
    """A followed investor's 13F, summed up. `value_usd` is the net of every change."""

    kind: Literal["filing_summary"] = Field(
        description="A followed investor's 13F.", examples=["filing_summary"]
    )
    ref: WatchInvestor = Field(description="The followed investor.")
    counts: ChangeCounts = Field(
        description=(
            "Its changes against the quarter before. All zero in the investor's first "
            "quarter, where every position is new only to us."
        )
    )
    largest_change: LargestChange | None = Field(
        default=None,
        description=(
            "Its largest trade by traded dollars, unsigned as on `/v1/market/activity`. "
            "Null in the investor's first quarter."
        ),
    )
    position_count: int = Field(description="Positions at `as_of`.", examples=[41])
    portfolio_value_usd: Money | None = Field(
        default=None,
        description="The portfolio's value at `as_of`, as of the views' last refresh.",
        examples=["263000000000.00"],
    )


class TimelineEvent(
    RootModel[
        Annotated[
            InsiderTradeEvent | HolderChangeEvent | FilingSummaryEvent,
            Field(discriminator="kind"),
        ]
    ]
):
    """One event in the timeline: an `insider_trade`, a `holder_change` or a
    `filing_summary`, by `kind`."""


class TimelineMeta(Meta):
    """``meta``, with the window the events were chosen for."""

    window_from: date = Field(
        description="The first day of the window, in `tz`.", examples=["2026-06-21"]
    )
    window_to: date = Field(
        description="The last day of the window, in `tz`.", examples=["2026-10-18"]
    )
    tz: str = Field(
        description="The IANA zone the window's days were counted in.",
        examples=["America/New_York"],
    )


class TimelineEventEnvelope(Envelope[TimelineEvent]):
    meta: TimelineMeta = Field(description="The window, and when this answer was built.")


class PendingHolders(BaseModel):
    """The last quarter's holders of a followed stock that have not filed this one."""

    count: int = Field(description="How many.", examples=[12])
    first: list[InvestorRef] = Field(
        description="Up to three of them, largest holding first.",
    )


class UpcomingFiling(BaseModel):
    """A 13F still to come for the current quarter, for a followed stock or investor."""

    ref: Annotated[WatchStock | WatchInvestor, Field(discriminator="kind")] = Field(
        description="The followed stock or investor."
    )
    period: str = Field(
        description="The current quarter: the one that ended most recently, in `tz`.",
        examples=["2026Q3"],
    )
    due_at: date = Field(
        description="When its 13Fs are due: 45 days after it ended.", examples=["2026-11-14"]
    )
    pending_holders: PendingHolders | None = Field(
        default=None,
        description="Stock refs only: the holders still to file. Null for an investor.",
    )


class UpcomingMeta(Meta):
    """``meta``: the current quarter and its coverage, and the zone that chose it."""

    tz: str = Field(
        description="The IANA zone whose today chose the current quarter.",
        examples=["America/New_York"],
    )


class UpcomingFilingEnvelope(Envelope[UpcomingFiling]):
    meta: UpcomingMeta = Field(description="The current quarter, and how far it has filled in.")
