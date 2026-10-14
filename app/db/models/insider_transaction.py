"""One line of a Form 4's transaction tables.

Event-shaped where ``holding`` is periodic: a holding says what a filer had at
a quarter end, and this says what an insider did on a day. Both of a Form 4's
tables land here, ``nonDerivativeTable`` (shares) and ``derivativeTable``
(options, warrants, convertibles), told apart by :attr:`is_derivative`. Its
``...Holding`` rows, which report a balance and no transaction, do not.

**Partitioned by ``transaction_date``, eventually** (SEC-6, after the feed
exists). Postgres requires the partition key in every unique constraint on a
partitioned table, the primary key included, so it is in both already. That
makes SEC-6 a data move, not a change to what counts as a duplicate.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    desc,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY, QUANTITY
from app.db.models.implied_price import PRICE

#: The generated :attr:`InsiderTransaction.value_usd`, in SQL. Null when either
#: side is, which is every grant and gift.
VALUE_EXPRESSION = "round(shares * price_per_share, 2)"

#: The codes an open-market trade is filed under: a purchase and a sale. The
#: two that carry information about what an insider thinks, and the feed's
#: default. Everything else is compensation or mechanics (see the data model).
OPEN_MARKET_CODES = ("P", "S")

OPEN_MARKET_PREDICATE = "transaction_code IN ({})".format(
    ", ".join(f"'{code}'" for code in OPEN_MARKET_CODES)
)


class InsiderTransaction(Base):
    """One transaction an insider reported on one Form 4."""

    __tablename__ = "insider_transaction"

    # autoincrement because the key is composite, and SQLAlchemy only gives a
    # lone integer key a sequence by default. A sequence rather than an
    # IDENTITY column: Postgres 16 refuses IDENTITY on a partitioned table.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    transaction_date: Mapped[date] = mapped_column(Date, primary_key=True)
    """``transactionDate``: the day of the trade, not of the filing, which is
    up to two business days later and for a late filer much more. In the
    primary key only because SEC-6 partitions on it; :attr:`id` alone is
    unique."""

    filing_id: Mapped[int] = mapped_column(BigInteger)
    """The Form 4 or 4/A. See the composite FK in __table_args__."""

    insider_id: Mapped[int] = mapped_column(BigInteger)
    """The reporting owner the transaction is attributed to: the first one
    the filing names. See :class:`~app.db.models.insider.ReportingOwner` for
    the rest, and the composite FK in __table_args__."""

    line_number: Mapped[int] = mapped_column(SmallInteger)
    """Where the transaction sits in its document: 1 for the first, counting
    the non-derivative table and then the derivative table, in document order.

    The row identity the source does not have. One Form 4 can legitimately
    report the same trade shape twice, a sale executed in two tranches of 500
    shares at $41.20 on the same day, and every column but this one is then
    equal. A key on the values would keep one tranche and lose the other.
    """

    issuer_cik: Mapped[str] = mapped_column(CHAR(10))
    """``issuerCik``: the company the insider is an insider of."""

    issuer_name: Mapped[str] = mapped_column(Text)
    """``issuerName`` as filed."""

    issuer_ticker: Mapped[str | None] = mapped_column(Text)
    """``issuerTradingSymbol`` as filed: the one place in this pipeline a ticker
    arrives for free. As of the filing, and filers write "NONE" or nothing for
    an issuer without one. Evidence for :attr:`security_id`, not a substitute."""

    security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # SET NULL: the link is our interpretation, and losing it loses no
        # transaction. RESTRICT would let a stale link block security clean-up.
        ForeignKey("security.id", ondelete="SET NULL"),
    )
    """The issuer's security, where the loader could tell which one it is.
    Null otherwise, and that blocks nothing: the row is still loaded, still in
    the feed, and still found by :attr:`issuer_cik`.

    Written by the insider loader (INS-4), from :attr:`issuer_cik`,
    :attr:`issuer_ticker` and :attr:`security_title`. A CIK alone cannot always
    say which security: Alphabet is one CIK and two listed classes. On the
    issuer's security for a derivative line too, which is the underlying."""

    security_title: Mapped[str | None] = mapped_column(Text)
    """``securityTitle`` as filed: "Common Stock", "Class A Common Stock",
    "Stock Option (Right to Buy)"."""

    is_derivative: Mapped[bool] = mapped_column(Boolean)
    """True for a line of the derivative table. :attr:`shares` is then the
    number of underlying shares, and :attr:`price_per_share` the exercise or
    conversion price as filed, which is not a market price."""

    transaction_code: Mapped[str] = mapped_column(CHAR(1))
    """``transactionCode``, raw: P, S, A, M, F, G, C and the rest.

    No check, unlike every other vocabulary in the schema, because the set is
    the SEC's and it is not ours to close. Interpreted nowhere but the read
    layer. Collapsing codes into "bought" and "sold" at ingest is how a vesting
    event becomes a headline.
    """

    acquired_disposed: Mapped[str] = mapped_column(CHAR(1))
    """``transactionAcquiredDisposedCode``: ``'A'`` or ``'D'``."""

    shares: Mapped[Decimal | None] = mapped_column(QUANTITY)
    """``transactionShares``. Unsigned: :attr:`acquired_disposed` gives the
    direction. Null only when the filing left it out."""

    price_per_share: Mapped[Decimal | None] = mapped_column(PRICE)
    """``transactionPricePerShare``, in dollars. **Null, never zero**, when the
    filing has none: a grant, a gift, a price given only as a range in a
    footnote. A zero averages into "insiders bought at $0" the first time
    anyone computes a mean, so the check below refuses it."""

    value_usd: Mapped[Decimal | None] = mapped_column(
        MONEY, Computed(VALUE_EXPRESSION, persisted=True)
    )
    """:attr:`shares` times :attr:`price_per_share`, to the cent. Generated,
    so it cannot disagree with them, and null whenever the price is."""

    shares_owned_after: Mapped[Decimal | None] = mapped_column(QUANTITY)
    """``sharesOwnedFollowingTransaction``: the insider's holding of this
    security after the trade."""

    is_10b5_1: Mapped[bool] = mapped_column(Boolean, server_default=false())
    """The trade was made under a Rule 10b5-1 plan: decided months before the
    date on the form, so it says nothing about what the insider thinks today.
    The form's checkbox since April 2023, and only a footnote before it."""

    footnotes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The footnotes this transaction refers to, by the filing's own ids:
    ``{"F1": "The price reported is a weighted average...", ...}``. Null when
    it refers to none.

    Kept because footnotes are where Form 4 says what the structured fields
    cannot: the price range behind a weighted average, a 10b5-1 plan before
    the checkbox, shares held through a trust.
    """

    __table_args__ = (
        # --- the natural key ------------------------------------------------
        #
        # A re-ingested filing is the same filing_id, so its lines conflict on
        # line_number and ON CONFLICT DO NOTHING drops them. transaction_date
        # adds nothing to the key today, since a line has one date. It is here
        # for SEC-6's partitioning, which cannot keep a unique constraint
        # without it.
        UniqueConstraint(
            "filing_id",
            "line_number",
            "transaction_date",
            name="uq_insider_transaction_filing_id_line_number_transaction_date",
        ),
        # --- keeping the attribution honest ---------------------------------
        #
        # A transaction's insider is one of its filing's reporting owners. The
        # same composite-FK device as holding's: it also carries filing_id's
        # and insider_id's integrity, through reporting_owner's own FKs.
        ForeignKeyConstraint(
            ["filing_id", "insider_id"],
            ["reporting_owner.filing_id", "reporting_owner.insider_id"],
            name="fk_insider_transaction_filing_id_insider_id_reporting_owner",
            ondelete="CASCADE",
        ),
        # --- indexes ----------------------------------------------------------
        #
        # The feed: open-market trades, newest first. Partial, because P and S
        # are a minority of Form 4 lines (grants, exercises and tax
        # withholding are most of them) and the only ones the feed shows by
        # default.
        Index(
            "ix_insider_transaction_transaction_date_open_market",
            desc("transaction_date"),
            postgresql_where=text(OPEN_MARKET_PREDICATE),
        ),
        # --- invariants -------------------------------------------------------
        CheckConstraint("line_number > 0", name="line_number_is_positive"),
        CheckConstraint("acquired_disposed IN ('A', 'D')", name="acquired_disposed_is_known"),
        CheckConstraint("shares >= 0", name="shares_is_not_negative"),
        CheckConstraint("shares_owned_after >= 0", name="shares_owned_after_is_not_negative"),
        CheckConstraint("price_per_share > 0", name="a_price_is_positive_or_null"),
        CheckConstraint("jsonb_typeof(footnotes) = 'object'", name="footnotes_is_an_object"),
    )
