"""A member of Congress's Periodic Transaction Report, as the chamber's index lists it.

The STOCK Act filing a member makes within 45 days of a trade. One row per
filing, from the index each chamber publishes — the House Clerk's yearly ZIP
(:mod:`app.ingestion.congress.house_index`), Senate eFD's search
(:mod:`app.ingestion.congress.senate_ptr`) — before anything has read the
filing itself: who filed, for which office, when, and the chamber's id for
the document. The trades in it are a later step's.

Keyed naturally on ``(chamber, doc_id)``: a doc id is the chamber's own, and
unique within it (the House's are digits, the Senate's UUIDs). Reading an
index again updates a row only where the index now says something different —
the Clerk correcting a name, or a date — so a re-run that finds nothing new
writes nothing, ``updated_at`` included.

Its PDF is read later, by ``ingest-house-ptrs``, into ``congress_transaction``
rows (:mod:`app.db.models.congress_transaction`); a Senate report's page, by
``ingest-senate-ptrs``. The columns from ``document_url`` down say how that
went. A filing whose ``parser_version`` is null has not been read yet.

Not linked to a member table: there is none yet, and the index names people
as they filed (``Hon.``, a middle initial or not), not as a roster would.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: The chambers, as ``chamber`` spells them.
CHAMBERS = ("house", "senate")

#: What a filing is, across chambers. Only PTRs so far: annual reports are
#: another shape, and out of scope.
REPORT_TYPES = ("ptr",)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


CHAMBER_CHECK = _in("chamber", CHAMBERS)
REPORT_TYPE_CHECK = _in("report_type", REPORT_TYPES)


class CongressFiling(Base):
    """One PTR, by the chamber's doc id."""

    __tablename__ = "congress_filing"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    chamber: Mapped[str] = mapped_column(Text)
    """``house`` or ``senate``."""

    doc_id: Mapped[str] = mapped_column(Text)
    """The chamber's id for the document: the House's ``DocID``, ``20033725``,
    which also names its PDF; the UUID in eFD's link to a Senate report,
    lower-cased."""

    report_type: Mapped[str] = mapped_column(Text)
    """``ptr``. The House's ``FilingType`` ``P``; eFD's ``report_types`` 11."""

    last_name: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    """As filed, with any middle name or initial: ``Robert B.``."""
    prefix: Mapped[str | None] = mapped_column(Text)
    """``Hon.``, ``Mr.``, ``Dr.``; usually ``Hon.`` or nothing."""
    suffix: Mapped[str | None] = mapped_column(Text)
    """``Jr.``, ``III``, as filed (``Jr`` and ``Jr.`` both occur)."""

    office: Mapped[str | None] = mapped_column(Text)
    """The House's ``StateDst``: state and district, ``CA11``, ``AK00`` at large.
    For the Senate, the filer's role as eFD gives it: ``Senator``, ``Former
    Senator``."""

    filing_date: Mapped[date] = mapped_column(Date)
    """When the chamber received it."""

    year: Mapped[int] = mapped_column(SmallInteger)
    """The year the index files it under, which can be years before
    :attr:`filing_date` for a late filing. The Senate has no yearly index: its
    filing date's year."""

    report_date: Mapped[date | None] = mapped_column(Date)
    """Senate only: the day eFD's title says the report is "for" — on an
    amendment, the original's, which is how the two are matched."""
    amendment: Mapped[int | None] = mapped_column(SmallInteger)
    """Senate only: ``1`` for "(Amendment 1)"; null on an original. An
    amendment restates the whole report."""

    source_key: Mapped[str] = mapped_column(Text)
    """The raw-store key of the index that first listed it: for the Senate, the
    search page."""

    document_url: Mapped[str | None] = mapped_column(Text)
    """Where the chamber serves the filing itself: the "view original" link."""
    document_key: Mapped[str | None] = mapped_column(Text)
    """The raw-store key of its PDF, or its eFD page, once fetched. Null for a
    Senate paper filing, which is linked, never fetched."""
    pages: Mapped[int | None] = mapped_column(SmallInteger)
    extraction: Mapped[str | None] = mapped_column(Text)
    """How its rows were read: ``text``, ``ocr``, ``html`` (a Senate report
    filed online), or ``none`` (a scan with no OCR installed when it was read,
    or a Senate paper filing)."""
    parser_version: Mapped[int | None] = mapped_column(SmallInteger)
    """The :data:`~app.ingestion.congress.ptr.PARSER_VERSION` that read it;
    null until read."""
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parse_error: Mapped[str | None] = mapped_column(Text)
    """Why no row could be read from it — it then has a placeholder row — or
    why it could not be read at all yet (no PDF at the Clerk's)."""

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    """When a re-read last changed something; :attr:`first_seen_at` until then."""

    __table_args__ = (
        UniqueConstraint("chamber", "doc_id"),
        # The newest filings first, per chamber: what a feed reads.
        Index("ix_congress_filing_chamber_filing_date", "chamber", "filing_date"),
        CheckConstraint(CHAMBER_CHECK, name="chamber_is_known"),
        CheckConstraint(REPORT_TYPE_CHECK, name="report_type_is_known"),
        CheckConstraint("doc_id <> ''", name="doc_id_is_not_empty"),
        CheckConstraint(
            "extraction IS NULL OR extraction IN ('text', 'ocr', 'html', 'none')",
            name="extraction_is_known",
        ),
        CheckConstraint("amendment IS NULL OR amendment >= 1", name="amendment_is_positive"),
    )
