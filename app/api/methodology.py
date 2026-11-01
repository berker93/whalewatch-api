"""The Methodology sheet of every Excel export: how the numbers in it are made.

An export is forwarded to people who never saw the page or its tooltips, so
this says in full what the page leaves to them: what an implied price is and
how sure of it we are, what a split does to a change, what each action means.
Each threshold is the constant the pipeline runs on, read here rather than
restated, so the sheet cannot fall behind a change to one.

:func:`thirteen_f` is for the 13F exports, :data:`FORM4` for the insider feed's.
"""

from decimal import Decimal
from fractions import Fraction
from typing import Final

from app.api.schemas.insider import CODE_LABELS
from app.api.xlsx_export import Section
from app.db.models.insider_transaction import OPEN_MARKET_CODES
from app.db.queries.returns import RETURN_PLACES
from app.derived.implied_price import AGREEMENT_PCT, PRICE_PLACES
from app.derived.position_change import HOLD_BAND_PCT
from app.derived.security_split import (
    AGREEMENT_TOLERANCE,
    EXACT_NOT_REQUIRED_FROM,
    EXACT_SHARES,
    MAJORITY_ENOUGH_FROM,
    MIN_AGREEING,
    MIN_AGREEING_SHARE,
    OUTLIER_FACTOR,
    PRODUCT_BAND,
    SPLIT_FRACTIONS,
)


def _ratio(fraction: Fraction) -> str:
    """``3-for-2``, ``1-for-10``."""
    return f"{fraction.numerator}-for-{fraction.denominator}"


def _number(value: Decimal) -> str:
    """``2``, ``0.01``, ``1.25``: no trailing zeros, never an exponent."""
    return format(value.normalize(), "f")


def _units(*, form4: bool) -> Section:
    shares = (
        "Share counts are shown to the whole share. The cell holds the count as "
        "filed, fractions included."
    )
    return Section(
        "Reading this workbook",
        (
            "Data is the page's table: every row of every page, in the page's order, "
            "with the columns of the CSV export by the same names. About says when the "
            "file was made, and of what.",
            (
                "Numbers",
                "Every number is a number Excel can add, not text. Dollars are US "
                "dollars as filed. Percentages are real percentages: a weight of 12.50% "
                "is the number 0.125, so SUM, AVERAGE and charts work on it. "
                + (
                    shares
                    if form4
                    else "A change in weight (weight_delta) is in percentage points, "
                    "shown as pp: 1.25 pp is 1.25, not 0.0125. " + shares
                ),
            ),
            (
                "Text",
                "CUSIPs, tickers, CIKs and accession numbers are text, so a CUSIP like "
                "037833100 keeps its leading zero. Text is never a formula, whatever "
                "it starts with.",
            ),
            (
                "Times",
                "Dates are yyyy-mm-dd. Times (filed_at) are UTC. A blank cell is "
                "unknown, or does not apply to the row.",
            ),
            (
                "Colours",
                "A change is teal when positive and crimson when negative: WhaleWatch's "
                "colours for buying and selling. They say which way, not whether it "
                "was a good idea.",
            ),
        ),
    )


def _actions() -> Section:
    band = _number(HOLD_BAND_PCT)
    return Section(
        "Actions",
        (
            "A 13F says what a manager held at a quarter end. What they did is read "
            "from the difference between two of their filings: this quarter's against "
            "their previous published one. Previous means the last quarter we have a "
            "filing for: across a quarter with none, the change is against the one "
            "before the gap, and prev_period names it.",
            ("new", "Held now, not held in the previous filing."),
            (
                "add",
                f"More shares than the previous filing, by more than {band}% of them.",
            ),
            (
                "trim",
                f"Fewer shares, by more than {band}%. A trim is the reported position "
                "shrinking. It is not necessarily a sale: shares can be lent, "
                "transferred or converted.",
            ),
            (
                "hold",
                f"Within {band}% either way. Dividend reinvestment and rounding move "
                "share counts by a handful with nobody trading. The small delta is "
                "kept, so the deltas still add up to the position.",
            ),
            (
                "exit",
                "Held in the previous filing and not in this one. Its deltas are the "
                "whole previous position, negative. A manager that has not filed its "
                "next quarter yet has not exited anything.",
            ),
            "A manager's first quarter with us is all new positions. That is where "
            "our history of the manager starts, not a quarter in which it bought "
            "everything, so the activity and the screener leave it out.",
        ),
    )


