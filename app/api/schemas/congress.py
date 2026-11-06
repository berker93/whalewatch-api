"""Response models for the Congress feed: members' trades, from their PTRs.

A row is one line of one Periodic Transaction Report, as the member filed it.
The STOCK Act asks for an amount *band* — "$1,001 - $15,000" — never a figure,
so every amount here is a band, and every total is the sum of the bands' ends:
a range, never a midpoint. "Pelosi bought $8M of NVDA" is not something the
filings say; "between $1.25M and $5.5M" is.

The dates are what they are, so the lags are facts: how many days after the
trade the member was told of it (``notification_lag_days``), and how many after
it the filing reached the chamber (``disclosure_lag_days``), against the
statute's deadline.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field

from app.api.schemas.envelope import Envelope, Meta
from app.api.schemas.types import CURRENT_TICKER, DELISTED_BY, Money, Percent, Score

#: Days after the trade the STOCK Act gives a member to file, at the latest.
DEADLINE_AFTER_TRADE: Final = 45
#: Days after being notified of it, when that is sooner.
DEADLINE_AFTER_NOTIFICATION: Final = 30


class Chamber(StrEnum):
    """Which chamber a filing was made to."""

    HOUSE = "house"
    SENATE = "senate"


class Owner(StrEnum):
    """Whose account the trade was in, as the form's owner column says."""

    SELF = "self"
    SPOUSE = "spouse"
    JOINT = "joint"
    DEPENDENT_CHILD = "dependent_child"


class TradeType(StrEnum):
    """What kind of trade a line is, as the form's type column says."""

    PURCHASE = "purchase"
    SALE = "sale"
    PARTIAL_SALE = "partial_sale"
    EXCHANGE = "exchange"


class MemberMatch(StrEnum):
    """Whether the name on the filing is known to be a member."""

    MATCHED = "matched"
    REVIEW = "review"
    """Possibly one of several members: waiting for a person."""
    UNMATCHED = "unmatched"
    """No member fits, or a person said none does."""
    UNLINKED = "unlinked"
    """Not looked at yet: the filing was listed since the last match."""


class UnreadReason(StrEnum):
    """Why none of a filing's lines are in the rows."""

    UNREADABLE = "unreadable"
    """No line could be read: a scan, handwriting, a Senate paper filing."""
    IN_REVIEW = "in_review"
    """Lines were read, none surely enough: a person is checking them."""
    PENDING = "pending"
    """Not read yet, or its PDF was not at the Clerk's when tried."""


class CongressMemberRef(BaseModel):
    """The member a filing is by, as of the trade."""

    bioguide_id: str = Field(
        description="The Bioguide's id: what `/v1/congress/members/{bioguide}` takes.",
        examples=["P000197"],
    )
    name: str = Field(description="As the Bioguide gives it.", examples=["Nancy Pelosi"])
    party: str | None = Field(
        default=None,
        description=(
            "Their party in the term they were serving on the trade day, or their "
            "latest if none was: `Democrat`, `Republican`, `Independent`."
        ),
        examples=["Democrat"],
    )
    state: str = Field(description="That term's state, a USPS code.", examples=["CA"])
    district: int | None = Field(
        default=None,
        description="That term's House district, `0` at large. Null for a senator.",
        examples=[11],
    )


