"""Estimated return since filing: what a stock has done since the quarter end a filer reported it.

A 13F says what a manager held at a quarter end and what it was worth. It does
not say when they bought it or at what price, so a return "since purchase" is
not something the filings can tell. A return **since filing** is: the price at
the period's close, which ``implied_price`` reads off every holder's filing,
against a later price. That is all this computes, for a ``hold`` as much as for
a ``new`` or an ``add``. docs/estimated-return.md is the methodology.

Two returns, to two later prices
--------------------------------
``estimated_return_pct``
    To the latest close in ``security_quote``: market data, from a quote
    provider. Production runs with ``QUOTE_PROVIDER=null``, so there it is
    always null.
``return_to_last_quarter_pct``
    To the security's latest implied price after the period: SEC-derived, so
    it is there in production too. It stops at a quarter end, and says which
    in ``last_quarter_period``.

Both are against the same basis, the implied price at the row's own period,
and both are null rather than zero when anything is missing: no implied price,
an implied price with no price (every holder reported $0), no later price.

Across a split
--------------
An implied price is per share as the shares were then, and a later price is
per share as they are now. NVDA's 10-for-1 makes every holder's 2024Q1 basis
ten times today's close, a return of -90% that nobody lost. So the basis is
divided by every split in ``security_split`` after the period and on or before
the later price's date, multiplied (``basis_split_ratio``).
:func:`~app.derived.position_change.split_between` is the same product the
changes are adjusted by.

A split is dated by the quarter end by which it had happened, so a quote dated
inside a split's quarter, before its end, could be from either side of it.
That return is null. It needs a quote older than the split's detection, which
is a quote provider left off for a quarter, not the normal case. The normal
case has a gap of its own: a split since the last quarter end is not in
``security_split`` until the next quarter's filings show it, and until then
the estimated return is off by its ratio. Nothing here can see that coming.

Read, not stored
----------------
Nothing is written. The latest quote changes daily, the splits and prices on
every ``recompute``, and a stored return would be stale after any of them.
:func:`estimated_return` is a ``LATERAL`` for an endpoint to join onto its
rows: per row, one probe of ``implied_price``'s primary key, a backward scan of
it and of ``security_quote``'s for the latest row, and ``security_split``'s
for the ratios. Each is an index probe on a key that leads with the security.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Date,
    Numeric,
    Select,
    SQLColumnExpression,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    select,
    text,
    true,
)
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import LateralFromClause

from app.db.models.implied_price import ImpliedPrice
from app.db.models.security_quote import SecurityQuote
from app.db.models.security_split import SecuritySplit
from app.derived.implied_price import PRICE_PLACES, SecurityPeriod, key_table
from app.derived.position_change import split_between

#: The places a return is rounded to, in percent. Two: it is an estimate from a
#: median of filings, and a third decimal would claim a precision it has not.
RETURN_PLACES: Final = 2

#: The places a split ratio is rounded to, as ``security_split.ratio`` and
#: ``position_change.split_ratio`` store it.
RATIO_PLACES: Final = 10

#: :func:`estimated_return`'s columns, in order.
ESTIMATED_RETURN_COLUMNS: Final = (
    "implied_price",
    "price_confidence",
    "price_observation_count",
    "quote_close",
    "quote_date",
    "basis_split_ratio",
    "basis_price",
    "estimated_return_pct",
    "last_quarter_period",
    "last_quarter_price",
    "last_quarter_confidence",
    "last_quarter_split_ratio",
    "return_to_last_quarter_pct",
)


def estimated_return(
    security_id: SQLColumnExpression[Any],
    period: SQLColumnExpression[Any] | date,
) -> LateralFromClause:
    """``LATERAL``: the estimated return since ``period`` of ``security_id``, one row or none.

    None when the security has no ``implied_price`` row in the period, so a
    ``LEFT JOIN ... ON true`` leaves every column null for it. Otherwise one
    row of :data:`ESTIMATED_RETURN_COLUMNS`:

    ``implied_price``, ``price_confidence``, ``price_observation_count``
        The basis as ``implied_price`` holds it, in the period's shares, with
        how sure of it the filings are. Null price and confidence when every
        holder reported $0.
    ``quote_close``, ``quote_date``
        The latest close after the period, or null.
    ``basis_split_ratio``
        Every split from after the period to the quote, multiplied: 10 across
        NVDA's 10-for-1. Null when none, or no quote.
    ``basis_price``
        ``implied_price`` over ``basis_split_ratio``: the price at the period
        end in today's shares. Null with no usable quote.
    ``estimated_return_pct``
        ``quote_close / basis_price - 1``, in percent.
    ``last_quarter_period``, ``last_quarter_price``, ``last_quarter_confidence``
        The latest implied price after the period, as ``implied_price`` holds
        it, or null when there is none: the period is the security's latest.
    ``last_quarter_split_ratio``
        Every split after the period to ``last_quarter_period``, multiplied.
    ``return_to_last_quarter_pct``
        ``last_quarter_price`` against the basis in that quarter's shares, in
        percent.

    :param security_id: The row's security, a column of the statement this is
        joined into.
    :param period: The quarter end the return is since: the row's period, a
        column, or one ``date`` for every row.
    """
    if isinstance(period, date):
        period = literal(period, Date)

    basis = aliased(ImpliedPrice, name="basis")
    later = aliased(ImpliedPrice, name="later_price")

    # Correlated to the basis row, not to the caller's columns, so each is a
    # lateral of a lateral with one thing to correlate to.
    split = SecuritySplit
    # A split dated the end of the quote's own quarter, which the quote is
    # before: it happened in that quarter, but before the quote or after it
    # the filings cannot say.
    straddled = exists().where(
        split.security_id == SecurityQuote.security_id,
        split.period_of_report > SecurityQuote.date,
        split.period_of_report <= _quarter_end(SecurityQuote.date),
    )
    quote = (
        select(SecurityQuote.close, SecurityQuote.date, straddled.label("straddled"))
        .where(
            SecurityQuote.security_id == basis.security_id,
            SecurityQuote.date > basis.period_of_report,
        )
        .order_by(SecurityQuote.date.desc())
        .limit(1)
        .lateral("quote")
    )
    last_quarter = (
        select(later.period_of_report, later.price, later.confidence)
        .where(
            later.security_id == basis.security_id,
            later.period_of_report > basis.period_of_report,
            later.price.is_not(None),
        )
        .order_by(later.period_of_report.desc())
        .limit(1)
        .lateral("last_quarter")
    )
    to_quote = split_between(
        basis.security_id, basis.period_of_report, quote.c.date, name="split_to_quote"
    )
    to_last = split_between(
        basis.security_id,
        basis.period_of_report,
        last_quarter.c.period_of_report,
        name="split_to_last_quarter",
    )

    usable = and_(quote.c.close.is_not(None), ~quote.c.straddled)

    def ratio(lateral: Any) -> ColumnElement[Any]:
        quotient: ColumnElement[Any] = _numeric(lateral.c.numerator) / lateral.c.denominator
        return quotient

    def since(price: ColumnElement[Any], lateral: Any) -> ColumnElement[Any]:
        # price over the basis in price's shares, less one, in percent. The
        # basis is divided by the ratio, so the ratio multiplies the price.
        times = func.coalesce(ratio(lateral), 1)
        return func.round((_numeric(price) * times / basis.price - 1) * 100, RETURN_PLACES)

    return (
        select(
            basis.price.label("implied_price"),
            basis.confidence.label("price_confidence"),
            basis.observation_count.label("price_observation_count"),
            quote.c.close.label("quote_close"),
            quote.c.date.label("quote_date"),
            case((usable, func.round(ratio(to_quote), RATIO_PLACES))).label("basis_split_ratio"),
            case(
                (
                    usable,
                    func.round(
                        _numeric(basis.price) / func.coalesce(ratio(to_quote), 1), PRICE_PLACES
                    ),
                )
            ).label("basis_price"),
            case((usable, since(quote.c.close, to_quote))).label("estimated_return_pct"),
            last_quarter.c.period_of_report.label("last_quarter_period"),
            last_quarter.c.price.label("last_quarter_price"),
            last_quarter.c.confidence.label("last_quarter_confidence"),
            func.round(ratio(to_last), RATIO_PLACES).label("last_quarter_split_ratio"),
            since(last_quarter.c.price, to_last).label("return_to_last_quarter_pct"),
        )
        .select_from(basis)
        .outerjoin(quote, true())
        .outerjoin(to_quote, true())
        .outerjoin(last_quarter, true())
        .outerjoin(to_last, true())
        .where(basis.security_id == security_id, basis.period_of_report == period)
        .lateral("estimated_return")
    )


def estimated_returns(keys: frozenset[SecurityPeriod]) -> Select[Any]:
    """``security_id``, ``period_of_report`` and :data:`ESTIMATED_RETURN_COLUMNS`
    for each of ``keys``, null where there is no implied price. Unordered."""
    pairs = key_table(keys)
    returns = estimated_return(pairs.c.security_id, pairs.c.period_of_report)
    return (
        select(pairs.c.security_id, pairs.c.period_of_report, *returns.c)
        .select_from(pairs)
        .outerjoin(returns, true())
    )


def _quarter_end(day: SQLColumnExpression[Any]) -> ColumnElement[Any]:
    """The last day of ``day``'s calendar quarter, as a ``date``."""
    return cast(
        func.date_trunc("quarter", day) + text("interval '3 months'") - text("interval '1 day'"),
        Date,
    )


def _numeric(expression: SQLColumnExpression[Any]) -> ColumnElement[Any]:
    """``expression`` as ``numeric`` of no fixed scale, so SQLAlchemy does not
    cast the arithmetic to a column's ``numeric(20, 8)``."""
    return cast(expression, Numeric())
