"""One row per execution of a job: what ran, with what, and how it ended.

So that "did the backfill finish?" is a ``SELECT`` rather than an hour of
scrollback archaeology. The log says everything a run did, line by line; this
says what it came to, in one row, and ``id`` is the ``run_id`` on every one of
those lines, so either leads to the other.

Written by :func:`~app.jobs.runs.track_run` and nothing else, through sessions
of its own rather than the job's. A job whose transaction rolls back has
nothing left to say about itself except here, and a row written in that
transaction would roll back with it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, desc, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class RunStatus(StrEnum):
    """How a run ended, or that it has not. Text with a ``CHECK``, by the rule
    in :mod:`app.db.models.enums`: the set is ours.

    ``running``
        Started, not finished. Also what a process killed outright leaves
        behind, since nothing runs after ``SIGKILL`` to say otherwise: an old
        ``running`` row is a crash, not a slow job.
    ``success``
        Finished, and every item it took on went through.
    ``partial``
        Finished, with something left undone: filings that failed, CIKs that
        could not be read, a run stopped before it reached the end of its plan.
        :attr:`IngestionRun.error` lists what.
    ``failed``
        Raised. :attr:`IngestionRun.error` says what, first line first.
    """

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


RUN_STATUS_CHECK = "status IN ({})".format(", ".join(f"'{status.value}'" for status in RunStatus))

#: A run is finished exactly when it has a finish time. Without it, a row can
#: say ``success`` with no ``finished_at`` and the duration of every run in a
#: report becomes a guess about which column to believe.
FINISHED_CHECK = "(status = 'running') = (finished_at IS NULL)"

#: ``partial`` and ``failed`` are claims that something went wrong, and a claim
#: with no detail sends whoever reads it to the logs — which this table exists
#: so they do not have to start with. ``filing``'s suspect check, for runs.
NOT_SUCCESS_SAYS_WHY_CHECK = "status NOT IN ('partial', 'failed') OR error IS NOT NULL"


class IngestionRun(Base):
    """One execution of one job."""

    __tablename__ = "ingestion_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    """The run's ``run_id``, bound into the logging context for its duration.

    A UUID rather than a sequence, for the logs' sake: ``grep`` for a UUID
    returns that run's lines and nothing else, where ``grep 42`` returns every
    line with a 42 in it. :func:`~app.jobs.runs.track_run` generates it before
    the row is written, so even a run whose row could not be written has an id
    in its log lines. The server default is for rows inserted by hand.
    """

    job_name: Mapped[str] = mapped_column(Text)
    """The ``job_name`` in the logs: the CLI verb, or ``backfill_13f``."""

    status: Mapped[str] = mapped_column(Text, server_default=text(f"'{RunStatus.RUNNING}'"))
    """One :class:`RunStatus` value. ``str`` for the reason given on
    :attr:`~app.db.models.filing.Filing.parse_status`."""

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set with the final status, from the database's clock, as
    :attr:`started_at` is, so a duration never mixes two machines' clocks."""

    items_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    """What the job took on, in its own unit: filings planned, filings listed,
    filers in the list. The README's ``runs`` section has the unit per job."""

    items_written: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    """How many of those it wrote. Zero for the audits, which write nothing."""

    error: Mapped[str | None] = mapped_column(Text)
    """Why the run is not ``success``, one ``ExceptionType: message`` per line.

    The exception that failed the run comes first. A partial run lists each
    item that went wrong, so a backfill with three failed filings names three
    accession numbers here, the same text their queue rows keep.
    """

    context: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    """The run's parameters, as given: ``{"filer": null, "force": true, ...}``.

    ``jsonb`` so a question like "every forced backfill this month" is a
    ``WHERE context @> '{"force": true}'`` rather than a parse.
    """

    __table_args__ = (
        # The question this table is for: the latest runs of one job.
        Index("ix_ingestion_run_job_name_started_at", "job_name", desc("started_at")),
        CheckConstraint(RUN_STATUS_CHECK, name="status_is_known"),
        CheckConstraint(FINISHED_CHECK, name="finished_when_not_running"),
        CheckConstraint(NOT_SUCCESS_SAYS_WHY_CHECK, name="an_unsuccessful_run_says_why"),
        CheckConstraint("items_seen >= 0", name="items_seen_is_not_negative"),
        CheckConstraint("items_written >= 0", name="items_written_is_not_negative"),
    )
