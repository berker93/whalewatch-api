"""The price estimate and the returns since filing, on every row that is a security in a period.

:func:`~app.db.queries.returns.estimated_return` is a ``LATERAL``, joined onto
the page of rows an endpoint has already chosen, so it runs for the rows
served and no others: about 10 µs a row (docs/query-performance.md).
:func:`estimate_columns` is what to select from it, and :func:`estimates`
turns a row with those columns, and the change's ``is_split`` and
``split_ratio``, into :class:`~app.api.schemas.estimate.Estimates`' fields.
"""

from datetime import date
from typing import Any, Final

from sqlalchemy import ColumnElement, Row
from sqlalchemy.sql.selectable import LateralFromClause

from app.api.schemas.estimate import PriceEstimate
from app.core.periods import quarter_label

#: :data:`~app.db.queries.returns.ESTIMATED_RETURN_COLUMNS` an endpoint serves.
_SERVED: Final = (
    "implied_price",
    "price_confidence",
    "price_observation_count",
    "quote_date",
    "estimated_return_pct",
    "last_quarter_period",
    "return_to_last_quarter_pct",
)


def estimate_columns(returns: LateralFromClause) -> list[ColumnElement[Any]]:
    """What :func:`estimates` reads from an ``estimated_return`` join."""
    return [returns.c[name] for name in _SERVED]


def estimates(row: Row[Any], period: date) -> dict[str, Any]:
    """:class:`~app.api.schemas.estimate.Estimates`' fields, from a row with
    :func:`estimate_columns` and the change's ``is_split`` and ``split_ratio``.

    :param period: The row's own period, which the price estimate is at.
    """
    price_estimate = (
        None
        if row.implied_price is None
        else PriceEstimate(
            price=row.implied_price,
            period=quarter_label(period),
            confidence=row.price_confidence,
            observation_count=row.price_observation_count,
            basis="implied_from_filings",
        )
    )
    estimated = row.estimated_return_pct
    last = row.return_to_last_quarter_pct
    return {
        "estimated_return_pct": estimated,
        "estimated_return_as_of": row.quote_date if estimated is not None else None,
        "return_to_last_quarter_pct": last,
        "last_quarter_period": quarter_label(row.last_quarter_period) if last is not None else None,
        "price_estimate": price_estimate,
        "is_split": row.is_split,
        "split_ratio": row.split_ratio,
    }
