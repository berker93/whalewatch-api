"""The people and entities behind Form 4s, and what they were to the issuer when they filed.

An *insider* is whoever a Form 4's ``reportingOwner`` names: an officer, a
director, a 10% owner, sometimes a fund that is all three. Identified by CIK,
which EDGAR assigns to people as well as to companies, and never by name: two
directors called John Smith are two insiders, and one director who signs as
"Smith John A" and "John A. Smith" is one.

The roles are not here. They belong to one filing (:class:`ReportingOwner`),
because they are a relationship with one issuer at one time. Warren Buffett is
a director of Berkshire and a 10% owner of other companies, and a CFO who joins
the board becomes a director in March without having been one in January.
"""

from sqlalchemy import CHAR, BigInteger, Boolean, ForeignKey, Index, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Insider(Base):
    """One reporting owner, across every filing and every issuer."""

    __tablename__ = "insider"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    cik: Mapped[str] = mapped_column(CHAR(10), unique=True)
    """The natural key, ten characters, zero-padded: ``rptOwnerCik`` as filed.

    Real and stable for people, which is what makes it the key. Matching on
    name would merge different people who share one and split one person
    across spellings.
    """

    name: Mapped[str] = mapped_column(Text)
    """``rptOwnerName`` as the latest-filed filing loaded spelled it, whatever
    order they were loaded in, so re-loading an old filing does not bring back
    an old spelling. EDGAR writes people surname first ("BUFFETT WARREN E"),
    and nothing here reorders it."""


class ReportingOwner(Base):
    """One insider on one Form 4, with the relationship to the issuer they checked.

    A filing can name several reporting owners. Joint filings are common where a
    fund, its general partner and the person who runs both all own the same
    shares. The transactions are reported once for all of them, so each
    ``insider_transaction`` row is attributed to one owner (the first named) and
    the others are here. Fanning the transaction out to every owner instead
    would count one purchase once per entity: three "insiders" buying, in a
    cluster query, that are one fund buying once.
    """

    __tablename__ = "reporting_owner"

    filing_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE: deleting a filing is how a 4/A supersedes it and how a bad
        # parse is redone, as for holding.
        ForeignKey("filing.id", ondelete="CASCADE"),
        primary_key=True,
    )

    insider_id: Mapped[int] = mapped_column(
        BigInteger,
        # RESTRICT: an insider with filings is not one to delete by accident.
        ForeignKey("insider.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    # Booleans, not an enum: one owner can be several at once, and a CEO who
    # sits on the board is both an officer and a director on the same filing.
    is_director: Mapped[bool] = mapped_column(Boolean, server_default=false())
    is_officer: Mapped[bool] = mapped_column(Boolean, server_default=false())
    is_ten_percent_owner: Mapped[bool] = mapped_column(Boolean, server_default=false())
    is_other: Mapped[bool] = mapped_column(Boolean, server_default=false())

    officer_title: Mapped[str | None] = mapped_column(Text)
    """``officerTitle`` as filed: "Chief Executive Officer", "CEO", "EVP & CFO".
    Free text, so no check ties it to :attr:`is_officer`, and filers do write a
    title without checking the box."""

    other_text: Mapped[str | None] = mapped_column(Text)
    """``otherText``: what "other" means, when :attr:`is_other` is checked."""

    __table_args__ = (
        # An insider's filings: the loader's latest-spelling rule, and the
        # RESTRICT check when an insider row is deleted.
        Index("ix_reporting_owner_insider_id", "insider_id"),
    )
