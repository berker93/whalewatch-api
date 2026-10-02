"""Keyset pagination, and the cursor that carries a page's position.

Every collection is paginated by *where the last page ended*, never by how many
rows came before it. ``OFFSET 10000`` makes Postgres produce and throw away ten
thousand rows to return fifty, so the deep pages of an index fund's holdings
cost more the further a client walks. And an offset counts rows, so a row
inserted or deleted ahead of it while a client is walking shifts every later
page by one: a repeated row, or a skipped one, and nothing reports either.

A keyset page instead asks for the rows after the last one seen::

    WHERE (value_usd, security_id) < (:last_value, :last_id)
    ORDER BY value_usd DESC, security_id DESC

which an index on the sort columns answers by seeking straight to the
position, at the same cost on page 200 as on page 1. A row added mid-walk
lands either before the position (already passed; not seen) or after it
(seen once, in order), and every row that was there all along is returned
exactly once.

That guarantee needs the ordering to be *total*: the last :class:`SortKey`
must be unique, like a primary key, or two rows tied on every key straddle a
page boundary and one of them is skipped. And every key must be ``NOT NULL``,
because a row comparison against a null is null, not false, and drops the row.

The cursor
----------
The position is the last row's sort values, sent to the client as an opaque
token and returned as ``?cursor=``. It is base64url of a small JSON object
carrying a version, the name of the :class:`Keyset` it was made for, and the
values. The client is never meant to read it. The version is there so that a
change to the format is a clean 400 telling the client to start again rather
than a misread position; the keyset name so that a cursor made under one sort
order cannot be replayed under another.

It is not signed. Signing would stop a client forging a position, but a forged
position is only a ``WHERE`` the client could have asked for with a filter: it
reaches no row it could not already see. What does need stopping is a value of
the wrong type reaching Postgres as a 500, and that is what decoding is strict
about. Every value is checked against the type its key declares before any SQL
is built.
"""

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, Row, Select, and_, literal, or_, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.envelope import Page

#: Bumped whenever the encoding changes. A cursor of any other version is
#: refused, not interpreted: the client loses its place and starts again,
#: rather than resuming somewhere it did not leave off.
CURSOR_VERSION: Final = 1

DEFAULT_LIMIT: Final = 50
#: The most rows a page can be asked for. Above this a request is refused
#: rather than quietly cut down, so that a client counting rows to detect the
#: last page cannot be fooled by one that came back short.
MAX_LIMIT: Final = 200

#: Far longer than any real cursor — a few keys' worth of values — and short
#: enough that decoding one is never a cost worth worrying about.
_MAX_CURSOR_LENGTH: Final = 1024

#: The value types a key can hold, as they travel in the cursor's JSON. A
#: ``Decimal`` travels as a string, for the reason it is a string everywhere in
#: this API (see :data:`app.api.schemas.types.Money`); dates and datetimes as
#: ISO 8601.
KeyType = type[Decimal] | type[int] | type[str] | type[date] | type[datetime]


class InvalidCursorError(HTTPException):
    """A ``?cursor=`` that this endpoint cannot resume from. Always a 400.

    An ``HTTPException`` so that FastAPI renders it as one with no handler to
    register, and raised as itself so the reason stays specific.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cursor: {reason}. Start again from the first page, without ?cursor=.",
        )


@dataclass(frozen=True, slots=True)
class SortKey:
    """One column of a keyset ordering."""

    name: str
    """The name the value is read back from on each result row, so the
    paginated statement must select it under exactly this name."""
    expression: ColumnElement[Any]
    """What is ordered on and compared against."""
    type: KeyType
    """The Python type the column's values come back as."""
    descending: bool = False


