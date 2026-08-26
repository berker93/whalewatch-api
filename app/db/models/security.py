"""One instrument, identified by one CUSIP.

Note the noun. A *security* is not a company: a company with common stock and
two classes of convertible is one company and three securities, with three
CUSIPs, three sets of share counts and — usually — one ticker between them.
Merging the two concepts is what makes share totals wrong, so the issuer half
lives in its own table (Epic 3, with the ``/stocks`` search that needs it) and
this one holds only what a 13F information table actually gives us.
"""

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Security(Base):
    """A CUSIP, everything we have managed to resolve about it, and nothing else."""

    __tablename__ = "security"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    cusip: Mapped[str] = mapped_column(CHAR(9), unique=True)
    """Nine characters, the natural key, and the only identifier a 13F gives us.

    Unique because that is what lets the loader resolve a holding line to a
    security with an upsert instead of a lookup-then-insert race — two workers
    parsing two filings that both hold Apple will both try to create it.

    Worth knowing when reading anything downstream: a CUSIP is not permanent.
    They change on corporate actions, and the same nine characters can describe
    different instruments across a long enough span. Which is why every join
    inside the database is on :attr:`id` and never on this column — the CUSIP is
    how a row is *found* once, at parse time, not how it is referred to.
    """

    name: Mapped[str | None] = mapped_column(Text)
    """``nameOfIssuer`` as it appeared on the filing that first created this row.

    Filer-supplied, inconsistent, and frequently abbreviated to fit a column
    ("BERKSHIRE HATHAWAY INC DEL"). It exists so that an unresolved security is
    still displayable, and it is superseded by the issuer's real name as soon as
    there is an issuer table to hold one.
    """

    ticker: Mapped[str | None] = mapped_column(Text)
    """Null when unresolved, and **it stays nullable**.

    CUSIP-to-ticker resolution fails, routinely and permanently, for delisted
    names and obscure instruments. A ``NOT NULL`` here would force the loader
    into the only two options it must never take: invent a ticker, or drop the
    holding. It drops neither — an unresolved security still gets a row, is still
    held, and still counts in dollar totals. Unresolved is a normal state, not a
    failure, and code that treats it as one will silently shrink portfolios.
    """

    figi: Mapped[str | None] = mapped_column(CHAR(12))
    """OpenFIGI's identifier, twelve characters. Unlike a CUSIP, it is stable."""

    resolution_source: Mapped[str | None] = mapped_column(Text)
    """Where :attr:`ticker` and :attr:`figi` came from.

    Exists because the newer 13F information table may itself carry a FIGI
    column, and when it does that is better evidence than an OpenFIGI lookup.
    Recording the provenance is what makes a bad mapping *findable* later: when
    one CUSIP turns out to point at the wrong company, the question is "which
    other rows came from the same source" and without this column the answer is
    "re-resolve all of them".
    """

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the resolution above was performed.

    Null is the retry queue: the enrichment job looks for securities with no
    ``resolved_at``, and a newly listed name that missed last month resolves
    next month without anyone intervening.
    """

    __table_args__ = (
        # Text-plus-CHECK rather than a native enum, per the reasoning in
        # app/db/models/enums.py: this set is ours, and it grows whenever we
        # find another place a mapping can come from. A CHECK is one ALTER to
        # widen; an enum value cannot be removed again at all.
        CheckConstraint(
            "resolution_source IS NULL"
            " OR resolution_source IN ('openfigi', '13f_column', 'manual')",
            name="resolution_source_is_known",
        ),
    )
