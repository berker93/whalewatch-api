"""The institution behind a 13F, and the CIKs it files under.

Two tables rather than one, because the thing a user means by "Berkshire" and
the thing EDGAR means by a CIK are not the same thing and do not have the same
cardinality.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class FilerCategory(StrEnum):
    """The investment style a filer is filed under on the site.

    Text with a ``CHECK``, not a native enum, by the rule in
    :mod:`app.db.models.enums`: the set is ours, it is editorial, and the day a
    seventh style is wanted should be one line of DDL rather than an
    ``ALTER TYPE`` that cannot be reversed.

    One per filer, and deliberately coarse. It exists to group pages and to cut
    the aggregate views, not to describe a strategy — a style label precise
    enough to be uncontroversial would be too fine to group anything by.
    """

    VALUE = "value"
    ACTIVIST = "activist"
    QUANT = "quant"
    MACRO = "macro"
    GROWTH = "growth"
    MULTI_STRATEGY = "multi_strategy"


CATEGORY_CHECK = "category IS NULL OR category IN ({})".format(
    ", ".join(f"'{category.value}'" for category in FilerCategory)
)


class OverlapPolicy(StrEnum):
    """What to do when two of a filer's CIKs both report the same period.

    It happens in two situations that look identical in the filing index and
    need opposite treatment. During a reorganisation the old entity and the
    new one both file for a quarter or two, reporting **one book twice** —
    summing them doubles the fund. A few institutions instead run **several
    books permanently**, each registered adviser filing its own — keeping
    only one of them loses the rest.

    ``successor``
        The default. Only the CIK listed last in ``data/investors.yaml`` (the
        highest :attr:`FilerCik.priority`) counts for a period both filed.
    ``sum``
        Every CIK's filings count. Opt-in, per filer, and only once
        ``audit-overlaps`` has shown the books really are separate.

    Applied by the ``effective_filing`` view, never by the loader: every filing
    is loaded as filed, and this decides only which of them a read counts.
    """

    SUCCESSOR = "successor"
    SUM = "sum"


OVERLAP_CHECK = "overlap IN ({})".format(", ".join(f"'{policy.value}'" for policy in OverlapPolicy))


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

    display_name: Mapped[str | None] = mapped_column(Text)
    """Our name for the institution, from ``data/investors.yaml``.

    Separate from :attr:`name` because they answer different questions.
    ``name`` is what the filer called itself on its latest cover page —
    "Pershing Square Capital Management, L.P." — and belongs to ingestion;
    this is what the page header says, and belongs to whoever edits the list.

    The curated columns below are all nullable for the same reason: the list
    is how a filer *gets* them, not what makes a filer a filer. A row a test or
    a future discovery job inserts with only a name and a slug is still a valid
    filer, and "not curated yet" is a state worth being able to represent.
    """

    manager_name: Mapped[str | None] = mapped_column(Text)
    """The person the page is known by, which is not always who runs it today.

    "Warren Buffett" for Berkshire, "Jim Simons" for Renaissance. Succession
    goes in :attr:`notes`, not here — a manager name that tracked every CIO
    change would make the pages people search for harder to find.
    """

    category: Mapped[str | None] = mapped_column(Text)
    """One :class:`FilerCategory` value. See there for why it is coarse."""

    country: Mapped[str | None] = mapped_column(CHAR(2))
    """ISO 3166-1 alpha-2, where the manager is run from — ``GB`` for London.

    Not EDGAR's ``stateOrCountry``, which is a mailing address and a private
    code list (``X0`` is the United Kingdom, ``E9`` the Cayman Islands). A
    Hong Kong manager filing through a Cayman entity is ``HK`` here.
    """

    notes: Mapped[str | None] = mapped_column(Text)
    """Editorial notes for whoever maintains ingestion. Not shown to users."""

    overlap: Mapped[str] = mapped_column(Text, server_default=OverlapPolicy.SUCCESSOR.value)
    """One :class:`OverlapPolicy` value. ``NOT NULL``, unlike the curated
    columns above: it changes what a read returns, so "unset" has to mean the
    safe default rather than nothing."""

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

    __table_args__ = (
        CheckConstraint(CATEGORY_CHECK, name="category_is_known"),
        CheckConstraint(OVERLAP_CHECK, name="overlap_is_known"),
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

    priority: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    """This CIK's position in its filer's ``ciks`` list, from 0.

    Decides who wins an overlap under :attr:`OverlapPolicy.SUCCESSOR`: the
    highest priority. The list is written oldest first, so the successor
    entity wins a reorganisation's overlap quarters by default — and a filer
    whose *primary* entity is not its newest lists the primary last anyway.
    Rows inserted outside the seed get 0, and ties are broken by CIK so a
    period never resolves to two CIKs by accident.
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
