"""Field types shared by every response model.

One entry so far, and it is the one that would otherwise be got wrong
independently in each router.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

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
Money = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]

#: Share counts and principal amounts. Same treatment, same reason; a separate
#: name because the unit is not dollars and
#: :attr:`~app.db.models.holding.Holding.sshprnamt_type` says which it is.
Quantity = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="json")]