def _implied_price() -> Section:
    return Section(
        "Implied price",
        (
            "A 13F line gives a position's market value on the quarter's last day and "
            "its share count, so value divided by shares is that manager's report of "
            "the quarter-end price. Every manager holding the stock is one "
            "observation of the same number. No market data is involved.",
            (
                "Derivation",
                "The implied price is the median of the observations. It is the "
                "middle observation itself, never a blend of the two either side of "
                "the middle, so it is always a price a manager reported. It is not "
                "weighted by position size: one large holder carrying a stock at book "
                f"value cannot move it. It is stored to {PRICE_PLACES} decimal places.",
            ),
            (
                "Unpriced",
                "A line worth $0 is counted in observation_count and left out of the "
                "median: it says the manager could not price the stock, not that the "
                "stock was free. When every holder reported $0 there is no implied "
                "price, and its cells are blank.",
            ),
            (
                "observation_count",
                "Managers with shares in the stock in the quarter: one observation "
                "each, priced or not.",
            ),
            (
                "What it is not",
                "A quote. It is the quarter-end close as the holders' filings state "
                "it, as of the quarter the row is in, and it is only as good as the "
                "agreement between them, which is the confidence.",
            ),
        ),
    )


def _confidence() -> Section:
    agreement = _number(AGREEMENT_PCT)
    return Section(
        "Confidence scale",
        (
            f"An observation agrees with the implied price when it is within "
            f"{agreement}% of it, either way. Before 2023 a 13F value is in "
            f"thousands of dollars, rounded, which alone can put a small position 1% "
            f"off: {agreement}% lets that through, and nothing like a split, a scale "
            "error or a stale price. Only priced observations are counted.",
            ("high", "Three or more observations agree, and they are a majority."),
            (
                "medium",
                "Two agree, and they are a majority: two holders who agree, or two of three.",
            ),
            (
                "low",
                "One holder, or no majority agreeing. The price is still the median "
                "and still the best estimate there is, but a single manager reporting "
                "in thousands, or a share count not yet updated for a split, could be "
                "all of it.",
            ),
            (
                "blank",
                "No implied price: nobody held the stock with shares, or every holder reported $0.",
            ),
        ),
    )


def _splits() -> Section:
    forward = [f for f in SPLIT_FRACTIONS if f > 1]
    low, high = PRODUCT_BAND
    share = MIN_AGREEING_SHARE
    return Section(
        "Split adjustment",
        (
            "Share counts are as filed, so a 4-for-1 split would make every holder "
            "look as if it had quadrupled its position. Nothing in a 13F says a split "
            "happened, and we use no corporate-action feed: splits are detected from "
            "the filings, and changes across them adjusted.",
            (
                "Detection",
                "For each stock and quarter, every manager that held it this quarter "
                "and the one before has a shares ratio, now over then. The median "
                "ratio is snapped to the nearest split ratio we detect: "
                + ", ".join(_ratio(f) for f in forward)
                + ", and the reverse of each. It must be within "
                f"{_number(AGREEMENT_TOLERANCE)}x of it either way, or there is no "
                "candidate.",
            ),
            (
                "Agreement",
                f"At least {MIN_AGREEING} holders, and at least "
                f"{share.numerator} in {share.denominator} of them, must have a ratio "
                f"within {_number(AGREEMENT_TOLERANCE)}x of the split's. So no single "
                "filing can make a split.",
            ),
            (
                "Price",
                "The implied price has to have moved the other way. The split ratio "
                "times the price ratio is one plus the stock's return over the quarter: "
                f"it must be between {_number(low)} and {_number(high)}, and nearer one "
                "than the split ratio is.",
            ),
            (
                "Small ratios",
                "A manager keeping a stock at a fixed weight trims a quarter of it "
                "when the price rises a third, which looks like a 2-for-3. So the "
                "nearer a ratio is to one, the more it needs. "
                f"{_ratio(EXACT_NOT_REQUIRED_FROM)} and beyond, either way: the rules "
                f"above. {_ratio(MAJORITY_ENOUGH_FROM)} and its reverse: a holder "
                f"exact to the share (within {EXACT_SHARES} share of its previous "
                "count times the ratio, a manager that did not trade), or at least "
                "three holders agreeing and a majority of them. Anything nearer one: "
                "a holder exact to the share.",
            ),
            (
                "Adjustment",
                "A change whose previous filing is before a split and whose own is on "
                "or after it is taken against the previous shares times the ratio "
                "(times each ratio, across more than one). is_split_adjusted is TRUE "
                "and split_ratio is the multiple. A manager that held through it is a "
                "hold, one that held and bought is an add of what it bought, and "
                "shares_delta and shares_delta_pct are in today's shares. The "
                "previous figures stay as filed, so across a split the deltas no "
                "longer add up to the position.",
            ),
            (
                "Not adjusted",
                "A new position, which has no previous shares, and an exit, which is "
                "the position as filed whatever the ratio. Spin-offs, mergers, ADR "
                "ratio changes, stock dividends and splits of other ratios are not "
                "detected. Where shares and price moved apart by "
                f"{_number(OUTLIER_FACTOR)}x or more with no split detected, the "
                "change is left as filed and listed for review.",
            ),
            (
                "Timing",
                "A split is dated by the quarter end by which it had happened, and is "
                "known only once that quarter's filings are in: a split since the last "
                "quarter end is not adjusted for yet.",
            ),
        ),
    )


