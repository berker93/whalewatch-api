"""The one exception every parser in this package raises.

Its own module rather than a home in ``thirteen_f`` because ``form4`` raises it
too, and a shared exception that lives in the first module to have needed it is
an import cycle waiting for the second one.

Why a type of our own rather than ``ValueError``. A parse failure has a
destination: :attr:`~app.db.models.filing.Filing.parse_error`, the column that
makes "which filings are broken right now" a query instead of a grep. Something
has to catch the failure and write it there, and ``except ValueError`` around a
parser also catches every ``int()`` and ``strptime`` that our *own* code got
wrong — recording a bug in the loader as though the document were malformed, on
a row that then looks like EDGAR's fault forever.
"""

from __future__ import annotations


class FilingParseError(Exception):
    """A filing document did not contain what the form requires.

    Always carries :attr:`field` — the local element name that was missing or
    unreadable — because the failure is only actionable if you know where to
    look. "could not parse primary_doc.xml" sends someone to read the whole
    document; "periodOfReport: expected MM-DD-YYYY (got '2024-13-45')" sends
    them to one line of it.

    :param field: Local name of the offending element, spelled as EDGAR spells
        it (``tableValueTotal``, not ``table_value_total``) so it can be grepped
        for in the raw document.
    :param reason: What was expected, in the imperative — "is required",
        "expected an integer".
    :param value: The text actually found, when there was any. ``None``
        distinguishes an element that was absent from one that was unreadable,
        which are different bugs: a missing element usually means the wrong
        document was fetched, a malformed one means the parser is behind a
        schema change.
    """

    def __init__(self, *, field: str, reason: str, value: str | None = None) -> None:
        detail = f"{field}: {reason}"
        if value is not None:
            detail += f" (got {value!r})"
        super().__init__(detail)
        self.field = field
        self.reason = reason
        self.value = value
