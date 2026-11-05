"""Members of Congress, and which of them each filed name is.

``congress_member``
    Everyone who has sat in Congress since 2008, by Bioguide id, from the
    ``unitedstates/congress-legislators`` dataset
    (:mod:`app.ingestion.congress.legislators`), with their names and, from
    their latest term, chamber, party, state and district. Seeded by
    ``seed-congress-members``; a re-seed updates a member only where the
    dataset now says something different.
``congress_member_term``
    Every term each served: what "in office when they filed" is checked against.
    Replaced whole on each seed.
``congress_filed_name``
    A name as a chamber's index filed it — chamber, the state of a House
    filing's ``StateDst``, and the folded name
    (:func:`~app.ingestion.congress.member_match.name_key`) — and the member it
    is. Every ``congress_filing`` points at one. Matched once per name, not per
    filing: ``Hon. Rohit Khanna`` of CA files a dozen PTRs a year, and is
    reviewed once.
``congress_member_candidate``
    The members a filed name might be, for a person to choose from: the manual
    review queue. A run never accepts one.

Overrides
---------
A filed name matched by a person (``method = 'manual'``) is never matched again
by a run: not by re-reading an index, not by a re-seed. Re-reading an index can
change a filing's name, which moves the filing to another filed name, but the
override stays with the name it was made for. As ``resolution_source =
'manual'`` on a security (SEC-3).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.congress_filing import CHAMBER_CHECK
from app.ingestion.congress.member_match import AUTO_ACCEPT, AUTOMATIC, Method


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class NameStatus(StrEnum):
    MATCHED = "matched"
    """A member: by an exact or normalised match a run accepted, or by a person."""
    REVIEW = "review"
    """Candidates, none accepted: waiting for a person."""
    UNMATCHED = "unmatched"
    """No candidate, or a person rejected them all. The filings keep the name as
    filed, and are flagged."""


class CandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    """A person said the name is this member."""
    REJECTED = "rejected"
    """A person said it is not. Never suggested for the name again."""
    SUPERSEDED = "superseded"
    """Closed without a decision on it: the name was matched another way, or a
    later run no longer suggests it."""


NAME_STATUS_CHECK: Final = _in("status", tuple(status.value for status in NameStatus))
CANDIDATE_STATUS_CHECK: Final = _in("status", tuple(status.value for status in CandidateStatus))
#: A filed name is matched by exact, normalised or a person: never by trigram.
NAME_METHOD_CHECK: Final = "method IS NULL OR " + _in(
    "method", (*(method.value for method in AUTOMATIC), Method.MANUAL.value)
)
CANDIDATE_METHOD_CHECK: Final = _in(
    "method", tuple(method.value for method in Method if method is not Method.MANUAL)
)
#: A run accepts a match only at :data:`~app.ingestion.congress.member_match.AUTO_ACCEPT`.
AUTO_MATCH_CHECK: Final = f"method IS NULL OR method = 'manual' OR confidence >= {AUTO_ACCEPT}"


class CongressMember(Base):
    """One person who has sat in Congress since 2008."""

    __tablename__ = "congress_member"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    bioguide_id: Mapped[str] = mapped_column(Text, unique=True)
    """The Bioguide's id: ``P000197``. What the dataset, and everyone else, keys on."""

    full_name: Mapped[str] = mapped_column(Text)
    """The dataset's ``official_full`` (``Nancy Pelosi``), or the name put
    together where it has none."""
    first_name: Mapped[str] = mapped_column(Text)
    middle_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    suffix: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[str | None] = mapped_column(Text)
    """The name they go by, where it is not their first: ``Chuck`` for Charles."""
    other_last_names: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    """Last names they served under before."""

    chamber: Mapped[str] = mapped_column(Text)
    """Their latest term's: ``house`` or ``senate``."""
    state: Mapped[str] = mapped_column(Text)
    """Their latest term's, as a USPS code: ``CA``. Includes ``DC``, ``PR`` and
    the territories, whose delegates sit in the House."""
    district: Mapped[int | None] = mapped_column(SmallInteger)
    """Their latest term's, in the House: ``0`` at large. Null for a senator."""
    party: Mapped[str | None] = mapped_column(Text)
    """Their latest term's, as the dataset spells it: ``Democrat``, ``Republican``,
    ``Independent``."""
    current: Mapped[bool] = mapped_column(Boolean)
    """In the dataset's ``legislators-current``: in office, as it last said."""

    source_key: Mapped[str] = mapped_column(Text)
    """The raw-store key of the dataset file that last changed the row."""
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("bioguide_id ~ '^[A-Z][0-9]{6}$'", name="bioguide_id_is_an_id"),
        CheckConstraint(CHAMBER_CHECK, name="chamber_is_known"),
        CheckConstraint("state ~ '^[A-Z]{2}$'", name="state_is_a_code"),
        CheckConstraint(
            "chamber = 'house' OR district IS NULL", name="only_the_house_has_districts"
        ),
    )


