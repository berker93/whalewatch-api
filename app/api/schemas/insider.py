"""Response models for the insider feed: Form 4 transactions, one row per line.

A row is one line of one Form 4, as the insider reported it, with the codes
read for a person. Nothing is netted, summed or called "bought" or "sold":
code ``F`` is shares withheld for tax, and a feed that called it a sale would
be the misleading headline the default filter exists to avoid.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, Field

from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.types import CURRENT_TICKER, DELISTED_BY, Money, Price, Quantity

#: Every ``transactionCode`` the SEC defines, as a person would read it: the
#: General Instructions to Form 4, section 8. The set is the SEC's, so a code
#: not here is still served, with no label.
CODE_LABELS: Final = {
    "P": "Open-market or private purchase",
    "S": "Open-market or private sale",
    "V": "Reported voluntarily, before it was due",
    "A": "Grant or award",
    "D": "Disposed of to the issuer",
    "F": "Shares withheld or delivered for tax or exercise price",
    "I": "Discretionary transaction",
    "M": "Option exercise or conversion",
    "C": "Conversion of a derivative",
    "E": "Expiration of a short derivative position",
    "H": "Expiration or cancellation of a long derivative position",
    "O": "Exercise of an out-of-the-money derivative",
    "X": "Exercise of an in-the-money or at-the-money derivative",
    "G": "Gift",
    "L": "Small acquisition",
    "W": "Acquired or disposed of by will or inheritance",
    "Z": "Deposit into or withdrawal from a voting trust",
    "J": "Other acquisition or disposition (see footnotes)",
    "K": "Equity swap or similar",
    "U": "Tendered in a change of control",
}


class Role(StrEnum):
    """What a reporting owner checked on the filing: what they were to the issuer."""

    DIRECTOR = "director"
    OFFICER = "officer"
    TEN_PERCENT_OWNER = "ten_percent_owner"
    OTHER = "other"


class TransactionCode(BaseModel):
    """A transaction code, and what it means."""

    code: str = Field(description="One letter, as filed.", examples=["P"])
    label: str | None = Field(
        default=None,
        description="What the code means, for a person. Null for a code the SEC does not define.",
        examples=["Open-market or private purchase"],
    )


class InsiderTransaction(BaseModel):
    """One line of one Form 4: what one insider did, on one day, in one security."""

    accession_no: str = Field(
        description=(
            "The Form 4 (or 4/A) the line is on. With `line_number`, what identifies "
            "the row: stable however often the filing is loaded, and replaced when a "
            "4/A amends it."
        ),
        examples=["0001197647-26-000042"],
    )
    line_number: int = Field(
        description=(
            "Where the line is in its filing, from 1: the non-derivative table, then "
            "the derivative table."
        ),
        examples=[1],
    )
    transaction_date: date = Field(
        description="The day of the trade, which is not the day it was filed.",
        examples=["2026-06-20"],
    )
    filed_at: datetime = Field(
        description=(
            "When the filing reached EDGAR. Up to two business days after "
            "`transaction_date`, and for a late filer much more."
        ),
        examples=["2026-06-23T21:05:00Z"],
    )
    issuer_cik: str = Field(
        description="The company's CIK, ten digits: the issuer the insider is an insider of.",
        examples=["0001045810"],
    )
    issuer_name: str = Field(
        description="The issuer's name as the filing gives it.", examples=["NVIDIA CORP"]
    )
    issuer_ticker: str | None = Field(
        default=None,
        description=(
            "The issuer's trading symbol as the filing gives it, unchecked: filers "
            "write `NONE`, nothing, or both of two classes."
        ),
        examples=["NVDA"],
    )
    cusip: str | None = Field(
        default=None,
        description=(
            "The security the line was linked to, by the issuer's CIK and the symbol "
            "filed. Null when that could not say which one: an issuer with no listed "
            "security, or two classes and a symbol that names neither or both."
        ),
        examples=["67066G104"],
    )
    ticker: str | None = Field(
        default=None,
        description=(
            "The linked security's ticker on `transaction_date`. Null when the line is "
            "not linked to a security, or no ticker is known for it."
        ),
        examples=["NVDA"],
    )
    current_ticker: str | None = Field(default=None, description=CURRENT_TICKER, examples=["NVDA"])
    delisted_by: date | None = Field(default=None, description=DELISTED_BY, examples=[None])
    insider_cik: str = Field(
        description=(
            "The insider's CIK, ten digits. People have CIKs too, and are told apart by them."
        ),
        examples=["0001197647"],
    )
    insider_name: str = Field(
        description=(
            "The insider's name as their latest filing spells it, surname first as "
            "EDGAR writes people."
        ),
        examples=["HUANG JEN HSUN"],
    )
    roles: list[Role] = Field(
        description=(
            "What the insider was to the issuer on this filing, as they checked it: "
            "any of `director`, `officer`, `ten_percent_owner`, `other`. Several at "
            "once is common. A joint filing's lines are the first owner's named."
        ),
        examples=[["director", "officer"]],
    )
    officer_title: str | None = Field(
        default=None,
        description="Free text as filed: `CEO`, `EVP & CFO`. Null when none was given.",
        examples=["President and CEO"],
    )
    other_role: str | None = Field(
        default=None,
        description="What `other` means, as filed, when it is checked.",
        examples=[None],
    )
    code: str = Field(
        description=(
            "The transaction code, as filed. `P` and `S` are trades on the open "
            "market; most other codes are compensation or mechanics."
        ),
        examples=["S"],
    )
    code_label: str | None = Field(
        default=None,
        description="What `code` means. Null for a code the SEC does not define.",
        examples=["Open-market or private sale"],
    )
    acquired_disposed: str = Field(
        description="`A` if the insider acquired the shares, `D` if they disposed of them.",
        examples=["D"],
    )
    is_derivative: bool = Field(
        description=(
            "A line of the derivative table: options, RSUs, warrants. Its `shares` "
            "and `price_per_share` are the derivative's, not the underlying's."
        ),
        examples=[False],
    )
    security_title: str | None = Field(
        default=None,
        description="What changed hands, as filed.",
        examples=["Common Stock"],
    )
    shares: Quantity | None = Field(
        default=None,
        description="How many, unsigned: `acquired_disposed` gives the direction.",
        examples=["500.0000"],
    )
    price_per_share: Price | None = Field(
        default=None,
        description=(
            "Dollars a share. Null, never zero, when the filing gives none: a grant, a "
            "gift, a price given only as a range in a footnote."
        ),
        examples=["141.20000000"],
    )
    value_usd: Money | None = Field(
        default=None,
        description="`shares` times `price_per_share`, to the cent. Null when either is.",
        examples=["70600.00"],
    )
    shares_owned_after: Quantity | None = Field(
        default=None,
        description="The insider's holding of this security after the trade.",
        examples=["75000000.0000"],
    )
    is_10b5_1: bool = Field(
        description=(
            "Made under a Rule 10b5-1 plan, set up months before: the form's checkbox "
            "since April 2023, a footnote before it. Says nothing about what the "
            "insider thinks now."
        ),
        examples=[True],
    )
    footnotes: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The footnotes the line cites, by the filing's own ids. Where a weighted "
            "average's price range, a plan, or a trust is explained. Null when it "
            "cites none."
        ),
        examples=[{"F1": "The price reported is a weighted average price."}],
    )


class InsiderFeedMeta(Meta):
    """``meta``, with the transaction codes the rows were filtered to."""

    codes: list[TransactionCode] | None = Field(
        default=None,
        description=(
            "The codes the rows are limited to, with what each means. Null when "
            "every code is included (`?code=all`)."
        ),
        examples=[
            [
                {"code": "P", "label": "Open-market or private purchase"},
                {"code": "S", "label": "Open-market or private sale"},
            ]
        ],
    )
    codes_default: bool = Field(
        default=False,
        description=(
            "True when no `?code=` was given and `codes` is the default: open-market "
            "purchases and sales only. Grants, option exercises, tax withholding and "
            "gifts are left out, and a reader who is not told so will think the data "
            "incomplete."
        ),
        examples=[True],
    )
    insider_ciks: list[str] | None = Field(
        default=None,
        description="The insiders the rows are limited to (`?insider_cik=`), or null for anyone.",
        examples=[["0001197647"]],
    )
    filed_from: date | None = Field(
        default=None,
        description="`?filed_from=`: the first day, in UTC, the rows were filed on.",
        examples=["2026-06-01"],
    )
    filed_to: date | None = Field(
        default=None,
        description="`?filed_to=`: the last day, in UTC, the rows were filed on.",
        examples=["2026-06-30"],
    )


class InsiderTransactionEnvelope(Envelope[InsiderTransaction]):
    meta: InsiderFeedMeta = Field(
        description="What the rows describe, including the codes they were filtered to."
    )


# --- clusters -------------------------------------------------------------------


class ClusterInsider(BaseModel):
    """One insider's purchases within a cluster, summed."""

    insider_cik: str = Field(description="The insider's CIK, ten digits.", examples=["0001214156"])
    insider_name: str = Field(
        description="As their latest filing spells it, surname first.",
        examples=["LEVINSON ARTHUR D"],
    )
    roles: list[Role] = Field(
        description=(
            "What they checked on any of the cluster's filings: `director`, `officer`, or both."
        ),
        examples=[["director"]],
    )
    officer_title: str | None = Field(
        default=None,
        description="As filed on their latest filing in the cluster, when one was given.",
        examples=[None],
    )
    first_date: date = Field(
        description="Their first purchase in the cluster.", examples=["2026-05-04"]
    )
    last_date: date = Field(
        description="Their last purchase in the cluster.", examples=["2026-05-06"]
    )
    transaction_count: int = Field(description="Lines, as filed.", examples=[2])
    shares: Quantity | None = Field(
        default=None, description="Shares bought.", examples=["10000.0000"]
    )
    value_usd: Money | None = Field(
        default=None,
        description="Dollars paid. Null when no line gave a price.",
        examples=["1415000.00"],
    )