class CongressTrade(BaseModel):
    """One line of one PTR: what a member, their spouse or child traded, on one day."""

    chamber: Chamber = Field(description="Where it was filed.", examples=["house"])
    doc_id: str = Field(
        description=(
            "The chamber's id for the filing: the House's `DocID`, a Senate report's "
            "UUID. With `line`, what identifies the row."
        ),
        examples=["20033725"],
    )
    line: int = Field(description="Where the line is in the filing, from 1.", examples=[3])
    filing_date: date = Field(
        description=(
            "When the chamber received the filing: the day the trade was disclosed. "
            "`disclosure_lag_days` counts to it."
        ),
        examples=["2026-08-14"],
    )
    document_url: str | None = Field(
        default=None,
        description="The filing as the chamber serves it: the original, to check a row against.",
        examples=["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033725.pdf"],
    )
    member: CongressMemberRef | None = Field(
        default=None,
        description=(
            "Who filed it. Null when the name on the filing is not known to be a "
            "member yet, which `member_match` says why: the row is shown, flagged, "
            "under `filer_name`."
        ),
    )
    member_match: MemberMatch = Field(
        description="Whether `filer_name` is known to be `member`.", examples=["matched"]
    )
    filer_name: str = Field(
        description="The name as the chamber's index has it, honorific and all.",
        examples=["Hon. Nancy Pelosi"],
    )
    owner: Owner = Field(
        description=(
            "Whose account: the member's own (`self`), their spouse's, a joint one, "
            "or a dependent child's. Members report all four."
        ),
        examples=["spouse"],
    )
    asset_name: str = Field(
        description="What was traded, as filed.", examples=["NVIDIA Corporation - Common Stock"]
    )
    asset_type: str | None = Field(
        default=None,
        description=(
            "The House's two-letter asset code as filed: `ST` stock, `OP` option, "
            "`GS` government security… A Senate row's words are mapped to it."
        ),
        examples=["ST"],
    )
    asset_class: str | None = Field(
        default=None,
        description=(
            "What kind of asset, in our terms: `equity`, `stock_option`, "
            "`municipal_bond`, `treasury`, `agency_bond`, `corporate_bond`, "
            "`mutual_fund`, `crypto`, `real_estate`, `private`, `other`, `unknown`. "
            "Null until the line is resolved."
        ),
        examples=["equity"],
    )
    filed_ticker: str | None = Field(
        default=None,
        description="The ticker the filer wrote, unchecked. Null on a scan.",
        examples=["NVDA"],
    )
    cusip: str | None = Field(
        default=None,
        description=(
            "The stock the line was resolved to, or an option's underlying: the same "
            "security a 13F holding of it is. Null for a bond, a fund, or a stock no "
            "lookup found."
        ),
        examples=["67066G104"],
    )
    ticker: str | None = Field(
        default=None,
        description="That security's ticker on `transaction_date`.",
        examples=["NVDA"],
    )
    current_ticker: str | None = Field(default=None, description=CURRENT_TICKER, examples=["NVDA"])
    delisted_by: date | None = Field(default=None, description=DELISTED_BY, examples=[None])
    asset_confidence: Score | None = Field(
        default=None,
        description=(
            "0 to 1: how sure the match to `cusip` is. 1 when the filer's ticker and "
            "name agree; under 0.9 is a guess a person has not checked."
        ),
        examples=["1.000"],
    )
    type: TradeType = Field(
        description="`purchase`, `sale`, `partial_sale` (some of the holding), or `exchange`.",
        examples=["purchase"],
    )
    transaction_date: date = Field(description="The day of the trade.", examples=["2026-06-20"])
    notification_date: date | None = Field(
        default=None,
        description=(
            "The day the member was told of the trade: later than it for an account "
            "someone else manages. Null when the filing gives none."
        ),
        examples=["2026-06-25"],
    )
    amount_low: int | None = Field(
        default=None,
        description=(
            "The band's lower end, in dollars, as the form gives it. Null when the "
            "filer wrote a figure instead (`amount_exact`)."
        ),
        examples=[1000001],
    )
    amount_high: int | None = Field(
        default=None,
        description=(
            'The band\'s upper end. Null with `amount_low` for an open band: "over $50,000,000".'
        ),
        examples=[5000000],
    )
    amount_exact: Money | None = Field(
        default=None,
        description="A figure the filer wrote instead of a band. Never with one.",
        examples=[None],
    )
    notification_lag_days: int | None = Field(
        default=None,
        description=(
            "`notification_date` - `transaction_date`, in days. Null when there is no "
            "notification date, or one before the trade, which is a filer's error."
        ),
        examples=[5],
    )
    disclosure_lag_days: int | None = Field(
        default=None,
        description=(
            "`filing_date` - `transaction_date`, in days: how long after the trade the "
            "public could know of it. Null on an amended or a Senate amendment's line, "
            "whose filing is not the first to disclose it, and when the trade is dated "
            "after the filing, which is a filer's error."
        ),
        examples=[71],
    )
    disclosure_deadline: date | None = Field(
        default=None,
        description=(
            f"When the STOCK Act had it due: {DEADLINE_AFTER_NOTIFICATION} days after "
            f"`notification_date`, and never more than {DEADLINE_AFTER_TRADE} after "
            "`transaction_date`. Null exactly when `disclosure_lag_days` is."
        ),
        examples=["2026-07-25"],
    )
    days_late: int | None = Field(
        default=None,
        description=(
            "Days `filing_date` is past `disclosure_deadline`; `0` when it was filed "
            "in time. Null exactly when `disclosure_lag_days` is. Counted from the day "
            "the chamber received it, which can be days before it was published."
        ),
        examples=[26],
    )
    late: bool | None = Field(
        default=None,
        description="Filed after `disclosure_deadline`. Null exactly when `days_late` is.",
        examples=[True],
    )
    filing_status: str | None = Field(
        default=None,
        description=(
            "A House e-filed line's own status: `new`, or `amended` when it corrects "
            "a line of an earlier filing. Null on a scan, and on the Senate's."
        ),
        examples=["new"],
    )
    amendment: int | None = Field(
        default=None,
        description=(
            "On a Senate amendment's line, which amendment: `1` for "
            '"(Amendment 1)". An amendment restates the whole report, and the '
            "report it replaces is left out."
        ),
        examples=[None],
    )
    cap_gains_over_200: bool | None = Field(
        default=None,
        description="The House form's box: capital gains over $200 on a sale.",
        examples=[False],
    )
    description: str | None = Field(
        default=None,
        description="What the filer wrote under the asset: an option's strike and expiry.",
        examples=[
            "Purchased 50 call options with a strike price of $80 and an expiration "
            "date of 1/16/2026."
        ],
    )
    comment: str | None = Field(
        default=None,
        description="The filer's comment, as filed.",
        examples=["Spouse's managed account; no direct involvement."],
    )
    extraction: str = Field(
        description=(
            "How the line was read: `text` from the PDF's text, `html` from a Senate "
            "report filed online — both exact — or `ocr` from a scan."
        ),
        examples=["text"],
    )
    read_confidence: Score | None = Field(
        default=None,
        description=(
            "0 to 1, for a line read by OCR or accepted from review: how sure the "
            "reading is. Null when read exactly."
        ),
        examples=[None],
    )


