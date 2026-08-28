"""Loaders: the only code that writes parsed filings into the database.

One module per form family, each exposing a single ``load_*`` function that is
safe to call again on a filing it has already loaded. That property is the whole
reason this package is separate from the parsers: a parser is a pure function of
some bytes and is trivially re-runnable, while a write is only re-runnable if it
was designed to be.
"""

from app.ingestion.loaders.filing import LoadResult, load_filing

__all__ = ["LoadResult", "load_filing"]
