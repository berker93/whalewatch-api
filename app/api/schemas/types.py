"""Field types shared by every response model.

Each is one that would otherwise be got wrong independently in each router.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Final

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from app.core.periods import parse_period

#: A ``numeric`` column, rendered in JSON as a **string**.
#:
#: Every quantity in this schema is ``numeric`` in Postgres and ``Decimal`` in
#: Python, for the reasons written down on :class:`~app.db.models.holding.Holding`
#: — these columns are summed, ranked and compared for equality. Serialising one
#: as a JSON number undoes all of that at the last possible moment: JSON numbers
#: are IEEE 754 doubles, and a nine-figure share count with four decimal places
#: has more significant digits than a double carries. The value that comes back
#: out is then not the value in the database, and nothing in between reports an
#: error.
#:
#: A string round-trips exactly, and any client that wants arithmetic has to
#: parse it deliberately — into its own decimal type, which is the decision we
#: want it making.
#:
#: Pydantic 2 happens to serialise ``Decimal`` this way already. It is spelled
#: out anyway: the default is a default, this is a wire contract, and the pattern
#: constraint Pydantic infers for the implicit case makes the OpenAPI schema
#: harder to read than the explicit one.
#:
#: ``format: decimal`` is what says so in the schema. A generated TypeScript type
#: can only say ``string``; the format reaches its doc comment, which is where a
#: frontend reader learns this is a number to parse with a decimal library, not
#: with ``Number()``.
_DECIMAL_STRING = (
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "decimal"}, mode="serialization"),
)

Money = Annotated[Decimal, *_DECIMAL_STRING]

#: Share counts and principal amounts. Same treatment, same reason; a separate
#: name because the unit is not dollars and
#: :attr:`~app.db.models.holding.Holding.sshprnamt_type` says which it is.
Quantity = Annotated[Decimal, *_DECIMAL_STRING]

#: A percentage, ``0`` to ``100``, as a string. ``numeric`` for the same
#: reason, with six decimal places (see
#: :data:`~app.db.models.position_snapshot.WEIGHT_PCT`).
Percent = Annotated[Decimal, *_DECIMAL_STRING]


def _period(value: object) -> date:
    if isinstance(value, date):  # a default, never a query string
        return value
    if not isinstance(value, str):
        raise ValueError("a period is a string: 2026Q1 or 2026-03-31")
    return parse_period(value)


#: A quarter, as a client sends it: ``2026Q1`` or ``2026-03-31``, parsed to
#: the quarter-end ``date`` by :func:`~app.core.periods.parse_period`. Use it
#: for every ``?period=``-like parameter, so that every endpoint accepts the
#: same spellings and refuses the same mistakes with the same 422.
#:
#: Plain rather than ``Before``: a ``date`` field would go on to accept
#: ``20260331`` and a Unix timestamp, which are not periods. The schema says
#: string, since that is what goes in the query.
Period = Annotated[
    date,
    PlainValidator(_period),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^\d{4}([Qq]\d|-\d{2}-\d{2})$",
            "examples": ["2026Q1", "2026-03-31"],
        }
    ),
]


# --- a security's tickers -------------------------------------------------------
#
# Every model that names a security carries the same three: the ticker as of
# the period the row is for, the current one beside it, and whether it is
# still listed. Defined once so they read the same everywhere.

TICKER_THEN: Final = (
    "The ticker it traded under in the period this row is for: FB for Meta in "
    "2021. Where there is no period, the current one. Null while the CUSIP is "
    "unresolved, and for good for some."
)
CURRENT_TICKER: Final = (
    "The ticker it trades under now, or the last it had if it is no longer listed. "
    "Differs from `ticker` after a rename (META beside FB), and is what to link "
    "`/v1/stocks/{ticker}` by."
)
DELISTED_BY: Final = (
    "Set when it is no longer listed: the first quarter end by which it had left "
    "the SEC's 13(f) list, so a bound rather than the day. Null while listed, and "
    "for a security no list has had (an OTC ADR, a bond), whose listing is unknown."
)
