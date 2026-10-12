"""Fuzzy matches waiting for a person: the manual review queue.

When neither OpenFIGI nor the SEC's 13(f) list gives a security a ticker,
``resolve-cusips`` looks for a security that has one under a similar name —
trigram similarity, the ``pg_trgm`` the search already uses — and writes each
candidate here. It never applies one. A similar name is how ``ALPHABET INC``
class C comes to look like class A, and how an old CUSIP comes to look like
the new one after a reorganisation, sometimes rightly and sometimes not; which
is a judgement, and a wrong ticker is worse than none.

A person takes a suggestion with ``resolve-cusip CUSIP --ticker T``, which
closes every pending row for that security: ``accepted`` if T is what a row
suggested, ``superseded`` if not. A security that later resolves another way
has its rows closed ``superseded`` the same way.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, Text, func
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class ReviewStatus(StrEnum):
    """Text with a ``CHECK``, by the rule in :mod:`app.db.models.enums`."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    """``resolve-cusip`` set the ticker this row suggested."""
    SUPERSEDED = "superseded"
    """The security was resolved some other way: another ticker by hand, or
    OpenFIGI on a later run."""


REVIEW_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{status.value}'" for status in ReviewStatus)
)


class ManualReview(Base):
    """One suggested ticker for one unresolved security."""

    __tablename__ = "manual_review"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    security_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )
    """The unresolved security."""

    suggested_security_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )
    """The resolved security whose name matched."""

    suggested_ticker: Mapped[str] = mapped_column(Text)
    """Its ticker when the match was made, so the queue still reads the same if
    that security is later resolved differently."""

    matched_name: Mapped[str] = mapped_column(Text)
    """The unresolved security's name as searched with."""

    suggested_name: Mapped[str] = mapped_column(Text)
    """The name it matched."""

    similarity: Mapped[float] = mapped_column(Float)
    """``pg_trgm``'s ``similarity()`` of the two names, 0 to 1."""

    status: Mapped[str] = mapped_column(Text, server_default=ReviewStatus.PENDING.value)
    """One :class:`ReviewStatus` value."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When it stopped being pending. Null while it is."""

    __table_args__ = (
        CheckConstraint(REVIEW_STATUS_CHECK, name="status_is_known"),
        CheckConstraint(
            "(status = 'pending') = (closed_at IS NULL)", name="closed_when_not_pending"
        ),
        CheckConstraint("security_id <> suggested_security_id", name="not_a_suggestion_of_itself"),
        # One pending suggestion per pair, so a second run adds nothing it
        # suggested already. Closed rows are kept and do not count.
        Index(
            "uq_manual_review_pending",
            "security_id",
            "suggested_security_id",
            unique=True,
            postgresql_where=sql_text("status = 'pending'"),
        ),
    )
