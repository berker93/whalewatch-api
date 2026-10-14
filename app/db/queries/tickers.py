"""Tickers as of a date, and tickers two securities claim at once.

A ticker is a label at the API's edge. Inside, everything joins on
``security_id``. These are the places a ticker is read with a date:

* :func:`ticker_labels`, the columns every response that names a security in a
  period selects: the ticker *then*, the ticker *now*, and whether it is still
  listed. Berkshire's 2021Q4 portfolio shows ``FB`` with ``META`` beside it.
* :func:`lookup_query`, the other way round: the security a ticker names on a
  date. ``/v1/stocks/{ticker}`` reads its path with it, and the insider loader
  the tickers SEC lists for a Form 4's issuer.
* :func:`ticker_collisions`, the one query here that compares tickers across
  securities, because a collision is precisely two securities answering to
  the same label at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import (
    BindParameter,
    ColumnElement,
    Date,
    Integer,
    Select,
    and_,
    cast,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.position_snapshot import PositionSnapshot
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


def _last_held(security_id: Any) -> Any:
    """The newest period ``security_id`` is in ``position_snapshot``: one
    entry of the (security_id, period_of_report) index, read backwards."""
    snapshot = PositionSnapshot
    return (
        select(func.max(snapshot.period_of_report))
        .where(snapshot.security_id == security_id)
        .scalar_subquery()
    )


def lookup_query(
    key: str | BindParameter[str], *, cusip: bool, on: date | BindParameter[date] | None = None
) -> Select[Any]:
    """The security ``key`` names on ``on``, as one row, or none.

    Matched against, in order: a ticker it traded under on ``on``
    (``security_alias``); its current ticker; a ticker it traded under at any
    other time, the most recent first; and, with ``cusip``, its CUSIP. Within
    a tier, the security held most recently wins, then the newer row. See
    :mod:`app.api.routers.stocks` for why.

    :param key: As stored (:func:`~app.core.tickers.normalise_ticker`).
    :param cusip: Whether to try ``key`` as a CUSIP too. Only a nine-character
        key can be one, and comparing a longer one with ``char(9)`` would
        either fail or, cast, compare its first nine characters.
    :param on: The date being viewed. Today when ``None``.
    """
    alias = SecurityAlias
    day: Any = on if on is not None else func.current_date()
    # How recent a range is: an open one is the most recent of all.
    recency = func.coalesce(alias.valid_to, literal(date.max, Date))
    always = literal(date.max, Date)
    candidates = [
        select(
            alias.security_id.label("security_id"),
            literal_column("0", Integer).label("via"),
            recency.label("recency"),
        ).where(func.upper(alias.ticker) == key, valid_on(day, alias)),
        select(Security.id, literal_column("1", Integer), always).where(
            func.upper(Security.ticker) == key
        ),
        select(alias.security_id, literal_column("2", Integer), recency).where(
            func.upper(alias.ticker) == key
        ),
    ]
    if cusip:
        # Cast, or a parameter shared with the text comparisons above is typed
        # text, cusip is compared as text, and uq_security_cusip is no use.
        candidates.append(
            select(Security.id, literal_column("3", Integer), always).where(
                Security.cusip == cast(key, Security.cusip.type)
            )
        )
    matches = union_all(*candidates).subquery("matches")

    return (
        select(Security.id, Security.cusip, Security.ticker, Security.name, Security.delisted_by)
        .join(matches, matches.c.security_id == Security.id)
        .order_by(
            matches.c.via,
            matches.c.recency.desc(),
            _last_held(Security.id).desc().nulls_last(),
            Security.id.desc(),
        )
        .limit(1)
    )


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
