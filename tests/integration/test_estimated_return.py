"""Estimated return since filing: the implied price at a period against a later price.

Most tests write ``implied_price``, ``security_split`` and ``security_quote``
rows straight in, since the return reads nothing else: each is one rule on
made-up numbers. The first goes the whole way, from filings through
``recompute``, with the made-up ALPHA of ``test_security_split`` and its
10-for-1 in Q2 2024: a position held through it is not shown losing 90%.
"""

from datetime import date
from decimal import Decimal
from itertools import count
from typing import Any

from sqlalchemy import RowMapping, insert, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ImpliedPrice, Security, SecurityQuote, SecuritySplit
from app.db.queries.returns import ESTIMATED_RETURN_COLUMNS, estimated_return, estimated_returns
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from tests.integration.test_security_split import ALPHA, Q1, Q2, Q3, _held

D = Decimal

Q4 = date(2024, 12, 31)

_cusips = count(1)


async def _security(session: AsyncSession) -> int:
    security_id = await session.scalar(
        insert(Security).values(cusip=f"{next(_cusips):08d}0").returning(Security.id)
    )
    assert security_id is not None
    return security_id


async def _price(
    session: AsyncSession,
    security_id: int,
    period: date,
    price: str | None,
    *,
    confidence: str = "high",
) -> None:
    await session.execute(
        insert(ImpliedPrice).values(
            security_id=security_id,
            period_of_report=period,
            price=None if price is None else D(price),
            observation_count=5,
            unpriced_count=0 if price is not None else 5,
            agreeing_count=4 if price is not None else 0,
            confidence=None if price is None else confidence,
            suspect=False,
        )
    )


async def _split(
    session: AsyncSession, security_id: int, period: date, numerator: int, denominator: int
) -> None:
    ratio = D(numerator) / D(denominator)
    await session.execute(
        insert(SecuritySplit).values(
            security_id=security_id,
            period_of_report=period,
            numerator=numerator,
            denominator=denominator,
            confidence="high",
            holders=5,
            holders_agreeing=5,
            holders_exact=2,
            shares_ratio=ratio,
            price_ratio=1 / ratio,
        )
    )


async def _quote(session: AsyncSession, security_id: int, day: date, close: str) -> None:
    await session.execute(
        insert(SecurityQuote).values(
            security_id=security_id, date=day, close=D(close), ticker="ALPHA", source="yfinance"
        )
    )


async def _returns(session: AsyncSession, security_id: int, period: date) -> RowMapping:
    rows = (await session.execute(estimated_returns(frozenset({(security_id, period)})))).mappings()
    return rows.one()


def _picked(row: RowMapping, *names: str) -> tuple[Any, ...]:
    return tuple(row[name] for name in names)


RETURNS = ("estimated_return_pct", "return_to_last_quarter_pct")


# --- through a split -------------------------------------------------------------


async def test_a_position_held_through_a_ten_for_one_split_is_not_down_ninety_percent(
    db_session: AsyncSession,
) -> None:
    """ALPHA is $100 in Q1 and $10.50 after its 10-for-1, and closes at $12
    in October. As filed, the Q1 holders' $100 against $12 is -88%. In
    today's shares their basis is $10, and the stock is up 20% since. To the
    last quarter, Q3's $10.50, it is up 5%."""
    await _held(
        db_session,
        {Q1: 100, Q2: 1_000, Q3: 1_000},
        {Q1: 200, Q2: 2_000, Q3: 2_000},
        {Q1: 300, Q2: 3_000, Q3: 3_000},
    )
    await recompute(db_session, EVERYTHING)
    alpha = await db_session.scalar(select(Security.id).where(Security.cusip == ALPHA))
    assert alpha is not None
    await _quote(db_session, alpha, date(2024, 10, 15), "12")

    q1, q2, q3 = [await _returns(db_session, alpha, period) for period in (Q1, Q2, Q3)]

    assert _picked(
        q1, "implied_price", "basis_split_ratio", "basis_price", "estimated_return_pct"
    ) == (D(100), D(10), D(10), D("20.00"))
    assert _picked(q1, "last_quarter_period", "last_quarter_split_ratio") == (Q3, D(10))
    assert q1["return_to_last_quarter_pct"] == D("5.00")
    # After the split, nothing to adjust for.
    assert _picked(q2, "basis_split_ratio", "basis_price", *RETURNS) == (
        None,
        D("10.5"),
        D("14.29"),
        D("0.00"),
    )
    # The latest period has no later quarter: null, not zero.
    assert _picked(q3, *RETURNS) == (D("14.29"), None)


