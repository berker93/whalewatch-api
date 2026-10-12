"""One instrument, identified by one CUSIP.

Note the noun. A *security* is not a company: a company with common stock and
two classes of convertible is one company and three securities, with three
CUSIPs, three sets of share counts and — usually — one ticker between them.
Merging the two concepts is what makes share totals wrong, so the issuer half
lives in its own table (Epic 3, with the ``/stocks`` search that needs it) and
this one holds only what a 13F information table actually gives us.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class ResolutionSource(StrEnum):
    """Values of :attr:`Security.resolution_source`; see there. Text with a
    ``CHECK``, by the rule in :mod:`app.db.models.enums`: the set is ours."""

    OPENFIGI = "openfigi"
    SEC_13F_LIST = "13f_list"
    MANUAL = "manual"
    THIRTEEN_F_COLUMN = "13f_column"


RESOLUTION_SOURCE_CHECK = "resolution_source IS NULL OR resolution_source IN ({})".format(
    ", ".join(f"'{source.value}'" for source in ResolutionSource)
)


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
    """The best name we have for the issuer, and the one the API shows.

    OpenFIGI's when :attr:`resolution_source` is ``openfigi``, the SEC's 13(f)
    list's when it is ``13f_list``, and otherwise :attr:`filed_name`. Written by
    ``resolve-cusips`` (:mod:`app.ingestion.enrichment.resolution`), never by
    the loader, which creates the row with :attr:`filed_name` here too and does
    nothing to it after. Null only when the filing left ``nameOfIssuer`` blank
    and nothing has named the CUSIP since: the API then shows the CUSIP alone.
    """

    filed_name: Mapped[str | None] = mapped_column(Text)
    """``nameOfIssuer`` as it appeared on the filing that first created this row.

    Filer-supplied, inconsistent, and frequently abbreviated to fit a column
    ("BERKSHIRE HATHAWAY INC DEL"), sometimes stale ("FACEBOOK INC") and
    sometimes a note to self ("Full Truck Alliance - converted at IPO"). Kept
    because nothing else does: :attr:`name` is overwritten by resolution, and
    this is what it goes back to when a re-run finds no better source.
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
    """Which source won: where :attr:`ticker`, :attr:`figi` and :attr:`name` came from.

    ``openfigi``
        A US equity listing: ticker, FIGI and name.
    ``13f_list``
        The SEC's 13(f) list named it. A name and nothing else: no ticker.
    ``manual``
        ``resolve-cusip``: a person set the ticker, and :attr:`resolution_note`
        says why. Nothing automatic overwrites it, ``--force`` included.
    ``13f_column``
        Reserved for a FIGI the information table carries itself. Unused.
    ``NULL``
        Nothing has named it beyond the filing.

    Exists because the newer 13F information table may itself carry a FIGI
    column, and when it does that is better evidence than an OpenFIGI lookup.
    Recording the provenance is what makes a bad mapping *findable* later: when
    one CUSIP turns out to point at the wrong company, the question is "which
    other rows came from the same source" and without this column the answer is
    "re-resolve all of them".
    """

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When :attr:`resolution_source` was last set. Null when it is.

    The retry queue is a null :attr:`ticker`, not this: a security the 13(f)
    list named has a ``resolved_at`` and is still asked about on every run, so a
    newly listed name that missed last month resolves next month without anyone
    intervening.
    """

    resolution_note: Mapped[str | None] = mapped_column(Text)
    """Why a person resolved it as they did: ``resolve-cusip --note``. Null on
    every other row."""

    __table_args__ = (
        # Text-plus-CHECK rather than a native enum, per the reasoning in
        # app/db/models/enums.py: this set is ours, and it grows whenever we
        # find another place a mapping can come from. A CHECK is one ALTER to
        # widen; an enum value cannot be removed again at all.
        CheckConstraint(RESOLUTION_SOURCE_CHECK, name="resolution_source_is_known"),
        # A ticker says where it came from. The reverse does not hold: the
        # 13(f) list wins with a name alone.
        CheckConstraint(
            "ticker IS NULL OR resolution_source IS NOT NULL",
            name="a_ticker_has_a_source",
        ),
        # /stocks?q= searches names with ILIKE '%...%', which no B-tree can
        # serve. Trigrams can (0017): a bitmap index scan instead of reading
        # every security. Here until the issuer table it belongs on exists.
        Index(
            "ix_security_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # resolve-cusips' fuzzy match compares filed names as well as current
        # ones (0023): a filer's "HONEYWELL INTL INC" is the SEC's spelling,
        # and OpenFIGI's "HONEYWELL INTERNATIONAL INC" is only 0.54 similar.
        Index(
            "ix_security_filed_name_trgm",
            "filed_name",
            postgresql_using="gin",
            postgresql_ops={"filed_name": "gin_trgm_ops"},
        ),
        # /v1/search matches tickers by trigram as well as names (0020). Without
        # it, the search's OR over both columns is a sequential scan.
        Index(
            "ix_security_ticker_trgm",
            "ticker",
            postgresql_using="gin",
            postgresql_ops={"ticker": "gin_trgm_ops"},
        ),
        # /stocks/{ticker} ignores case, so it looks up upper(ticker), and its
        # 404 suggests tickers by prefix (0018). text_pattern_ops for the
        # prefix, as on security_alias.
        Index(
            "ix_security_upper_ticker",
            func.upper(ticker).label("upper_ticker"),
            postgresql_ops={"upper_ticker": "text_pattern_ops"},
        ),
    )