def _returns() -> Section:
    return Section(
        "Estimated returns",
        (
            "A 13F does not say when a position was bought or at what price, so no "
            "return since purchase can be read from it. These are returns since the "
            "quarter end the row is of, from that quarter's implied price, rounded "
            f"to {RETURN_PLACES} decimal places.",
            (
                "estimated_return_pct",
                "To the latest close we hold a quote for, dated by "
                "estimated_return_as_of. Blank where no quote is held.",
            ),
            (
                "return_to_last_quarter_pct",
                "To the stock's latest implied price after the row's quarter, the one "
                "last_quarter_period names. Filings only, no market data.",
            ),
            (
                "Across a split",
                "The starting price is divided by every split since, so a 10-for-1 is "
                "not a 90% loss. A quote dated inside a split's quarter could be "
                "from either side of the split, and its return is blank.",
            ),
            "Either is blank, not zero, when a price it needs is missing.",
        ),
    )


def _flows() -> Section:
    return Section(
        "Screener figures",
        (
            "A 13F has one price per quarter, the quarter end's, so dollars traded are "
            "estimated at it: a position bought at $50 and worth $80 by the quarter "
            "end counts as $80 of buying.",
            (
                "gross_bought_usd",
                "Dollars of buying: new positions at their whole value, adds at the "
                "shares added. gross_sold_usd likewise for trims and exits, an exit "
                "at the price it was last held at. A hold trades nothing.",
            ),
            ("net_value_usd", "Bought less sold."),
            (
                "A year",
                "Four quarters ending with the period. The dollars are the four "
                "quarters' sums, so a year adds up to its quarters. The counts are "
                "managers, each counted once however many of the quarters it traded in.",
            ),
            (
                "Coverage",
                "13Fs arrive across the 45 days after a quarter end, so a quarter's "
                "figures are partial until they are in. About says how many managers "
                "had filed when this file was made.",
            ),
        ),
    )


def thirteen_f(*, flows: bool = False) -> tuple[Section, ...]:
    """The 13F exports' sheet; with ``flows``, the screener's own figures as well."""
    sections = [
        _units(form4=False),
        _actions(),
        _implied_price(),
        _confidence(),
        _splits(),
        _returns(),
    ]
    if flows:
        sections.insert(1, _flows())
    return tuple(sections)


FORM4: Final = (
    _units(form4=True),
    Section(
        "Form 4 lines",
        (
            "Each row is one line of a Form 4, the filing a company's directors, "
            "officers and 10% owners make within two business days of trading its "
            "stock. Some are filed late. Each figure is as the insider reported it: "
            "nothing is implied, so confidence and observation_count are blank.",
            (
                "Price",
                "The price the insider reported. Blank where they wrote 0, which is "
                "how a grant, a gift or a vesting is filed, and where the price is "
                "given only as a range in a footnote. A weighted average over several "
                "trades has its range in the footnotes.",
            ),
            (
                "value_usd",
                "Shares times price, to the cent. Blank when either is.",
            ),
            (
                "Derivatives",
                "On a derivative line (is_derivative TRUE), shares are of the "
                "derivative and the price is what was paid for the derivative itself, "
                "not the strike. A grant or an exercise has no value.",
            ),
            (
                "is_10b5_1",
                "Made under a Rule 10b5-1 plan, set up in advance: it says nothing "
                "about what the insider thinks today. Read from a footnote the line "
                "cites, else from the form's checkbox when no line cites one.",
            ),
            (
                "Amendments",
                "A Form 4/A replaces the Form 4 it amends: only the amendment's lines are here.",
            ),
            (
                "Joint filings",
                "A trade reported by several owners on one form is one line, under "
                "the first owner named.",
            ),
        ),
    ),
    Section(
        "Transaction codes",
        (
            "The SEC's code for each line. "
            + " and ".join(sorted(OPEN_MARKET_CODES))
            + " are open-market trades, and the feed's default; the rest are "
            "compensation and mechanics. About says which codes this file has.",
            *((code, label) for code, label in CODE_LABELS.items()),
        ),
    ),
)
