"""One row per EDGAR submission, of any form type.

The table every ingestion path writes first and every re-ingest looks in, which
makes :attr:`Filing.accession_no` the single most important constraint in the
schema.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base
from app.db.models.enums import AmendmentKind
from app.db.models.filer import Filer

# The generated expression behind `filing.quarter`, as one definition rather
# than a string repeated in the model and again in the migration.
#
# The obvious spelling of this is `to_char(period_of_report, 'YYYY"Q"Q')`, and
# Postgres rejects it: `to_char(timestamp, text)` is STABLE, not IMMUTABLE —
# its output depends on `lc_time` and `DateStyle`, which a session can change —
# and a generated column's expression must be IMMUTABLE or the whole column is
# a lie the moment someone runs `SET lc_time`. `CREATE TABLE` fails outright
# with "generation expression is not immutable".
#
# `extract(text, date)` *is* immutable, and composing two of them produces
# byte-identical output: '2024Q1' for 2024-03-31. The `::int` casts are load
# bearing — EXTRACT returns numeric in Postgres 14+, so without them the year
# renders as '2024.000000...' rather than '2024'.
QUARTER_EXPRESSION = (
    "EXTRACT(YEAR FROM period_of_report)::int::text"
    " || 'Q' || "
    "EXTRACT(QUARTER FROM period_of_report)::int::text"
)


class Filing(Base):
    """An EDGAR submission: 13F-HR, 13F-HR/A, Form 4, whatever comes next.

    One table for every form type rather than one per form, because the things
    that are true of all of them — an accession number, a filer, a filing
    timestamp, a parse that may not have happened yet — are the things ingestion
    and the retry paths operate on, and splitting them means every one of those
    queries becomes a union.
    """

    __tablename__ = "filing"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    accession_no: Mapped[str] = mapped_column(CHAR(20), unique=True)
    """EDGAR's identifier for the submission, dashed: ``0001067983-24-000011``.

    **This is the idempotency key**, and the ``UNIQUE`` on it is what makes the
    whole ingestion pipeline safe to re-run. Every task in ``app/jobs`` is keyed
    on an accession number and every one of them can be delivered twice — Celery
    guarantees at-least-once, a backfill gets resumed after a crash, an operator
    re-runs a quarter by hand. The upsert those paths perform needs somewhere to
    conflict, and this is it.

    Without the constraint none of that fails loudly. It duplicates: two filing
    rows, two sets of holdings, and a portfolio reporting exactly twice the
    positions it should — which looks like a large fund rather than like a bug.

    Twenty characters, fixed, dashes included, exactly as EDGAR writes it. The
    undashed form appears in archive URLs; converting at the call site is one
    ``replace`` and keeps one spelling in the database.
    """

    cik: Mapped[str] = mapped_column(CHAR(10), index=True)
    """The CIK that submitted this, as filed. May be a filer, an issuer or an insider.

    Kept alongside :attr:`filer_id` rather than replaced by it, because the two
    answer different questions. This is EDGAR's fact about the submission and it
    is known the instant the daily index is read; ``filer_id`` is our resolution
    of it, which happens later and can be corrected. Discovery ("have I already
    got this one?") runs off the raw value, before any resolution exists.
    """

    filer_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # RESTRICT: a filer with filings is not a filer anyone should be able to
        # delete out from under them by accident.
        ForeignKey("filer.id", ondelete="RESTRICT"),
    )
    """The institution, once :attr:`cik` has been resolved through ``filer_cik``.

    Nullable, and stays nullable, for two reasons that both occur in normal
    operation: a filing discovered through the daily index is inserted before
    anything knows which filer it belongs to, and a Form 4 has no filer at all —
    its CIK is an insider's.
    """

    form_type: Mapped[str] = mapped_column(Text)
    """``13F-HR``, ``13F-HR/A``, ``4``, ``4/A``. Raw, as EDGAR spells it."""

    period_of_report: Mapped[date | None]
    """The quarter end this filing describes. Null for Form 4, which describes a day.

    Note this is the *period*, never the filing date, and the two are 45 days or
    more apart. Every aggregate in the API groups on this; nothing user-facing
    groups on :attr:`filed_at`.
    """

    quarter: Mapped[str | None] = mapped_column(
        Text,
        Computed(QUARTER_EXPRESSION, persisted=True),
    )
    """``'2024Q1'``. Generated by Postgres from :attr:`period_of_report`, stored.

    A generated column rather than a view, a trigger, or a value the loader
    writes, because those three can all disagree with the column they derive
    from and this one cannot: it has no ``INSERT`` path and no ``UPDATE`` path,
    so there is no code anywhere that could write ``2024Q1`` onto a June period.

    Stored rather than virtual because Postgres only implements ``STORED`` — and
    because the point is to index and group on it without recomputing.

    Null exactly when :attr:`period_of_report` is null, which is what makes it
    safe on the Form 4 rows sharing this table.

    Alembic cannot see this. Autogenerate does not diff generated columns, so
    the migration writes it with :data:`QUARTER_EXPRESSION` in raw DDL and any
    future change to the expression is a hand-written migration that drops and
    re-adds the column.
    """

    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When EDGAR accepted the submission. Timezone-aware, always.

    Carries more weight than a timestamp usually does. It decides
    :attr:`value_multiplier` — see there — and it is the only column that can
    answer "what did we know, and when", which a backtest needs in order not to
    trade on a position 45 days before it was disclosed.
    """

    value_multiplier: Mapped[int] = mapped_column(SmallInteger)
    """What the information table's ``value`` column had to be multiplied by: 1 or 1000.

    The 13F ``value`` field changed units on **2023-01-03**: thousands of dollars
    before, whole dollars on and after. The parser normalises every
    ``holding.value_usd`` to whole dollars regardless, and this column records
    which convention the filing itself used — so that a mis-parse is diagnosable
    from the database instead of by re-reading the raw document.

    Keyed off :attr:`filed_at`, **not** :attr:`period_of_report`. The convention
    follows the submission, so an amendment filed in 2024 for a 2019 period is
    in whole dollars even though the original filing for that same period was in
    thousands. A ``period < 2023`` test gets exactly the amendments wrong, and
    amendments are the filings nobody is watching.

    Getting this wrong is a 1000x error that does not announce itself: every
    filer in a mis-parsed quarter is wrong by the same factor, so rankings,
    percentages and quarter-over-quarter shapes all look completely normal.
    """

    amends_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("filing.id", ondelete="SET NULL"),
    )
    """The filing this one amends, for ``/A`` forms. Self-referential, nullable.

    A chain rather than a flag, because a period can be amended more than once
    and the order matters.
    """

    amendment_kind: Mapped[AmendmentKind | None] = mapped_column(
        Enum(
            AmendmentKind,
            name="amendment_kind",
            # Without this SQLAlchemy stores the *member names* — 'RESTATEMENT',
            # 'NEW_HOLDINGS' — while the Postgres type created from the same
            # Enum() carries the values. They differ, and every insert fails on
            # a type that looks correct in psql.
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    """Restatement or addition. Null when this is not an amendment.

    See :class:`~app.db.models.enums.AmendmentKind` for why this is the field
    that ruins a quarter when it is wrong.
    """

    report_type: Mapped[str | None] = mapped_column(Text)
    """13F cover page: ``HOLDINGS``, ``NOTICE`` or ``COMBINATION``.

    Stored, not derived at query time, because it changes what the filing
    *means*. A ``NOTICE`` contains no holdings at all — it says "everything I
    hold is reported by another manager" — and a ``COMBINATION`` contains only
    the subset the filer manages directly. Aggregating across filers without
    honouring this double-counts every position two affiliated managers both
    disclose.

    Text with no check constraint, unlike :attr:`amendment_kind`: the cover page
    wording has changed before and a new variant should land in the table for
    someone to look at, not fail the ingest.
    """

    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Null means fetched but not yet parsed, which is a normal intermediate state.

    Archiving the raw document and parsing it are separate steps on purpose, so
    that a parser bug is repaired by re-parsing bytes we already hold rather than
    by re-crawling EDGAR for a week.
    """

    parse_error: Mapped[str | None] = mapped_column(Text)
    """Why the last parse failed, kept on the row rather than only in the logs.

    "Which filings are broken right now" should be a query, not a grep through
    whatever retention the log shipper happens to have.
    """

    filer: Mapped[Filer | None] = relationship()
    amends: Mapped[Filing | None] = relationship(remote_side=[id])

    __table_args__ = (
        # The AC's two indexes.
        #
        # (filer_id, period_of_report DESC) serves the read path the whole API is
        # built on: one filer, newest periods first. The DESC is written because
        # that is the direction every caller wants; Postgres can walk a B-tree
        # backwards, so it changes no plan here — it matters the day a query
        # sorts these two columns in *opposite* directions, which is when a
        # uniform index stops being usable for the sort at all.
        Index(
            "ix_filing_filer_id_period_of_report",
            "filer_id",
            desc("period_of_report"),
        ),
        # (filed_at DESC) is the operational one: "what has landed recently",
        # across all filers, which is the ingestion monitor and the freshness
        # checks rather than anything user-facing.
        Index("ix_filing_filed_at", desc("filed_at")),
        # Not an index anyone queries — it exists to be the target of the
        # composite foreign key on `holding`, which is what stops that table's
        # denormalised period from drifting from this one. Postgres will only
        # let a foreign key reference a uniquely constrained set of columns, and
        # `id` alone being unique is not enough to satisfy it for a two-column
        # reference. See Holding.__table_args__.
        UniqueConstraint("id", "period_of_report", name="uq_filing_id_period_of_report"),
        CheckConstraint(
            "value_multiplier IN (1, 1000)",
            name="value_multiplier_is_one_or_one_thousand",
        ),
        # A 13F without a period cannot be grouped, cannot be compared to the
        # quarter before it, and cannot generate a `quarter`. Form 4 is exempt
        # because it genuinely has no period; the constraint is written as an
        # implication so that adding a new form type does not require touching
        # it.
        CheckConstraint(
            "form_type NOT LIKE '13F%' OR period_of_report IS NOT NULL",
            name="thirteen_f_has_a_period_of_report",
        ),
    )