async def test_splits_multiply_and_a_reverse_split_divides(db_session: AsyncSession) -> None:
    """A 2-for-1 and then a 3-for-1 make one Q1 share six today. A 1-for-10
    makes ten Q1 shares one."""
    split_twice = await _security(db_session)
    await _price(db_session, split_twice, Q1, "60")
    await _split(db_session, split_twice, Q2, 2, 1)
    await _split(db_session, split_twice, Q3, 3, 1)
    await _quote(db_session, split_twice, date(2024, 10, 15), "11")

    reversed_ = await _security(db_session)
    await _price(db_session, reversed_, Q1, "2")
    await _split(db_session, reversed_, Q2, 1, 10)
    await _quote(db_session, reversed_, date(2024, 10, 15), "25")

    twice = await _returns(db_session, split_twice, Q1)
    reverse = await _returns(db_session, reversed_, Q1)

    assert _picked(twice, "basis_split_ratio", "basis_price", "estimated_return_pct") == (
        D(6),
        D(10),
        D("10.00"),
    )
    assert _picked(reverse, "basis_split_ratio", "basis_price", "estimated_return_pct") == (
        D("0.1"),
        D(20),
        D("25.00"),
    )


async def test_a_split_on_or_before_the_period_is_not_adjusted_for(
    db_session: AsyncSession,
) -> None:
    """A split dated Q2 had happened by Q2's close, so Q2's price is already in
    the new shares."""
    security = await _security(db_session)
    await _price(db_session, security, Q2, "10")
    await _split(db_session, security, Q2, 10, 1)
    await _quote(db_session, security, date(2024, 10, 15), "11")

    row = await _returns(db_session, security, Q2)

    assert _picked(row, "basis_split_ratio", "estimated_return_pct") == (None, D("10.00"))


# --- the quote -------------------------------------------------------------------


async def test_the_latest_quote_is_the_one_compared(db_session: AsyncSession) -> None:
    security = await _security(db_session)
    await _price(db_session, security, Q3, "100")
    await _quote(db_session, security, date(2024, 10, 14), "90")
    await _quote(db_session, security, date(2024, 10, 15), "110")

    row = await _returns(db_session, security, Q3)

    assert _picked(row, "quote_date", "quote_close", "estimated_return_pct") == (
        date(2024, 10, 15),
        D(110),
        D("10.00"),
    )


async def test_no_quote_leaves_the_estimated_return_null_and_the_quarterly_one_there(
    db_session: AsyncSession,
) -> None:
    """Production: QUOTE_PROVIDER=null, so security_quote is empty. The return
    to the last quarter is from filings alone, and still there."""
    security = await _security(db_session)
    await _price(db_session, security, Q1, "50")
    await _price(db_session, security, Q2, "55")

    row = await _returns(db_session, security, Q1)

    assert _picked(
        row, "quote_close", "basis_split_ratio", "basis_price", "estimated_return_pct"
    ) == (None, None, None, None)
    assert _picked(row, "last_quarter_period", "return_to_last_quarter_pct") == (Q2, D("10.00"))


async def test_a_quote_from_before_the_period_ends_is_not_a_return_since_it(
    db_session: AsyncSession,
) -> None:
    """A quote from a provider left off since before the period: the period's
    own close is the latest price there is, and on the day itself the return
    is nothing anyone would call one."""
    security = await _security(db_session)
    await _price(db_session, security, Q3, "100")
    await _quote(db_session, security, date(2024, 8, 1), "90")
    await _quote(db_session, security, Q3, "101")

    row = await _returns(db_session, security, Q3)

    assert _picked(row, "quote_close", "estimated_return_pct") == (None, None)


async def test_a_quote_inside_a_splits_quarter_is_too_ambiguous_to_compare(
    db_session: AsyncSession,
) -> None:
    """A split dated Q3 happened some time in July to September. A quote from
    August could be from either side of it. One from Q3's close is after it."""
    security = await _security(db_session)
    await _price(db_session, security, Q2, "100")
    await _split(db_session, security, Q3, 2, 1)

    await _quote(db_session, security, date(2024, 8, 15), "48")
    inside = await _returns(db_session, security, Q2)
    await _quote(db_session, security, Q3, "52")
    at_end = await _returns(db_session, security, Q2)

    assert _picked(inside, "quote_close", "basis_split_ratio", "estimated_return_pct") == (
        D(48),
        None,
        None,
    )
    assert _picked(at_end, "basis_split_ratio", "estimated_return_pct") == (D(2), D("4.00"))