@dataclass(frozen=True, slots=True)
class Keyset:
    """A total ordering for one collection, and the comparisons that page it."""

    name: str
    """Written into every cursor this keyset makes, and checked on the way back
    in. One per endpoint and sort, e.g. ``"investor-holdings.value"``."""
    keys: tuple[SortKey, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError(f"keyset {self.name!r} has no keys")

    def order_by(self) -> list[ColumnElement[Any]]:
        return [key.expression.desc() if key.descending else key.expression for key in self.keys]

    def after(self, values: Sequence[object]) -> ColumnElement[bool]:
        """Rows that sort strictly after the row whose keys were ``values``.

        When every key runs the same way this is one row comparison,
        ``(a, b) < (:a, :b)``, which Postgres can answer with a single seek on
        an index over ``(a, b)``. Mixed directions cannot be written as one
        comparison, so they expand to the equivalent::

            a > :a OR (a = :a AND b < :b)

        which is correct, and can still use an index on ``a``.
        """
        directions = {key.descending for key in self.keys}
        if len(directions) == 1:
            columns = tuple_(*(key.expression for key in self.keys))
            # Typed from the columns, so asyncpg binds numeric as numeric
            # rather than leaving Postgres to guess from a bare parameter.
            position = tuple_(
                *(literal(v, k.expression.type) for k, v in zip(self.keys, values, strict=True))
            )
            return columns < position if directions.pop() else columns > position

        clauses = []
        for i, key in enumerate(self.keys):
            ties = [k.expression == v for k, v in zip(self.keys[:i], values[:i], strict=True)]
            beyond = key.expression < values[i] if key.descending else key.expression > values[i]
            clauses.append(and_(*ties, beyond))
        return or_(*clauses)

    def position_of(self, row: Row[Any]) -> list[object]:
        """``row``'s values for each key, checked, in key order."""
        mapping = row._mapping
        values: list[object] = []
        for key in self.keys:
            if key.name not in mapping:
                raise KeyError(
                    f"keyset {self.name!r}: the statement does not select {key.name!r}, "
                    "so a page's position cannot be read off its last row"
                )
            value = mapping[key.name]
            # Errors in our code, not the client's: they surface as a 500,
            # which is right, on the first page that would have hidden them.
            if value is None:
                raise ValueError(f"keyset {self.name!r}: {key.name} is null; keys must be NOT NULL")
            if not _is_a(value, key.type):
                raise TypeError(
                    f"keyset {self.name!r}: {key.name} is {type(value).__name__}, "
                    f"declared {key.type.__name__}"
                )
            values.append(value)
        return values


@dataclass(frozen=True, slots=True)
class Cursor:
    """A cursor that has been decoded but not yet checked against a keyset.

    The values are as they came out of JSON. :func:`paginate` types them, since
    only the keyset knows what they should be.
    """

    keyset: str
    values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class PageParams:
    """``?limit=`` and ``?cursor=``, validated. See :func:`app.api.deps.get_page_params`."""

    limit: int = DEFAULT_LIMIT
    cursor: Cursor | None = None


def encode_cursor(keyset: Keyset, values: Sequence[object]) -> str:
    """The token a client sends back to resume after the row with ``values``."""
    payload = {"v": CURSOR_VERSION, "k": keyset.name, "a": [_to_json(v) for v in values]}
    # Compact separators and no padding: it goes in a query string, and
    # nothing reading it needs either.
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(token: str) -> Cursor:
    """Unwrap a client's cursor, refusing anything this service did not make.

    :raises InvalidCursorError: Not base64url, not our JSON, or another version.
    """
    if len(token) > _MAX_CURSOR_LENGTH:
        raise InvalidCursorError("too long")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError):
        # json.JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        raise InvalidCursorError("not a cursor this API issued") from None

    if not isinstance(payload, dict) or set(payload) != {"v", "k", "a"}:
        raise InvalidCursorError("not a cursor this API issued")
    if payload["v"] != CURSOR_VERSION:
        raise InvalidCursorError("issued by a different version of this API")
    if not isinstance(payload["k"], str) or not isinstance(payload["a"], list):
        raise InvalidCursorError("not a cursor this API issued")
    return Cursor(keyset=payload["k"], values=tuple(payload["a"]))


def position(cursor: Cursor, keyset: Keyset) -> list[object]:
    """The cursor's values, typed for ``keyset``'s keys.

    :raises InvalidCursorError: Made for another keyset, or a value is not
        the type its key holds.
    """
    if cursor.keyset != keyset.name:
        raise InvalidCursorError("issued for a different listing or sort order")
    if len(cursor.values) != len(keyset.keys):
        raise InvalidCursorError("not a cursor this API issued")
    return [_from_json(raw, key) for raw, key in zip(cursor.values, keyset.keys, strict=True)]


async def paginate(
    session: AsyncSession,
    statement: Select[Any],
    keyset: Keyset,
    params: PageParams,
) -> tuple[list[Row[Any]], Page]:
    """One page of ``statement``'s rows, in ``keyset`` order, and the next cursor.

    ``statement`` must have no ``ORDER BY`` or ``LIMIT`` of its own: the keyset
    supplies both. It must select every key under the key's name. Its keys
    are compared in ``WHERE``, so they must be row-level expressions; to page
    an aggregate, page a subquery that has already aggregated.

    One row more than the page is fetched, so that whether there is a next
    page is known without a ``COUNT``. A last page that happens to be exactly
    full therefore says so, rather than handing out a cursor to an empty page.
    """
    if params.cursor is not None:
        statement = statement.where(keyset.after(position(params.cursor, keyset)))
    statement = statement.order_by(*keyset.order_by()).limit(params.limit + 1)

    rows = list(await session.execute(statement))
    page = rows[: params.limit]
    next_cursor = None
    if len(rows) > params.limit:
        next_cursor = encode_cursor(keyset, keyset.position_of(page[-1]))
    return page, Page(limit=params.limit, next_cursor=next_cursor)


def _is_a(value: object, kind: KeyType) -> bool:
    # bool is an int and a datetime is a date, and neither is the other here.
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is date:
        return isinstance(value, date) and not isinstance(value, datetime)
    return isinstance(value, kind)


def _to_json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):  # datetime included
        return value.isoformat()
    return value


def _from_json(raw: object, key: SortKey) -> object:
    """``raw`` as ``key.type``, or a 400 naming nothing a client should rely on."""
    try:
        if key.type is Decimal and isinstance(raw, str):
            value = Decimal(raw)
            # NaN and Infinity parse, and are not positions in any column here.
            if value.is_finite():
                return value
        elif key.type in (int, str) and _is_a(raw, key.type):
            return raw
        elif key.type is date and isinstance(raw, str):
            return date.fromisoformat(raw)
        elif key.type is datetime and isinstance(raw, str):
            return datetime.fromisoformat(raw)
    except (InvalidOperation, ValueError):
        pass
    raise InvalidCursorError("not a cursor this API issued")
