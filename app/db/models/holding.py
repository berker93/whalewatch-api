"""One line of a 13F information table.

The big table — low millions of rows a year — and the one every number the API
reports is ultimately a sum over. Most of what follows is about making sure the
sums are over comparable things.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base
from app.db.models.filing import Filing
from app.db.models.security import Security

# Money. Two decimal places because the normalised value is whole dollars and
# the fraction is only ever an artefact of the multiplier; twenty digits because
# the largest positions are in the hundreds of billions and the point of numeric
# is that the ceiling is a decision rather than a surprise.
MONEY = Numeric(20, 2)

# Share counts and principal amounts. Four decimal places, and see Holding.shares
# for why a share count needs any at all.
QUANTITY = Numeric(20, 4)


class Holding(Base):
    """A single reported position: this filer held this much of this security.

    Every quantity here is ``numeric``. Not float, not double precision, and the
    reason is not precision anxiety in the abstract — it is that these columns
    are summed, ranked and compared for equality across quarters. Binary
    floating point makes ``sum()`` depend on the order rows come back in, so the
    same portfolio total changes when the planner switches to a parallel scan,
    and two values that should be equal are not. There is no rounding
    convention that rescues a total once it has been accumulated in float.
    """

    __tablename__ = "holding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    filing_id: Mapped[int] = mapped_column(BigInteger)
    """The submission this line came from. See the composite FK in __table_args__."""

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        # RESTRICT: a security that is held is not deletable. Cascading here
        # would let a botched enrichment job delete positions.
        ForeignKey("security.id", ondelete="RESTRICT"),
    )

    filer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("filer.id", ondelete="RESTRICT"),
    )
    """Denormalised from :class:`~app.db.models.filing.Filing`.

    Every read path in the service filters on filer and period, and the
    alternative is a join to ``filing`` on literally every query. Denormalised
    columns are a lie waiting to happen, so this one and
    :attr:`period_of_report` are held to their source by a composite foreign
    key rather than by a convention — see __table_args__.
    """

    period_of_report: Mapped[date] = mapped_column()
    """Denormalised from the filing, and ``NOT NULL`` here where it is nullable there.

    A holding always has a period: only 13F filings produce holdings, and the
    check constraint on ``filing`` guarantees those have one.

    ``NOT NULL`` also matters for a reason that is not about this quarter. When
    this table eventually partitions — ``RANGE (period_of_report)``, because
    every read path already filters on it — Postgres requires the partition key
    to be non-null and to appear in every unique constraint on the table. Both
    are true here already, which turns that migration from a table rewrite into
    a decision.
    """

    cusip: Mapped[str] = mapped_column(CHAR(9))
    """The CUSIP exactly as this filing wrote it.

    Not redundant with ``security.cusip``, even though the FK resolves to the
    same nine characters today. This is the filing's own bytes, part of the
    natural key below, and the thing to compare against when a resolution turns
    out to have been wrong. ``security`` is a mutable interpretation; this is
    the record.
    """

    value_usd: Mapped[Decimal] = mapped_column(MONEY)
    """**Always whole dollars**, for every row, regardless of what the filing said.

    Normalised at parse time using ``filing.value_multiplier``. The alternative
    — store the filing's own units, convert on read — requires every consumer to
    know about the 2023-01-03 cutover, and one of them will not.

    One thing this column is not: comparable across ``put_call`` values. An
    option line's value is the notional value of the underlying, not the
    premium, so summing it with common stock inflates a portfolio by the entire
    underlying exposure.
    """

    shares: Mapped[Decimal] = mapped_column(QUANTITY)
    """Share count, or principal amount — :attr:`sshprnamt_type` says which.

    ``numeric`` rather than ``bigint`` for two independent reasons: ``PRN`` rows
    report a face value rather than a count, and fractional share counts do
    appear after some corporate actions. Truncating either to an integer loses
    real quantity.

    Reported as of the period end and never adjusted. Comparing two quarters
    across a split shows every holder's position multiplying, which is why
    ``holding_change`` split-adjusts rather than subtracting these directly.
    """

    sshprnamt_type: Mapped[str] = mapped_column(Text)
    """``'SH'`` for shares, ``'PRN'`` for principal amount.

    Convertible bonds report face value. Summing ``PRN`` rows with ``SH`` rows
    adds dollars to a share count and produces a number with no unit at all. The
    check constraint below can only keep the column honest — it cannot stop the
    query, which is why this is part of the natural key: the two kinds stay on
    separate rows so that a ``GROUP BY`` can separate them.
    """

    put_call: Mapped[str | None] = mapped_column(Text)
    """``'Put'``, ``'Call'``, or null for the underlying itself.

    Null is the overwhelmingly common case, and it is why the unique constraint
    below needs ``NULLS NOT DISTINCT``.
    """

    investment_discretion: Mapped[str | None] = mapped_column(Text)
    """``SOLE``, ``DEFINED`` or ``SHARED``, as filed."""

    voting_sole: Mapped[Decimal | None] = mapped_column(QUANTITY)
    voting_shared: Mapped[Decimal | None] = mapped_column(QUANTITY)
    voting_none: Mapped[Decimal | None] = mapped_column(QUANTITY)
    """The voting authority breakdown. Nullable: not every filer populates it.

    Deliberately not constrained to sum to :attr:`shares`. Filers get this wrong
    often enough that enforcing it would reject valid filings, and the
    disagreement is itself worth keeping.
    """

    filing: Mapped[Filing] = relationship()
    security: Mapped[Security] = relationship()

    __table_args__ = (
        # --- the natural key ------------------------------------------------
        #
        # What makes re-ingesting a filing an upsert rather than a second copy
        # of the position. Note that it is not (filing_id, cusip): one filer can
        # report the same CUSIP three times in one filing — common stock, calls
        # and puts — and can report it twice more split by SH and PRN. A
        # two-column key collapses all of those into one row on conflict and
        # silently discards the rest.
        #
        # NULLS NOT DISTINCT is the part that is easy to leave off and
        # impossible to notice. In Postgres's default behaviour two NULLs never
        # conflict, so with put_call null — the common-stock case, i.e. most
        # rows in the table — this constraint would permit unlimited duplicates
        # of exactly the rows it exists to protect. ON CONFLICT would then never
        # fire, and every re-ingest would add another copy of the entire filing.
        # Requires Postgres 15+; we target 16.
        UniqueConstraint(
            "filing_id",
            "cusip",
            "put_call",
            "sshprnamt_type",
            name="uq_holding_filing_id_cusip_put_call_sshprnamt_type",
            postgresql_nulls_not_distinct=True,
        ),
        # --- keeping the denormalisation honest -------------------------------
        #
        # filer_id and period_of_report are copies of columns on `filing`. This
        # composite FK is what makes them copies rather than a second opinion:
        # Postgres will reject any holding whose (filing_id, period_of_report)
        # pair does not exist on the filing itself, so the two cannot drift.
        # It references uq_filing_id_period_of_report, which exists for this.
        #
        # It also carries the referential integrity for filing_id, which is why
        # that column has no FK of its own.
        #
        # ON UPDATE CASCADE because a period corrected on the filing must reach
        # the holdings; ON DELETE CASCADE because a holding without its filing
        # is orphaned data, and deleting a filing is how a bad parse is redone.
        ForeignKeyConstraint(
            ["filing_id", "period_of_report"],
            ["filing.id", "filing.period_of_report"],
            name="fk_holding_filing_id_period_of_report_filing",
            onupdate="CASCADE",
            ondelete="CASCADE",
        ),
        # --- indexes ----------------------------------------------------------
        #
        # holding(filing_id): the AC asks for it explicitly. Worth flagging that
        # it duplicates the leading column of the unique constraint above, which
        # Postgres can already use for filing_id lookups — so this buys no plan
        # that is not already available and costs a write on every insert. Kept
        # because it is specified, and because it stops being redundant the day
        # the unique key's column order changes.
        Index("ix_holding_filing_id", "filing_id"),
        # holding(security_id): "who else holds this", across all filers.
        Index("ix_holding_security_id", "security_id"),
        # holding(cusip): the same question asked before resolution has run, and
        # the way a position is found when a security row is suspected wrong.
        Index("ix_holding_cusip", "cusip"),
        # Not in the AC's list, and the reason filer_id and period_of_report are
        # on this table at all. Denormalising two columns onto the largest table
        # in the schema to avoid a join, and then leaving the composite lookup
        # to a scan, is half of a decision.
        Index("ix_holding_filer_id_period_of_report", "filer_id", "period_of_report"),
        # --- invariants -------------------------------------------------------
        #
        # 13F is long-only: there are no short positions in this dataset and
        # never will be. A negative here is therefore not a bearish bet, it is a
        # sign error in the parser, and it should fail at the boundary rather
        # than quietly net against someone else's long.
        CheckConstraint("value_usd >= 0", name="value_usd_is_not_negative"),
        CheckConstraint("shares >= 0", name="shares_is_not_negative"),
        CheckConstraint("sshprnamt_type IN ('SH', 'PRN')", name="sshprnamt_type_is_known"),
        CheckConstraint(
            "put_call IS NULL OR put_call IN ('Put', 'Call')",
            name="put_call_is_known",
        ),
    )