class CongressMemberTerm(Base):
    """One term one member served."""

    __tablename__ = "congress_member_term"

    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("congress_member.id", ondelete="CASCADE"), primary_key=True
    )
    chamber: Mapped[str] = mapped_column(Text, primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    end_date: Mapped[date] = mapped_column(Date)
    """As the dataset has it: the end of the Congress, for a term still running."""
    state: Mapped[str] = mapped_column(Text)
    district: Mapped[int | None] = mapped_column(SmallInteger)
    party: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(CHAMBER_CHECK, name="chamber_is_known"),
        CheckConstraint("end_date >= start_date", name="ends_after_it_starts"),
        CheckConstraint("state ~ '^[A-Z]{2}$'", name="state_is_a_code"),
        CheckConstraint(
            "chamber = 'house' OR district IS NULL", name="only_the_house_has_districts"
        ),
    )


class CongressFiledName(Base):
    """A filer as a chamber's index names them, and who that is."""

    __tablename__ = "congress_filed_name"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    chamber: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    """The state of a House filing's ``StateDst``; null for the Senate, whose
    index gives none, and for a ``StateDst`` that is not one."""
    name_key: Mapped[str] = mapped_column(Text)
    """The name folded: lower case, no accents, punctuation, honorifics or suffix.
    ``thomas h kean``."""
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    """As the first filing under the key spelled them, for the queue to show."""

    status: Mapped[str] = mapped_column(Text)
    """One :class:`NameStatus` value."""
    member_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("congress_member.id", ondelete="RESTRICT")
    )
    method: Mapped[str | None] = mapped_column(Text)
    """How it was matched: ``exact``, ``normalised`` or ``manual``. Null unmatched."""
    confidence: Mapped[float | None] = mapped_column(Float)
    """The match's, 0 to 1 (1 for a person's); in review, the best candidate's."""
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the member or status last changed."""

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When a person last decided on it."""
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )
    review_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("chamber", "state", "name_key", postgresql_nulls_not_distinct=True),
        CheckConstraint(CHAMBER_CHECK, name="chamber_is_known"),
        CheckConstraint(NAME_STATUS_CHECK, name="status_is_known"),
        CheckConstraint(NAME_METHOD_CHECK, name="method_is_known"),
        CheckConstraint("name_key <> ''", name="name_key_is_not_empty"),
        CheckConstraint(
            "(status = 'matched') = (member_id IS NOT NULL)", name="matched_names_a_member"
        ),
        CheckConstraint("(member_id IS NULL) = (method IS NULL)", name="a_match_has_a_method"),
        CheckConstraint(AUTO_MATCH_CHECK, name="auto_match_is_confident"),
        CheckConstraint(
            "method <> 'manual' OR reviewed_at IS NOT NULL", name="manual_match_was_reviewed"
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_is_a_fraction"
        ),
        # The review queue, and the feed's flagged filers.
        Index(
            "ix_congress_filed_name_open",
            "status",
            postgresql_where=sql_text("status <> 'matched'"),
        ),
        Index("ix_congress_filed_name_member_id", "member_id"),
    )


class CongressMemberCandidate(Base):
    """One member a filed name might be."""

    __tablename__ = "congress_member_candidate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filed_name_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("congress_filed_name.id", ondelete="CASCADE")
    )
    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("congress_member.id", ondelete="CASCADE")
    )
    method: Mapped[str] = mapped_column(Text)
    """How it fit: ``exact`` or ``normalised`` (but not alone, or not in office),
    or ``trigram``."""
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, server_default=CandidateStatus.PENDING.value)
    """One :class:`CandidateStatus` value."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(CANDIDATE_STATUS_CHECK, name="status_is_known"),
        CheckConstraint(CANDIDATE_METHOD_CHECK, name="method_is_known"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_is_a_fraction"),
        CheckConstraint(
            "(status = 'pending') = (closed_at IS NULL)", name="closed_when_not_pending"
        ),
        # One pending suggestion per pair: a second run adds nothing it
        # suggested already.
        Index(
            "uq_congress_member_candidate_pending",
            "filed_name_id",
            "member_id",
            unique=True,
            postgresql_where=sql_text("status = 'pending'"),
        ),
        Index("ix_congress_member_candidate_filed_name_id", "filed_name_id"),
    )
