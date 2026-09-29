"""The investor universe: ``data/investors.yaml``, validated, and seeded.

The list decides what the product is about. Every page under ``/investors`` is
one entry, and a filing whose CIK is on no entry loads its cover page and defers
its holdings (see :func:`~app.ingestion.loaders.load_filing`) — so until an
institution is in this file, nothing it holds reaches any view. That makes the
file product, not configuration, and it is why the schema here is strict: a
typo in it is a missing fund, not a warning.

Two halves, deliberately separable. :func:`load_investors` is pure — bytes in,
validated entries out, no database — so the committed file is checked by the
unit suite on every run. :func:`seed_investors` writes those entries into
``filer`` and ``filer_cik`` and is safe to run as often as anyone likes.

What the seed will not do
-------------------------
*Move a CIK.* A CIK the database already maps to a different slug is refused,
not reassigned. The usual cause is a renamed slug — the new slug arrives as a
new filer and tries to claim its predecessor's CIKs — and a slug is a public
URL, so the refusal is the useful outcome. Moving a CIK on purpose is a data
migration with filings attached to it, and wants to be written as one.

*Delete anything.* A CIK dropped from the list stays mapped, and a filer
dropped from the list stays a filer. Both have filings hanging off them; the
seed reports the stragglers and leaves the decision to a person.

*Link filings already loaded.* A filing ingested before its CIK was listed has
``filer_id`` null and its holdings deferred. Setting the link here would make
it look like a loaded filing with no positions — which is what a ``13F-NT``
looks like — so those filings are re-ingested instead, and the loader links
them on the way through.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.filer import Filer, FilerCategory, FilerCik, OverlapPolicy

#: Where the list lives in the repository, and in the image (``COPY . .``).
DEFAULT_INVESTORS_PATH: Final = Path(__file__).resolve().parents[2] / "data" / "investors.yaml"

#: The curated columns, in the order :class:`InvestorEntry` declares them. The
#: seed compares and writes exactly these; ``name`` and the period span belong
#: to ingestion and are never touched after insert.
_CURATED: Final = ("display_name", "manager_name", "category", "country", "notes", "overlap")

#: Lowercase ASCII words joined by single hyphens. No leading, trailing or
#: doubled hyphen, nothing that needs percent-encoding, nothing that differs
#: from itself after the lowercasing some clients apply to paths.
_SLUG_PATTERN: Final = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

#: EDGAR's own ceiling: a CIK is at most ten digits.
_MAX_CIK: Final = 9_999_999_999

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class InvestorListError(ValueError):
    """The file is not a valid investor list. The message names every problem."""


class SeedConflictError(RuntimeError):
    """The list and the database disagree about who owns a CIK."""


class InvestorEntry(BaseModel):
    """One institution in the universe, as written in the YAML.

    ``extra="forbid"`` is the setting doing the most work here. Without it a
    misspelt key — ``manger_name`` — validates, the real field falls back to
    whatever it defaults to, and the typo ships.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: Annotated[str, StringConstraints(pattern=_SLUG_PATTERN, max_length=64)]
    """The public URL segment. Stable: see :attr:`app.db.models.filer.Filer.slug`."""

    display_name: _Text
    manager_name: _Text
    category: FilerCategory
    country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    """ISO 3166-1 alpha-2, uppercase. Quote ``"NO"`` for Norway: bare, YAML 1.1
    reads it as ``false`` and this field rejects it."""

    ciks: Annotated[list[Annotated[int, Field(ge=1, le=_MAX_CIK)]], Field(min_length=1)]
    """Every CIK the institution has filed 13Fs under, unpadded, oldest first.

    Integers in the file because that is how people copy them out of EDGAR's
    search results; :attr:`padded_ciks` is the spelling the database stores.

    The order is data, not presentation: it becomes
    :attr:`~app.db.models.filer.FilerCik.priority`, and under the ``successor``
    policy the CIK listed *last* is the one that counts when two filed for the
    same period.
    """

    notes: _Text | None = None

    overlap: OverlapPolicy = OverlapPolicy.SUCCESSOR
    """See :class:`~app.db.models.filer.OverlapPolicy`. Omitted for almost every
    entry; ``sum`` only where ``audit-overlaps`` has shown separate books."""

    @field_validator("ciks")
    @classmethod
    def _ciks_are_distinct(cls, ciks: list[int]) -> list[int]:
        repeated = sorted(cik for cik, count in Counter(ciks).items() if count > 1)
        if repeated:
            raise ValueError(f"lists CIK {', '.join(map(str, repeated))} more than once")
        return ciks

    @model_validator(mode="after")
    def _sum_needs_something_to_sum(self) -> Self:
        if self.overlap is OverlapPolicy.SUM and len(self.ciks) < 2:
            raise ValueError("overlap: sum needs at least two CIKs to sum")
        return self

    @property
    def padded_ciks(self) -> tuple[str, ...]:
        return tuple(f"{cik:010d}" for cik in self.ciks)

    def curated(self) -> dict[str, str | None]:
        """The values the seed writes to the curated columns, keyed by column."""
        return {
            "display_name": self.display_name,
            "manager_name": self.manager_name,
            "category": self.category.value,
            "country": self.country,
            "notes": self.notes,
            "overlap": self.overlap.value,
        }


