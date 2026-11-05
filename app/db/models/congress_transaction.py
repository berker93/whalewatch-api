"""One trade a member of Congress reported, as read from their PTR's PDF.

A row per line of the PTR's transaction grid (:mod:`app.ingestion.congress.ptr`),
keyed ``(filing_id, line)``, written by ``ingest-house-ptrs`` and, from the table
of a Senate report filed online, ``ingest-senate-ptrs``
(:mod:`app.ingestion.congress.senate_ptr`):

- ``parsed``: read in full — from the text layer, exactly, or by OCR with
  confidence — or accepted by a person in the review queue.
- ``needs_review``: read in part, or by OCR without confidence. Kept with what
  could be read, and why not the rest (``problems``), for the admin review
  queue. Never dropped.
- ``placeholder``: the one row of a filing from which no line could be read —
  a scan with OCR not installed, handwriting, a grid that is not the form's,
  or a Senate paper filing, which eFD serves only as page images.
  Every field of the trade is null; the filing it belongs to carries the
  member, the date and ``document_url``, so the feed can still say "filing not
  machine-readable — view original" rather than leave the member out.
- ``rejected``: a person said it is not a trade (OCR read a heading, a stamp).
  Kept, out of the feed.

Reading a filing again replaces its rows, unless a person has reviewed one of
them: then the filing is left as it is, and the run says so.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.ingestion.congress.ptr import OWNERS, Extraction, RowStatus, TransactionType


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


PARSE_STATUS_CHECK: Final = _in("parse_status", tuple(status.value for status in RowStatus))
EXTRACTION_CHECK: Final = _in("extraction", tuple(kind.value for kind in Extraction))
TRANSACTION_TYPE_CHECK: Final = _in(
    "transaction_type", tuple(kind.value for kind in TransactionType)
)
OWNER_CHECK: Final = _in("owner", OWNERS)

#: What a ``parsed`` row must have: a trade in something, of some kind, on a
#: day, in a band.
PARSED_IS_COMPLETE: Final = (
    "parse_status <> 'parsed' OR (asset_name IS NOT NULL AND transaction_type IS NOT NULL "
    "AND transaction_date IS NOT NULL AND (amount_low IS NOT NULL OR amount_exact IS NOT NULL))"
)
#: And a placeholder nothing of one.
PLACEHOLDER_IS_EMPTY: Final = (
    "parse_status <> 'placeholder' OR (asset_name IS NULL AND transaction_type IS NULL "
    "AND transaction_date IS NULL AND amount_low IS NULL AND amount_exact IS NULL)"
)


class CongressTransaction(Base):
    """One line of one PTR, or the placeholder for a PTR none could be read from."""

    __tablename__ = "congress_transaction"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("congress_filing.id", ondelete="CASCADE")
    )
    line: Mapped[int] = mapped_column(SmallInteger)
    """1-based, in the order the PTR lists them; a Senate report's own ``#``.
    A placeholder is line 1."""
    page: Mapped[int | None] = mapped_column(SmallInteger)
    parse_status: Mapped[str] = mapped_column(Text)
    """One :class:`~app.ingestion.congress.ptr.RowStatus` value."""
    extraction: Mapped[str] = mapped_column(Text)
    """``text``, ``ocr``, ``html``; ``none`` on a placeholder no reader could read."""

    owner: Mapped[str | None] = mapped_column(Text)
    """``SP`` spouse, ``JT`` joint, ``DC`` dependent child; null for the member."""
    asset_name: Mapped[str | None] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(Text)
    """As the filer wrote it in parentheses; never on a scan."""
    asset_type: Mapped[str | None] = mapped_column(Text)
    """The House's two-letter code: ``ST``, ``OP``, ``GS``… A Senate row's
    words are mapped to it (:data:`~app.ingestion.congress.senate_ptr.ASSET_TYPES`)."""
    transaction_type: Mapped[str | None] = mapped_column(Text)
    transaction_date: Mapped[date | None] = mapped_column(Date)
    notification_date: Mapped[date | None] = mapped_column(Date)
    amount_low: Mapped[int | None] = mapped_column(BigInteger)
    amount_high: Mapped[int | None] = mapped_column(BigInteger)
    """Null with a low: an open band, "over $50,000,000"."""
    amount_exact: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    """A figure the filer wrote instead of a band ("$710.52"); never with one."""
    cap_gains_over_200: Mapped[bool | None] = mapped_column(Boolean)
    filing_status: Mapped[str | None] = mapped_column(Text)
    """``new`` or ``amended``, as the e-filed form says."""
    amended_id: Mapped[str | None] = mapped_column(Text)
    """On an amended line, the id of the transaction it amends."""
    subholding_of: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    """0 to 1, on an OCR read."""
    problems: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=sql_text("'{}'::text[]")
    )
    """Why a row is ``needs_review``, or why a placeholder is one."""
    raw_text: Mapped[str] = mapped_column(Text, server_default="")
    """The line as read, for the reviewer."""

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("filing_id", "line"),
        # The review queue: a short list, read by filing, newest first.
        Index(
            "ix_congress_transaction_needs_review",
            "filing_id",
            postgresql_where=sql_text("parse_status = 'needs_review'"),
        ),
        CheckConstraint(PARSE_STATUS_CHECK, name="parse_status_is_known"),
        CheckConstraint(EXTRACTION_CHECK, name="extraction_is_known"),
        CheckConstraint(TRANSACTION_TYPE_CHECK, name="transaction_type_is_known"),
        CheckConstraint(OWNER_CHECK, name="owner_is_known"),
        CheckConstraint(PARSED_IS_COMPLETE, name="parsed_is_complete"),
        CheckConstraint(PLACEHOLDER_IS_EMPTY, name="placeholder_is_empty"),
        CheckConstraint("amount_high IS NULL OR amount_high > amount_low", name="amount_is_a_band"),
        CheckConstraint(
            "amount_exact IS NULL OR (amount_low IS NULL AND amount_exact > 0)",
            name="amount_is_a_band_or_a_figure",
        ),
        CheckConstraint("line >= 1", name="line_is_positive"),
        CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_by IS NULL)", name="review_names_its_admin"
        ),
    )
