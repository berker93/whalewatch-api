"""The institution behind a 13F, and the CIKs it files under.

Two tables rather than one, because the thing a user means by "Berkshire" and
the thing EDGAR means by a CIK are not the same thing and do not have the same
cardinality.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import CHAR, BigInteger, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class Filer(Base):
    """One institution, however many CIKs it files under.

    Deliberately carries no ``cik`` column. See :class:`FilerCik`.
    """

    __tablename__ = "filer"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    name: Mapped[str] = mapped_column(Text)
    """As reported on the most recent cover page, and therefore not stable.

    Managers rename themselves, and the name on a 2013 filing is not the name on
    a 2025 one. Nothing joins on this; it is display text.
    """

    slug: Mapped[str] = mapped_column(Text, unique=True)
    """The public identifier in URLs — ``/investors/berkshire-hathaway``.

    Ours, not EDGAR's, generated once from the name we first saw and then frozen
    even when the filer rebrands. A slug derived live from :attr:`name` is a URL
    that changes under a client the quarter a fund changes its letterhead.

    ``text`` rather than the ``citext`` the data model sketched: slugs are
    generated lowercase by the one function that mints them, so case-insensitive
    comparison has nothing to do — and ``citext`` is an extension, which is a
    ``CREATE EXTENSION`` in every database anyone ever builds, including a
    developer's throwaway one.
    """

    first_period: Mapped[date | None]
    last_period: Mapped[date | None]
    """The span of periods we actually hold, maintained by ingestion.

    Denormalised summaries of ``filing``, kept here so that listing filers does
    not aggregate over every filing per row. Both are null until the filer's
    first successful ingest, which is a real state: a tracked filer we have not
    loaded yet.
    """

    ciks: Mapped[list[FilerCik]] = relationship(
        back_populates="filer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class FilerCik(Base):
    """One CIK, belonging to one filer. Many rows per filer.

    A single institution files under several CIKs, routinely and permanently.
    Funds are registered per legal entity, entities get reorganised, and an
    acquired manager keeps filing under its own CIK for years after the
    acquisition. Modelling ``cik`` as a unique column on ``filer`` forces a
    choice at ingest time between inventing a second "filer" for what is
    obviously one institution — splitting its history in half, so the API shows
    two Berkshires with a decade each — or picking one CIK as canonical and
    discarding the filings made under the others.

    So the join table, and the constraint that matters is the one below.
    """

    __tablename__ = "filer_cik"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    filer_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE, because a filer_cik row without its filer is not a row that
        # means anything — unlike a filing, which is EDGAR's fact and survives
        # whatever we decide about our own grouping of filers.
        ForeignKey("filer.id", ondelete="CASCADE"),
    )

    cik: Mapped[str] = mapped_column(CHAR(10))
    """Zero-padded to ten characters. Berkshire is ``0001067983``.

    Not an integer, and this is the column where that decision is load-bearing.
    ``1067983`` stops matching EDGAR's submissions URLs, stops matching the
    accession numbers and directory paths built from it, and stops matching
    every log line written before someone changed the type. Storing it as EDGAR
    writes it means the value can be pasted into a URL, and means a grep for a
    CIK returns the ingestion logs, the filings and the API access log together.

    ``CHAR`` rather than ``VARCHAR`` because the width is genuinely fixed at ten
    — the padding is part of the identifier, not incidental — so a nine
    character value in here is a bug worth having the type reject.
    """

    filer: Mapped[Filer] = relationship(back_populates="ciks")

    __table_args__ = (
        # The AC's "unique index on cik", and the half of this table that does
        # the work: one CIK belongs to exactly one filer, globally. Without it
        # two filer rows can claim the same CIK and the resolution from a
        # filing's CIK to a filer stops being a function.
        #
        # Named explicitly: the naming convention would render this
        # uq_filer_cik_cik from the table and column anyway, but a constraint a
        # later migration has to drop by name is one worth being able to read
        # off the model.
        UniqueConstraint("cik", name="uq_filer_cik_cik"),
    )
