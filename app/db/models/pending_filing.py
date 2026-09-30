"""The ingestion work queue: filings discovery found and ingestion has not loaded.

Separate from ``filing`` because the two answer different questions. A
``filing`` row is a submission we hold, with a period, a timestamp and a
multiplier that all had to come from EDGAR's documents; this is a submission we
know *exists*, from one line of a submissions index, and nothing more. Writing
discovered filings into ``filing`` would mean inventing a ``filed_at`` and a
``value_multiplier`` for rows nobody has fetched — and ``filed_at`` is the one
column in the schema whose wrong value is a silent 1000x error.

Keeping the queue as a table rather than in memory is what makes discovery and
ingestion separately restartable, and what makes the backlog a ``SELECT``
rather than something to reconstruct from logs.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import CHAR, CheckConstraint, DateTime, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class PendingStatus(StrEnum):
    """Where a discovered filing is in its life. Text with a ``CHECK``, by the
    rule in :mod:`app.db.models.enums`: the set is ours.

    ``pending``
        Discovered, not yet attempted — or re-discovered after an earlier load
        went missing (see :func:`~app.ingestion.discovery.enqueue`).
    ``failed``
        ``ingest-filing`` tried and raised. :attr:`PendingFiling.last_error`
        says why and :attr:`PendingFiling.attempts` how often; replaying it is
        running ``ingest-filing`` again.
    ``done``
        Loaded. Kept rather than deleted, so ``attempts`` and ``last_error``
        survive as the history of a filing that took three tries.
    """

    PENDING = "pending"
    FAILED = "failed"
    DONE = "done"


PENDING_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{status.value}'" for status in PendingStatus)
)


class PendingFiling(Base):
    """One discovered submission, and what ingesting it has come to so far."""

    __tablename__ = "pending_filing"

    accession_no: Mapped[str] = mapped_column(CHAR(20), primary_key=True)
    """Dashed, as on :attr:`~app.db.models.filing.Filing.accession_no`.

    The primary key rather than a surrogate ``id``: nothing references this
    table, and the accession number is the key every write here conflicts on.
    """

    cik: Mapped[str] = mapped_column(CHAR(10))
    """The CIK whose submissions index listed the filing, zero-padded.

    Which is the CIK whose archive directory holds it — the thing
    ``ingest-filing`` cannot work out from the accession number, and reads from
    here so that draining the queue needs no ``--cik``.
    """

    form_type: Mapped[str] = mapped_column(Text)
    """``13F-HR`` or ``13F-HR/A``, as EDGAR's index spells it."""

    filing_date: Mapped[date]
    """EDGAR's ``filingDate``. For ordering and for a person reading the queue;
    never an input to the units cutover, which needs the acceptance instant."""

    report_date: Mapped[date | None]
    """EDGAR's ``reportDate``, the period the filing describes."""

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    """When discovery first listed it. Not moved by later runs that list it again."""

    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{PendingStatus.PENDING}'"))
    """One :class:`PendingStatus` value. ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    """How many times ingesting it has failed. Not reset by success."""

    last_error: Mapped[str | None] = mapped_column(Text)
    """The most recent failure's message, exception type first."""

    __table_args__ = (
        CheckConstraint(PENDING_STATUS_CHECK, name="status_is_known"),
        CheckConstraint("attempts >= 0", name="attempts_is_not_negative"),
    )
