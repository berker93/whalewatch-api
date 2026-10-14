"""The split rule, on the evidence alone: what :func:`judge` calls a split, and how sure.

The candidates are the real ones from five years of 100 filers, as
``detect-splits --all --dry-run`` reported them, or close copies. The query that
gathers the evidence is ``tests/integration/test_security_split.py``'s.
"""

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from app.db.models.security_split import SplitConfidence
from app.derived.security_split import (
    SPLIT_FRACTIONS,
    Candidate,
    is_outlier,
    judge,
    split_keys,
)

D = Decimal
Q2 = date(2024, 6, 30)


def candidate(
    fraction: Fraction | None,
    *,
    agreeing: int,
    holders: int,
    exact: int = 0,
    shares: str = "1",
    price: str | None = "1",
) -> Candidate:
    return Candidate(
        security_id=1,
        period_of_report=Q2,
        holders=holders,
        shares_ratio=D(shares),
        fraction=fraction,
        agreeing=agreeing,
        exact=exact,
        price_ratio=None if price is None else D(price),
    )


# --- the fractions --------------------------------------------------------------


def test_the_fractions_include_25_and_50_for_1_and_the_reverse_of_each() -> None:
    forward = {fraction for fraction in SPLIT_FRACTIONS if fraction > 1}

    assert {Fraction(25), Fraction(50)} <= forward
    assert {1 / fraction for fraction in forward} == set(SPLIT_FRACTIONS) - forward


# --- splits --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "evidence", "confidence"),
    [
        (
            "AMZN 2022Q2, in a falling quarter",
            candidate(Fraction(20), agreeing=21, holders=35, exact=2, price="0.0326"),
            SplitConfidence.HIGH,
        ),
        (
            "CMG 2024Q2, 50-for-1",
            candidate(Fraction(50), agreeing=4, holders=11, exact=1, price="0.0216"),
            SplitConfidence.HIGH,
        ),
        (
            "SHOP 2022Q2, which fell 54%",
            candidate(Fraction(10), agreeing=6, holders=13, price="0.0462"),
            SplitConfidence.MEDIUM,
        ),
        (
            "ISRG 2021Q4, nine of twelve agreeing and none exact",
            candidate(Fraction(3), agreeing=9, holders=12, price="0.3614"),
            SplitConfidence.MEDIUM,
        ),
        (
            "WSM 2024Q3, 2-for-1 with one holder exact",
            candidate(Fraction(2), agreeing=2, holders=5, exact=1, price="0.5486"),
            SplitConfidence.MEDIUM,
        ),
        (
            "USLM 2024Q3, two holders of two, 5-for-1",
            candidate(Fraction(5), agreeing=2, holders=2, price="0.2682"),
            SplitConfidence.LOW,
        ),
        (
            "a 1-for-10 reverse split",
            candidate(Fraction(1, 10), agreeing=3, holders=4, exact=2, price="10.5"),
            SplitConfidence.HIGH,
        ),
    ],
)
def test_a_split_the_holders_and_the_price_agree_on(
    name: str, evidence: Candidate, confidence: SplitConfidence
) -> None:
    assert judge(evidence).confidence == confidence


# --- not splits ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "evidence", "reason"),
    [
        (
            "no fraction near the median",
            candidate(None, agreeing=0, holders=31, shares="1.12", price="1.379"),
            "no common ratio",
        ),
        (
            "one holder, however exact",
            candidate(Fraction(10), agreeing=1, holders=1, exact=1, price="0.1"),
            "1 of 1 holders agree",
        ),
        (
            "two holders of eleven: ON's 2-for-3 in 2021Q4",
            candidate(Fraction(2, 3), agreeing=2, holders=11, exact=1, price="1.4839"),
            "2 of 11 holders agree",
        ),
        (
            "a price on one side only",
            candidate(Fraction(10), agreeing=3, holders=3, exact=3, price=None),
            "no price on one side",
        ),
        (
            "the price fell less than half as far as the shares grew",
            candidate(Fraction(10), agreeing=3, holders=3, exact=1, price="0.4"),
            "the price moved x0.400",
        ),
        (
            "a 10-for-1 in a quarter the stock would have risen 130%",
            candidate(Fraction(10), agreeing=3, holders=3, price="0.2323"),
            "the price moved x0.232",
        ),
        (
            "rebalancing: UBER's 2023Q4 trims as the price rose a third",
            candidate(Fraction(2, 3), agreeing=7, holders=14, price="1.34"),
            "no holder exact to the share",
        ),
        (
            "buying a crash: a 3-for-1 with neither an exact holder nor a majority",
            candidate(Fraction(3), agreeing=2, holders=5, price="0.4532"),
            "no holder exact to the share, and no majority agreeing",
        ),
    ],
)
def test_not_a_split(name: str, evidence: Candidate, reason: str) -> None:
    verdict = judge(evidence)

    assert not verdict.split
    assert verdict.reason == reason


def test_the_product_band_is_inclusive_at_its_floor() -> None:
    """SHOP-like: 10-for-1 in a quarter the stock fell 60%, exactly the floor."""
    assert judge(candidate(Fraction(10), agreeing=3, holders=3, price="0.04")).split


# --- outliers --------------------------------------------------------------------


def test_an_outlier_is_shares_and_price_moving_apart_by_two_and_a_half_without_a_split() -> None:
    """CTAS's 4-for-1 in 2024Q3, which one holder of five moved by."""
    ctas = judge(candidate(Fraction(4), agreeing=1, holders=5, shares="4.44", price="0.294"))
    reverse = judge(candidate(None, agreeing=0, holders=2, shares="0.155", price="14.24"))
    profit_taking = judge(
        candidate(Fraction(1, 3), agreeing=1, holders=6, shares="0.35", price="2")
    )
    split = judge(candidate(Fraction(10), agreeing=3, holders=3, shares="10", price="0.1"))

    assert is_outlier(ctas)
    assert is_outlier(reverse)
    assert not is_outlier(profit_taking)
    assert not is_outlier(split)


# --- keys ------------------------------------------------------------------------


def test_split_keys_add_the_quarter_after_each() -> None:
    keys = split_keys({(1, date(2024, 12, 31)), (2, Q2)})

    assert keys == {
        (1, date(2024, 12, 31)),
        (1, date(2025, 3, 31)),
        (2, Q2),
        (2, date(2024, 9, 30)),
    }
