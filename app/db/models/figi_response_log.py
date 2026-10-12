"""Every answer OpenFIGI has given us, one row per CUSIP asked about.

OpenFIGI is quota-limited, and a 3,000-CUSIP backfill is the better part of an
hour unkeyed. Paying for the same answer twice — because a run was interrupted,
or because resolution logic changed and wants re-running — would be the
expensive way to find out nothing new. So the client writes each job's response
here, verbatim, before doing anything else with it, and on the next call reads
the latest answer per CUSIP from here instead of asking again.

Append-only. A CUSIP asked about twice has two rows, and the newer one wins;
the older is kept, because "what did OpenFIGI say about this CUSIP in March?"
is the question a bad mapping eventually raises.

Not foreign-keyed to ``security``: it records what an external API said about
nine characters, whether or not we hold anything under them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, Index, Text, desc, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class FIGIOutcome(StrEnum):
    """What one mapping job came back as. Text with a ``CHECK``, by the rule in
    :mod:`app.db.models.enums`: the set is ours.

    ``found``
        A ``data`` array of one or more instruments. More than one is common —
        a CUSIP maps to a listing per exchange — and all of them are kept.
    ``not_found``
        A ``warning``: OpenFIGI has no instrument for this CUSIP. A normal
        answer, and as worth keeping as a match.
    ``error``
        An ``error`` for this job alone (the rest of the request succeeded).
        Logged like any answer, but never reused: it says nothing about the
        CUSIP, so the next call asks again.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


OUTCOME_CHECK = "outcome IN ({})".format(", ".join(f"'{outcome.value}'" for outcome in FIGIOutcome))


class FigiResponseLog(Base):
    """One OpenFIGI mapping job and the answer it got."""

    __tablename__ = "figi_response_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    cusip: Mapped[str] = mapped_column(CHAR(9))
    """The ``idValue`` the job asked about."""

    outcome: Mapped[str] = mapped_column(Text)
    """One :class:`FIGIOutcome` value, classified from :attr:`response` when it
    was written, so finding every miss is a ``WHERE`` rather than a JSON probe."""

    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """The job's element of the response array, exactly as OpenFIGI sent it:
    ``{"data": [...]}``, ``{"warning": "..."}`` or ``{"error": "..."}``. The
    source of truth; everything the client returns is parsed from this."""

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """When the answer was received, from the database's clock. One request's
    jobs share one transaction, and so one timestamp."""

    __table_args__ = (
        # The question this table is for: the latest answer for these CUSIPs.
        Index("ix_figi_response_log_cusip_requested_at", "cusip", desc("requested_at")),
        CheckConstraint(OUTCOME_CHECK, name="outcome_is_known"),
    )
