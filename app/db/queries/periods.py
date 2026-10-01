"""A handle on the ``filer_period`` view, for building queries against it.

The view is defined in migration ``0013``. Like
:data:`~app.db.queries.effective.EFFECTIVE_FILING`, this is a ``table()``
construct and deliberately *not* on ``Base.metadata``, where autogenerate would
take it for a table and draft a ``CREATE TABLE``.

A filer's period is one it *published*: one with rows in ``position_snapshot``.
That is narrower than "one it filed for" on purpose. A period withheld for a
suspect filing is not a period here, and neither is a quarter with nothing
loaded, so a question about "the filer's next period" steps over both. If it
did not, everything held the quarter before a withheld one would read as sold
in it.
"""

from typing import Final

from sqlalchemy import BigInteger, Boolean, Date, column, table

FILER_PERIOD: Final = table(
    "filer_period",
    column("filer_id", BigInteger),
    column("period_of_report", Date),
    column("suspect", Boolean),
)
"""Every ``(filer, period)`` published in ``position_snapshot``, once each, and
whether it was published with a suspect filing (only ever true after
``recompute --include-suspect``)."""
