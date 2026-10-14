"""Loaders: the only code that writes parsed documents into the database.

One module per form family, each exposing a single ``load_*`` function that is
safe to call again on a filing it has already loaded. That property is the whole
reason this package is separate from the parsers: a parser is a pure function of
some bytes and is trivially re-runnable, while a write is only re-runnable if it
was designed to be.
"""

from app.ingestion.loaders.filing import LoadResult, load_filing
from app.ingestion.loaders.insider import (
    AmendmentLink,
    Form4LoadResult,
    SecurityLink,
    load_form4,
)
from app.ingestion.loaders.thirteen_f_list import AdditionsCheck, check_additions, load_13f_list

__all__ = [
    "AdditionsCheck",
    "AmendmentLink",
    "Form4LoadResult",
    "LoadResult",
    "SecurityLink",
    "check_additions",
    "load_13f_list",
    "load_filing",
    "load_form4",
]
