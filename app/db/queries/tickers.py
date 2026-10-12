"""Tickers as of a date, and tickers two securities claim at once.

A ticker is a label at the API's edge. Inside, everything joins on
``security_id``. These are the two places a ticker is read with a date:

* :func:`ticker_labels`, the columns every response that names a security in a
  period selects: the ticker *then*, the ticker *now*, and whether it is still
  listed. Berkshire's 2021Q4 portfolio shows ``FB`` with ``META`` beside it.
* :func:`ticker_collisions`, the one query here that compares tickers across
  securities, because a collision is precisely two securities answering to
  the same label at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Date, and_, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.security import Security
from app.db.models.security_alias import SecurityAlias


def valid_on(on: Any, alias: type[SecurityAlias] | Any = SecurityAlias) -> ColumnElement[bool]:
    """``alias``'s range covers the date ``on``: ``[valid_from, valid_to)``."""
    on = _as_date(on)
    return and_(
        or_(alias.valid_from.is_(None), alias.valid_from <= on),
        or_(alias.valid_to.is_(None), alias.valid_to > on),
    )


def ticker_at(on: Any) -> ColumnElement[str | None]:
    """The ticker ``Security`` traded under on ``on``, else its current one.

    One index probe per row (security_alias's rows for one security are a
    handful). Falls back to ``security.ticker`` when the history has no range
    covering the date: a period before the first ticker we know, or after a
    delisting closed the last one. That fallback is the last label it had,
    which is better than none.

    :param on: A date or a date-valued column: the period the row is for.
    """
    alias = SecurityAlias
    then = (
        select(alias.ticker)
        .where(alias.security_id == Security.id, valid_on(on, alias))
        .order_by(alias.valid_from.desc().nulls_last())
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(then, Security.ticker)


def ticker_labels(on: Any, *, prefix: str = "") -> list[ColumnElement[Any]]:
    """``ticker`` (on ``on``), ``current_ticker`` and ``delisted_by``, for a
    statement that joins ``Security``. ``prefix`` names them ``top_ticker`` etc."""
    return [
        ticker_at(on).label(f"{prefix}ticker"),
        Security.ticker.label(f"{prefix}current_ticker"),
        Security.delisted_by.label(f"{prefix}delisted_by"),
    ]


def _as_date(on: Any) -> Any:
    """A Python date as a typed literal, so asyncpg is not asked to guess."""
    return literal(on, Date) if isinstance(on, date) else on


# --- collisions -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollidingSecurity:
    cusip: str
    name: str | None
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class TickerCollision:
    """One ticker, held by two or more securities over overlapping ranges."""

    ticker: str
    securities: tuple[CollidingSecurity, ...]


_COLLISIONS = text(
    """
    WITH ranged AS (
        SELECT a.id, a.security_id, upper(a.ticker) AS ticker,
               daterange(a.valid_from, a.valid_to) AS valid
        FROM security_alias a
    ),
    clashing AS (
        SELECT DISTINCT x.id
        FROM ranged x
        JOIN ranged y
          ON y.ticker = x.ticker
         AND y.security_id <> x.security_id
         AND y.valid && x.valid
    )
    SELECT r.ticker, s.cusip, s.name, lower(r.valid) AS valid_from, upper(r.valid) AS valid_to
    FROM clashing c
    JOIN ranged r ON r.id = c.id
    JOIN security s ON s.id = r.security_id
    ORDER BY r.ticker, lower(r.valid) NULLS FIRST, s.cusip
    """
)


async def ticker_collisions(session: AsyncSession) -> list[TickerCollision]:
    """Every ticker two securities hold over overlapping ranges.

    Typically a CUSIP change OpenFIGI maps to the current ticker on the old
    CUSIP too, or a recycled ticker whose first owner's delisting we have no
    list to see. Either way the lookup picks one by recency, and a reader may be
    shown the other's figures, so each is reported for a person to settle with
    ``ticker-alias``. Ranges that meet without overlapping (a delisting, then
    the ticker reused) are history working as intended, and not reported.
    """
    rows = (await session.execute(_COLLISIONS)).all()
    grouped: dict[str, list[CollidingSecurity]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(
            CollidingSecurity(row.cusip, row.name, row.valid_from, row.valid_to)
        )
    return [TickerCollision(ticker, tuple(held)) for ticker, held in grouped.items()]