class InsiderCluster(BaseModel):
    """An episode of cluster buying: several of one issuer's insiders buying within a window."""

    issuer_cik: str = Field(description="The company's CIK, ten digits.", examples=["0000320193"])
    issuer_name: str = Field(
        description="As the latest of the cluster's filings gives it.", examples=["Apple Inc."]
    )
    cusip: str | None = Field(
        default=None,
        description=(
            "The security every purchase was linked to. Null when any was not, or "
            "they were in more than one of the issuer's classes."
        ),
        examples=["037833100"],
    )
    ticker: str | None = Field(
        default=None,
        description="That security's ticker on `window_end`.",
        examples=["AAPL"],
    )
    current_ticker: str | None = Field(default=None, description=CURRENT_TICKER, examples=["AAPL"])
    delisted_by: date | None = Field(default=None, description=DELISTED_BY, examples=[None])
    window_start: date = Field(
        description="The first purchase of the episode.", examples=["2026-05-04"]
    )
    window_end: date = Field(
        description=(
            "The last purchase of the episode. Overlapping windows are one episode, so "
            "this can be further than `window` days from `window_start`."
        ),
        examples=["2026-05-21"],
    )
    insider_count: int = Field(
        description="Distinct insiders who bought. One who bought on three days is one.",
        examples=[4],
    )
    transaction_count: int = Field(description="The purchase lines counted.", examples=[6])
    total_shares: Quantity | None = Field(
        default=None,
        description="Shares bought, the lines' shares summed.",
        examples=["52000.0000"],
    )
    total_value: Money | None = Field(
        default=None,
        description="Dollars paid, the lines' values summed. Null when no line gave a price.",
        examples=["7355000.00"],
    )
    insiders: list[ClusterInsider] = Field(
        description="Who bought, largest purchase first.",
    )


class InsiderClusterMeta(Meta):
    """``meta``, with the window and threshold the clusters were found with, and what counts."""

    window: int = Field(
        description="Days: a purchase, and every other up to this many days before it.",
        examples=[30],
    )
    min_insiders: int = Field(
        description="Distinct insiders a window needed to qualify.", examples=[3]
    )
    rules: list[str] = Field(
        description=(
            "What a purchase has to be to count. Grants, exercises, plan purchases and "
            "10% owners' buying are left out, and a reader who is not told so will "
            "wonder where they went."
        ),
        examples=[["Open-market purchases (code P) of shares, not of derivatives."]],
    )


class InsiderClusterEnvelope(Envelope[InsiderCluster]):
    meta: InsiderClusterMeta = Field(
        description="What the rows describe, including how the clusters were found."
    )