class InvestorList(RootModel[list[InvestorEntry]]):
    """The whole file, and the rules no single entry can check on its own."""

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> Self:
        """No slug twice, and no CIK on two entries.

        The second is the one that matters more and is easier to miss. The
        database would refuse it too — ``filer_cik.cik`` is unique — but only
        halfway through a seed, as an ``IntegrityError`` naming neither entry.
        """
        problems = []

        slugs = Counter(entry.slug for entry in self.root)
        problems += [f"duplicate slug {slug!r}" for slug, count in slugs.items() if count > 1]

        owners: dict[int, list[str]] = {}
        for entry in self.root:
            for cik in entry.ciks:
                owners.setdefault(cik, []).append(entry.slug)
        problems += [
            f"duplicate CIK {cik} on {', '.join(slugs_)}"
            for cik, slugs_ in owners.items()
            if len(slugs_) > 1
        ]

        if problems:
            raise ValueError("; ".join(problems))
        return self


def load_investors(path: Path = DEFAULT_INVESTORS_PATH) -> tuple[InvestorEntry, ...]:
    """Read and validate the investor list at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise InvestorListError(f"cannot read {path}: {unreadable}") from unreadable
    return parse_investors(text, source=str(path))


def parse_investors(text: str, *, source: str = "<string>") -> tuple[InvestorEntry, ...]:
    """Validate an investor list given as YAML text.

    ``safe_load``, never ``load``: the file is edited by hand and reviewed as
    data, and a YAML tag that constructs a Python object is not something a
    reviewer reading a list of fund names is looking for.
    """
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as malformed:
        raise InvestorListError(f"{source} is not valid YAML: {malformed}") from malformed

    try:
        return tuple(InvestorList.model_validate(raw).root)
    except ValidationError as invalid:
        raise InvestorListError(_describe(invalid, raw, source)) from invalid


def _describe(error: ValidationError, raw: Any, source: str) -> str:
    """Pydantic's errors, with each located by slug rather than by list index.

    ``3.category`` is useless in a hundred-entry file; ``pershing-square:
    category`` is a search away.
    """
    lines = []
    for problem in error.errors():
        location: list[int | str] = list(problem["loc"])
        index = location[0] if location else None
        if isinstance(index, int) and isinstance(raw, list):
            entry = raw[index] if index < len(raw) else None
            slug = entry.get("slug") if isinstance(entry, dict) else None
            location[0] = slug or f"entry {index}"
        where = ".".join(str(part) for part in location) or "list"
        lines.append(f"  {where}: {problem['msg']}")
    return f"{source} failed validation:\n" + "\n".join(lines)


# --- the seed ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What one run did, counted so a second run can be seen to do nothing."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: int
    ciks_added: int
    ciks_reprioritised: int
    """Existing mappings whose list position changed — which changes who wins
    an overlap, so it is counted rather than folded into "unchanged"."""
    ciks_listed: int
    unlisted_ciks: tuple[tuple[str, str], ...]
    """``(slug, cik)`` mapped in the database to a listed filer but absent from
    its entry. Reported, never removed — see the module docstring."""


async def seed_investors(session: AsyncSession, entries: tuple[InvestorEntry, ...]) -> SeedResult:
    """Upsert ``entries`` into ``filer`` and ``filer_cik``. Idempotent.

    Filers are matched on slug. A new slug is inserted with ``name`` set to the
    display name, as a placeholder until ingestion writes a cover page's; an
    existing one has its curated columns brought into line and nothing else.
    CIKs are inserted if absent; an existing mapping only has its ``priority``
    (its position in the entry's list) brought into line.

    Does not commit: the caller's transaction is the unit, so a conflict found
    on the last entry leaves nothing from the first.

    :raises SeedConflictError: when a listed CIK already belongs to a filer
        with a different slug. Checked before anything is written.
    """
    listed = {cik: entry.slug for entry in entries for cik in entry.padded_ciks}
    mapped = await _refuse_moved_ciks(session, listed)

    before = {
        row.slug: {column: getattr(row, column) for column in _CURATED}
        for row in await session.execute(
            select(Filer.slug, *(getattr(Filer, column) for column in _CURATED)).where(
                Filer.slug.in_([entry.slug for entry in entries])
            )
        )
    }
    created = tuple(entry.slug for entry in entries if entry.slug not in before)
    updated = tuple(
        entry.slug
        for entry in entries
        if entry.slug in before and before[entry.slug] != entry.curated()
    )

    ids = await _upsert_filers(session, entries)
    await _upsert_ciks(session, ids, entries)
    priorities = {cik: priority for entry in entries for priority, cik in _ranked(entry)}

    unlisted = (
        await session.execute(
            select(Filer.slug, FilerCik.cik)
            .join(FilerCik, FilerCik.filer_id == Filer.id)
            .where(Filer.slug.in_(ids), FilerCik.cik.not_in(listed))
            .order_by(Filer.slug, FilerCik.cik)
        )
    ).all()

    return SeedResult(
        created=created,
        updated=updated,
        unchanged=len(entries) - len(created) - len(updated),
        ciks_added=len(listed.keys() - mapped.keys()),
        ciks_reprioritised=sum(
            1 for cik, priority in mapped.items() if priority != priorities[cik]
        ),
        ciks_listed=len(listed),
        unlisted_ciks=tuple((row.slug, row.cik) for row in unlisted),
    )


async def _refuse_moved_ciks(session: AsyncSession, listed: dict[str, str]) -> dict[str, int]:
    """Raise if a listed CIK belongs to another slug; else ``{cik: priority}`` of
    the listed CIKs already mapped, which is the "before" the counts need."""
    rows = (
        await session.execute(
            select(FilerCik.cik, FilerCik.priority, Filer.slug)
            .join(Filer, Filer.id == FilerCik.filer_id)
            .where(FilerCik.cik.in_(listed))
        )
    ).all()
    moved = sorted(
        f"CIK {row.cik} is listed under {listed[row.cik]!r} but belongs to {row.slug!r}"
        for row in rows
        if row.slug != listed[row.cik]
    )
    if moved:
        raise SeedConflictError(
            "; ".join(moved) + ". A renamed slug is the usual cause, and slugs are public "
            "URLs: restore the old slug, or move the CIK deliberately with a migration."
        )
    return {row.cik: row.priority for row in rows}


async def _upsert_filers(
    session: AsyncSession, entries: tuple[InvestorEntry, ...]
) -> dict[str, int]:
    """Insert or update every entry in one statement; return ``{slug: id}``.

    The ``WHERE`` on the update is what keeps a no-op run a no-op at the row
    level too. Without it every run rewrites all hundred rows, each rewrite a
    dead tuple and a WAL record, to set every column to the value it had.
    """
    statement = pg_insert(Filer).values(
        [{"slug": entry.slug, "name": entry.display_name, **entry.curated()} for entry in entries]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Filer.slug],
        set_={column: statement.excluded[column] for column in _CURATED},
        where=or_(
            *(
                getattr(Filer, column).is_distinct_from(statement.excluded[column])
                for column in _CURATED
            )
        ),
    )
    await session.execute(statement)

    # Read back rather than RETURNING, because RETURNING yields nothing for the
    # rows the WHERE above chose not to touch.
    rows = await session.execute(
        select(Filer.slug, Filer.id).where(Filer.slug.in_([entry.slug for entry in entries]))
    )
    return {row.slug: row.id for row in rows}


async def _upsert_ciks(
    session: AsyncSession, ids: dict[str, int], entries: tuple[InvestorEntry, ...]
) -> None:
    """Map every listed CIK to its filer at its list position.

    The conflict update touches ``priority`` only, and is safe only because
    :func:`_refuse_moved_ciks` ran first: by now any CIK already present belongs
    to the filer it is listed under, so ``filer_id`` has nothing to change. The
    ``WHERE`` keeps an unchanged mapping from being rewritten.
    """
    statement = pg_insert(FilerCik).values(
        [
            {"filer_id": ids[entry.slug], "cik": cik, "priority": priority}
            for entry in entries
            for priority, cik in _ranked(entry)
        ]
    )
    statement = statement.on_conflict_do_update(
        index_elements=[FilerCik.cik],
        set_={"priority": statement.excluded.priority},
        where=FilerCik.priority.is_distinct_from(statement.excluded.priority),
    )
    await session.execute(statement)


def _ranked(entry: InvestorEntry) -> enumerate[str]:
    """``(priority, cik)`` in list order: the last-listed CIK ranks highest."""
    return enumerate(entry.padded_ciks)