async def test_a_quote_before_a_splits_quarter_needs_no_adjusting(
    db_session: AsyncSession,
) -> None:
    """A split dated Q4 happened after September, so a September quote is
    from before it, and Q1's price compares to it as it is."""
    security = await _security(db_session)
    await _price(db_session, security, Q1, "100")
    await _split(db_session, security, Q4, 2, 1)
    await _quote(db_session, security, date(2024, 9, 15), "120")

    row = await _returns(db_session, security, Q1)

    assert _picked(row, "basis_split_ratio", "estimated_return_pct") == (None, D("20.00"))


# --- the basis -------------------------------------------------------------------


async def test_no_implied_price_is_null_not_zero(db_session: AsyncSession) -> None:
    """No row in the period, or a row whose every holder reported $0."""
    unheld = await _security(db_session)
    await _quote(db_session, unheld, date(2024, 10, 15), "10")

    worthless = await _security(db_session)
    await _price(db_session, worthless, Q1, None)
    await _price(db_session, worthless, Q2, "10")
    await _quote(db_session, worthless, date(2024, 10, 15), "10")

    nothing = await _returns(db_session, unheld, Q1)
    unpriced = await _returns(db_session, worthless, Q1)

    assert all(nothing[name] is None for name in ESTIMATED_RETURN_COLUMNS)
    assert _picked(unpriced, "implied_price", "price_confidence", *RETURNS) == (
        None,
        None,
        None,
        None,
    )
    assert unpriced["price_observation_count"] == 5


async def test_the_confidence_and_count_travel_with_the_price(db_session: AsyncSession) -> None:
    security = await _security(db_session)
    await _price(db_session, security, Q1, "10", confidence="low")
    await _price(db_session, security, Q2, "12", confidence="medium")

    row = await _returns(db_session, security, Q1)

    assert _picked(
        row,
        "price_confidence",
        "price_observation_count",
        "last_quarter_confidence",
        "last_quarter_price",
    ) == ("low", 5, "medium", D(12))


async def test_the_last_quarter_is_the_latest_with_a_price(db_session: AsyncSession) -> None:
    security = await _security(db_session)
    await _price(db_session, security, Q1, "100")
    await _price(db_session, security, Q2, "80")
    await _price(db_session, security, Q3, None)

    row = await _returns(db_session, security, Q1)

    assert _picked(row, "last_quarter_period", "return_to_last_quarter_pct") == (Q2, D("-20.00"))


async def test_returns_are_rounded_to_two_places(db_session: AsyncSession) -> None:
    security = await _security(db_session)
    await _price(db_session, security, Q1, "3")
    await _quote(db_session, security, date(2024, 10, 15), "4")

    row = await _returns(db_session, security, Q1)

    assert row["estimated_return_pct"] == D("33.33")


# --- joined to an endpoint's rows ------------------------------------------------


async def test_it_joins_onto_rows_for_one_period(db_session: AsyncSession) -> None:
    """As the portfolio will join it: one period for every row, as a date."""
    priced, unpriced = await _security(db_session), await _security(db_session)
    await _price(db_session, priced, Q1, "10")
    await _quote(db_session, priced, date(2024, 10, 15), "15")

    returns = estimated_return(Security.id, Q1)
    rows = await db_session.execute(
        select(Security.id, returns.c.estimated_return_pct)
        .outerjoin(returns, true())
        .where(Security.id.in_([priced, unpriced]))
        .order_by(Security.id)
    )

    assert list(rows.tuples()) == [(priced, D("50.00")), (unpriced, None)]


async def test_a_literal_period_and_a_column_give_the_same_row(db_session: AsyncSession) -> None:
    security = await _security(db_session)
    await _price(db_session, security, Q1, "10")
    await _price(db_session, security, Q2, "11")
    await _quote(db_session, security, date(2024, 10, 15), "12")

    by_date = estimated_return(literal(security), Q1)
    from_column = await _returns(db_session, security, Q1)
    row = (await db_session.execute(select(by_date))).mappings().one()

    assert dict(row) == {name: from_column[name] for name in ESTIMATED_RETURN_COLUMNS}
