"""The SEC's Official List of Section 13(f) Securities, one row per CUSIP per quarter.

Every quarter the SEC's Division of Investment Management publishes the list of
securities a 13F filer must report: CUSIP, issuer name, a short description of
the issue, and whether it was added to or deleted from the list since the last
one. It is the one free, authoritative source of issuer names keyed by CUSIP,
and it covers CUSIPs OpenFIGI has never heard of. It gives no tickers.

What it is for here: an issuer name for a CUSIP OpenFIGI cannot match, so an
unresolved security still renders as ``ACME CORP · 037833100`` from a name the
SEC wrote rather than one a filer abbreviated (see
:mod:`app.ingestion.enrichment.issuer_names`).

History is kept. Each quarter's list is its own set of rows, because the list
changes as securities are added and removed, and "was this reportable in
2024Q3?" is a question about the 2024Q3 list, not today's. ``import-13f-list``
replaces one quarter's rows and leaves every other quarter alone.

Not foreign-keyed to ``security``, for ``figi_response_log``'s reason: it
records what the SEC said about nine characters, whether or not anything we
track holds them. Most of the ~25,000 CUSIPs on a list are not held by anyone
we track.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Index,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.filing import QUARTER_EXPRESSION


class ListStatus(StrEnum):
    """A CUSIP's standing on one quarter's list. Text with a ``CHECK``, by the
    rule in :mod:`app.db.models.enums`: the set is ours.

    ``listed``
        On the list, as it was on the last one. The SEC's blank status column.
    ``added``
        New to the list this quarter (``*A*``).
    ``deleted``
        On the last list and not on this one (``*D*``). Still a row, because
        the SEC still printed it with its name, but no longer reportable.
    """

    LISTED = "listed"
    ADDED = "added"
    DELETED = "deleted"


STATUS_CHECK = "status IN ({})".format(", ".join(f"'{status.value}'" for status in ListStatus))


class Sec13FSecurity(Base):
    """One CUSIP as one quarter's Official List of Section 13(f) Securities has it."""

    __tablename__ = "sec_13f_security"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    period_of_report: Mapped[date]
    """The quarter end the list is for: the period of the 13Fs it governs, as
    on ``filing``. The list the SEC calls "1st quarter 2026" is ``2026-03-31``."""

    quarter: Mapped[str] = mapped_column(Text, Computed(QUARTER_EXPRESSION, persisted=True))
    """``'2026Q1'``, generated from :attr:`period_of_report` exactly as
    ``filing.quarter`` is, so the two join and read alike."""

    cusip: Mapped[str] = mapped_column(CHAR(9))
    """As the SEC printed it. Not check-digit validated: the list's option rows
    carry synthetic CUSIPs (``…9`` in the seventh place) that fail the check."""

    issuer_name: Mapped[str] = mapped_column(Text)
    """The SEC's name for the issuer, upper case and cut to 28 characters by the
    list's fixed-width format: ``AGRICULTURE & NAT SOL ACQ CO``."""

    issuer_description: Mapped[str | None] = mapped_column(Text)
    """What the issue is: ``COM``, ``CL A``, ``NOTE 0.250% 6/0``, ``CALL``.
    Cut short by the format too. Null if the SEC printed none."""

    status: Mapped[str] = mapped_column(Text)
    """One :class:`ListStatus` value."""

    has_listed_options: Mapped[bool] = mapped_column(Boolean)
    """The list's asterisk beside the CUSIP: options on this security are
    themselves 13(f) securities."""

    __table_args__ = (
        # One row per CUSIP per list. The published file can print a CUSIP more
        # than once (option CUSIPs shared across a trust's funds); the parser
        # collapses those before they get here.
        UniqueConstraint("period_of_report", "cusip"),
        # The fallback's question: the latest list that names these CUSIPs.
        Index("ix_sec_13f_security_cusip_period_of_report", "cusip", desc("period_of_report")),
        CheckConstraint(STATUS_CHECK, name="status_is_known"),
        CheckConstraint("issuer_name <> ''", name="issuer_name_is_not_empty"),
    )
