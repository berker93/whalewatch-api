"""SEC's ticker-to-CIK map: which company files under which CIK, by its ticker.

Form 4s are filed under the issuer's CIK, and nothing else in the schema has
one: a ``security`` is a CUSIP with a ticker from OpenFIGI. This table is the
bridge. It is SEC's ``company_tickers.json``, about ten thousand listed
companies, each ticker with the CIK of the company that has it.

Used to bound the Form 4 crawl to issuers tracked investors hold
(:mod:`app.ingestion.insider_discovery`), and there for the insider loader to
link a transaction's issuer CIK back to a security.

Keyed on the ticker, as SEC spells it: a share class after a dash, ``BRK-B``,
where ``security.ticker`` has ``BRK.B``. A ticker belongs to one company at a
time, so a later import that gives it to another CIK moves it. A ticker
missing from a later file is kept, with the time it was last seen: SEC's file
lists only what is listed today, and a delisted company's CIK is still the
right one for its old filings.

Not foreign-keyed to ``security``, for ``sec_13f_security``'s reason: it
records what SEC publishes, whether or not anyone we track holds it.
"""

from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class SecCompanyTicker(Base):
    """One ticker, and the CIK of the company SEC last listed with it."""

    __tablename__ = "sec_company_ticker"

    ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    """As SEC spells it: upper case, ``BRK-B``."""

    cik: Mapped[str] = mapped_column(CHAR(10))
    """Zero-padded, as everywhere else. One company's classes share it:
    ``GOOGL`` and ``GOOG`` are both ``0001652044``."""

    name: Mapped[str] = mapped_column(Text)
    """SEC's ``title`` for the company, as written: "Apple Inc.", "BERKSHIRE
    HATHAWAY INC"."""

    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When the latest import that listed this ticker ran. Older than the
    newest import's for a ticker SEC no longer lists."""

    __table_args__ = (
        # Linking an issuer CIK from a Form 4 back to its tickers.
        Index("ix_sec_company_ticker_cik", "cik"),
        CheckConstraint("ticker <> ''", name="ticker_is_not_empty"),
    )