class AmountRange(BaseModel):
    """Some lines' amounts, added up: their bands' low ends, and their high ends."""

    transactions: int = Field(description="Lines added up.", examples=[12])
    amount_low: int = Field(
        description="The least they can have been worth, in dollars: every band's lower end.",
        examples=[1265006],
    )
    amount_high: int | None = Field(
        default=None,
        description=(
            "The most: every band's upper end. Null when one of them is open "
            '("over $50,000,000"), so there is no most.'
        ),
        examples=[5500000],
    )
    open_ended: int = Field(
        description=(
            "Lines in an open band, which make `amount_high` null: each worth more than "
            "its lower end by an unknown amount."
        ),
        examples=[0],
    )


class TypeRange(AmountRange):
    """One kind of trade's lines, added up."""

    type: TradeType = Field(
        description="`sale` counts partial sales too: both are selling.", examples=["purchase"]
    )


class CongressSummary(BaseModel):
    """Every line the filters choose, not only this page's, added up.

    In ranges: a band's low and high ends are what the filing says; the middle
    of one is not, and nothing here is.
    """

    transactions: int = Field(description="Lines, on every page.", examples=[48])
    filings: int = Field(description="Filings they are in.", examples=[9])
    members: int = Field(description="Distinct members who filed them.", examples=[4])
    unmatched_filers: int = Field(
        description="Distinct names on them not known to be a member yet.", examples=[0]
    )
    by_type: list[TypeRange] = Field(
        description=(
            "Purchases, then sales (partial ones included), then exchanges: each one "
            "listed only when there is one."
        )
    )
    first_transaction_date: date | None = Field(
        default=None,
        description="The earliest trade day. Null with no line.",
        examples=["2024-01-08"],
    )
    last_transaction_date: date | None = Field(
        default=None,
        description="The latest trade day. Null with no line.",
        examples=["2026-06-20"],
    )
    late: int = Field(description="Lines filed after their deadline (`late`).", examples=[7])
    lag_known: int = Field(
        description=("Lines with a `disclosure_lag_days`: what `late` and the lags below are of."),
        examples=[46],
    )
    median_disclosure_lag_days: int | None = Field(
        default=None,
        description=(
            "The middle line's `disclosure_lag_days`, a real line's (the lower of two "
            "middles), never an average. Null when no lag is known."
        ),
        examples=[29],
    )
    max_disclosure_lag_days: int | None = Field(
        default=None,
        description="The longest `disclosure_lag_days`. Null when no lag is known.",
        examples=[214],
    )


class CongressCoverage(BaseModel):
    """How much of a chamber's PTRs the rows can include.

    A trade is in the feed only once its filing has been read, and only when it
    was read surely enough. What is not — scans no line could be read from,
    lines a person is still checking — is counted here, so that an absence in
    the rows is not taken for an absence of trading.
    """

    chamber: Chamber = Field(description="The chamber counted.", examples=["house"])
    filings: int = Field(
        description="PTRs the chamber's index lists, of those loaded.", examples=[453]
    )
    filings_read: int = Field(description="With at least one line in the feed.", examples=[402])
    filings_unreadable: int = Field(
        description=(
            "Read, with no line in the feed: none could be read (a scan, a Senate "
            "paper filing), or all are waiting for a person. Their `document_url` "
            "is the only way to see them."
        ),
        examples=[48],
    )
    filings_pending: int = Field(
        description="Not read yet, or their PDF was missing when tried.", examples=[3]
    )
    lines_in_review: int = Field(
        description="Lines read but not surely enough, left out until a person checks them.",
        examples=[383],
    )
    filings_unmatched_filer: int = Field(
        description=(
            "Filings whose filer is not known to be a member yet: in the feed, with `member` null."
        ),
        examples=[29],
    )
    assets_resolved_pct: Percent | None = Field(
        default=None,
        description=(
            "Of the lines in the feed that should name a stock (stocks, options, and "
            "assets of no known kind), the percentage resolved to one. Null with none."
        ),
        examples=["98.4"],
    )
    first_filing_date: date | None = Field(
        default=None, description="The earliest filing loaded.", examples=["2026-01-03"]
    )
    latest_filing_date: date | None = Field(
        default=None, description="The latest filing loaded.", examples=["2026-11-03"]
    )
    last_read_at: datetime | None = Field(
        default=None,
        description="When a filing was last read. Null when none has been.",
        examples=["2026-11-04T06:12:40Z"],
    )


class CongressFeedMeta(Meta):
    """``meta``, with every chosen line added up, and what the rows could include."""

    summary: CongressSummary = Field(description="Every line the filters choose, in ranges.")
    congress_coverage: list[CongressCoverage] = Field(
        description=(
            "Per chamber — both, or the one `chamber` names — how much of its PTRs "
            "have been read into the rows. A chamber with `filings` 0 has not been "
            "loaded at all."
        )
    )


class CongressTerm(BaseModel):
    """One term a member served."""

    chamber: Chamber = Field(description="The chamber they sat in.", examples=["house"])
    start_date: date = Field(description="The day the term began.", examples=["2025-01-03"])
    end_date: date = Field(
        description="As the Bioguide has it: the Congress's end, for a term still running.",
        examples=["2027-01-03"],
    )
    state: str = Field(description="The state they sat for, a USPS code.", examples=["CA"])
    district: int | None = Field(
        default=None,
        description="Their House district, `0` at large. Null for a senator.",
        examples=[11],
    )
    party: str | None = Field(
        default=None,
        description="Their party in the term, as the Bioguide spells it.",
        examples=["Democrat"],
    )


class CongressMember(BaseModel):
    """A member, from the Bioguide."""

    bioguide_id: str = Field(description="The Bioguide's id.", examples=["P000197"])
    name: str = Field(description="As the Bioguide gives it.", examples=["Nancy Pelosi"])
    first_name: str = Field(description="As the Bioguide gives it.", examples=["Nancy"])
    last_name: str = Field(description="As the Bioguide gives it.", examples=["Pelosi"])
    chamber: Chamber = Field(description="Their latest term's.", examples=["house"])
    state: str = Field(description="Their latest term's.", examples=["CA"])
    district: int | None = Field(default=None, description="Their latest term's.", examples=[11])
    party: str | None = Field(
        default=None, description="Their latest term's.", examples=["Democrat"]
    )
    in_office: bool = Field(
        description="In the Bioguide's current legislators, as it last said.", examples=[True]
    )
    terms: list[CongressTerm] = Field(description="Every term since 2008's, latest first.")


class UnreadFiling(BaseModel):
    """A member's filing none of whose lines are in the rows."""

    chamber: Chamber = Field(description="Where it was filed.", examples=["senate"])
    doc_id: str = Field(
        description="The chamber's id for the filing.",
        examples=["f2b5c0b4-6a8e-4c1f-9d7e-2b0e7c0f9a11"],
    )
    filing_date: date = Field(description="When the chamber received it.", examples=["2026-03-02"])
    document_url: str | None = Field(
        default=None,
        description="The original: the only way to see what it says.",
        examples=["https://efdsearch.senate.gov/search/view/paper/f2b5c0b4-…/"],
    )
    reason: UnreadReason = Field(
        description=(
            "`unreadable`: no line could be read; `in_review`: a person is checking "
            "them; `pending`: not read yet."
        ),
        examples=["unreadable"],
    )


class CongressMemberMeta(CongressFeedMeta):
    """``meta`` of a member's page: who they are, and what of theirs the rows leave out."""

    member: CongressMember = Field(description="The member whose lines the rows are.")
    unread_filings: list[UnreadFiling] = Field(
        description=(
            "Their filings with no line in the rows, latest first: the filing is "
            "there, what it says is not, and a reader should be told."
        )
    )


class CongressTradeEnvelope(Envelope[CongressTrade]):
    meta: CongressFeedMeta = Field(
        description="What the rows describe: every chosen line added up, and coverage."
    )


class CongressMemberEnvelope(Envelope[CongressTrade]):
    meta: CongressMemberMeta = Field(
        description="Who the member is, their lines added up, and what of theirs is missing."
    )
