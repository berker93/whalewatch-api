"""The member registry in the database, and filings matched to it.

Three steps, each its own function, none committing:

:func:`store_members`
    The dataset (:mod:`app.ingestion.congress.legislators`) into
    ``congress_member``, upserted on the Bioguide id and touched only where it
    differs, and every seeded member's terms replaced.
:func:`link_filings`
    Each ``congress_filing`` with no ``filed_name_id`` linked to the
    ``congress_filed_name`` of its chamber, state and folded name, which is
    made if it is new.
:func:`match_names`
    Each filed name asked of :func:`~app.ingestion.congress.member_match.match`,
    and what it says written: the member, or the candidates for a person, or
    neither. A row is written only where the answer changed.

The index runs (``ingest-house-index``, ``ingest-senate-ptrs``) link what they
stored and match only the names that are new. ``seed-congress-members`` and
``match-congress-members`` match every name — the registry, or the matcher,
may say something new about an old one — except those a person matched, which
no run touches.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from sqlalchemy import Boolean, delete, func, insert, literal_column, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_member import (
    CandidateStatus,
    CongressFiledName,
    CongressMember,
    CongressMemberCandidate,
    CongressMemberTerm,
    NameStatus,
)
from app.ingestion.congress.legislators import FILES, Dataset, Legislator
from app.ingestion.congress.member_match import (
    Candidate,
    FiledName,
    Match,
    Member,
    Method,
    Registry,
    Tally,
    Term,
    match,
    name_key,
)

logger = get_logger(__name__)

#: Rows per INSERT: well under asyncpg's 32,767 parameters at sixteen a row.
_BATCH: Final = 1_000

#: A House ``StateDst``: a state and a two-digit district.
_STATE_DISTRICT: Final = re.compile(r"([A-Z]{2})\d{2}")

#: What a re-seed may change on a member: everything but the key.
_MEMBER_CHANGING: Final = (
    "full_name",
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "nickname",
    "other_last_names",
    "chamber",
    "state",
    "district",
    "party",
    "current",
)


# --- the registry -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MembersStored:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    terms: int = 0


async def store_members(session: AsyncSession, dataset: Dataset) -> MembersStored:
    """Upsert the dataset's people on their Bioguide id, and replace their terms.

    A member the dataset no longer lists is left as it is: filed names may
    point at them, and the dataset only ever moves people from current to
    historical.
    """
    keys = {copy.name: copy.key for copy in dataset.copies}
    rows = [_member_row(person, keys) for person in dataset.legislators]
    inserted = updated = 0
    table = CongressMember.__table__.c
    for start in range(0, len(rows), _BATCH):
        statement = pg_insert(CongressMember).values(rows[start : start + _BATCH])
        upsert = statement.on_conflict_do_update(
            index_elements=[CongressMember.bioguide_id],
            set_={name: statement.excluded[name] for name in _MEMBER_CHANGING}
            | {"source_key": statement.excluded.source_key, "updated_at": func.now()},
            where=tuple_(*(table[name] for name in _MEMBER_CHANGING)).is_distinct_from(
                tuple_(*(statement.excluded[name] for name in _MEMBER_CHANGING))
            ),
        ).returning(literal_column("xmax = 0", Boolean))
        for was_insert in (await session.scalars(upsert)).all():
            if was_insert:
                inserted += 1
            else:
                updated += 1

    ids = dict(
        (
            await session.execute(
                select(CongressMember.bioguide_id, CongressMember.id).where(
                    CongressMember.bioguide_id.in_([row["bioguide_id"] for row in rows])
                )
            )
        )
        .tuples()
        .all()
    )
    await session.execute(
        delete(CongressMemberTerm).where(CongressMemberTerm.member_id.in_(list(ids.values())))
    )
    terms = [
        {
            "member_id": ids[person.bioguide_id],
            "chamber": term.chamber,
            "start_date": term.start,
            "end_date": term.end,
            "state": term.state,
            "district": term.district,
            "party": term.party,
        }
        for person in dataset.legislators
        for term in _distinct_terms(person.terms)
    ]
    for start in range(0, len(terms), _BATCH):
        await session.execute(insert(CongressMemberTerm), terms[start : start + _BATCH])
    return MembersStored(
        inserted=inserted,
        updated=updated,
        unchanged=len(rows) - inserted - updated,
        terms=len(terms),
    )


def _member_row(person: Legislator, keys: dict[str, str]) -> dict[str, Any]:
    latest = person.latest
    return {
        "bioguide_id": person.bioguide_id,
        "full_name": person.full_name,
        "first_name": person.first_name,
        "middle_name": person.middle_name,
        "last_name": person.last_name,
        "suffix": person.suffix,
        "nickname": person.nickname,
        "other_last_names": list(person.other_last_names),
        "chamber": latest.chamber,
        "state": latest.state,
        "district": latest.district,
        "party": latest.party,
        "current": person.current,
        "source_key": keys[FILES[0] if person.current else FILES[1]],
    }


def _distinct_terms(terms: Sequence[Term]) -> list[Term]:
    """One term per chamber and start: the key. The dataset has no repeats
    since 2008, but a repeat would fail the whole seed."""
    return list({(term.chamber, term.start): term for term in terms}.values())


async def load_registry(session: AsyncSession) -> Registry:
    """Every member, with their terms, as :func:`match` takes them."""
    terms: dict[int, list[Term]] = {}
    for row in await session.execute(select(CongressMemberTerm)):
        term = row[0]
        terms.setdefault(term.member_id, []).append(
            Term(
                chamber=term.chamber,
                state=term.state,
                start=term.start_date,
                end=term.end_date,
                district=term.district,
                party=term.party,
            )
        )
    members = (await session.scalars(select(CongressMember).order_by(CongressMember.id))).all()
    return Registry(
        tuple(
            Member(
                id=member.id,
                bioguide_id=member.bioguide_id,
                first_name=member.first_name,
                last_name=member.last_name,
                middle_name=member.middle_name,
                nickname=member.nickname,
                suffix=member.suffix,
                full_name=member.full_name,
                other_last_names=tuple(member.other_last_names),
                terms=tuple(terms.get(member.id, ())),
            )
            for member in members
        )
    )


# --- linking filings to names -------------------------------------------------------


def filed_state(chamber: str, office: str | None) -> str | None:
    """The state a House ``StateDst`` names (``CA`` of ``CA11``); none otherwise."""
    if chamber != "house" or office is None:
        return None
    found = _STATE_DISTRICT.fullmatch(office.strip())
    return found.group(1) if found else None


@dataclass(frozen=True, slots=True)
class Linked:
    filings: int = 0
    """Filings given a name."""
    new_names: tuple[int, ...] = ()
    """The ids of the names made for them."""


async def link_filings(session: AsyncSession) -> Linked:
    """Link every filing with no ``filed_name_id`` to its name, making the
    names that are new. A new name is ``unmatched`` until :func:`match_names`."""
    unlinked = (
        await session.execute(
            select(
                CongressFiling.id,
                CongressFiling.chamber,
                CongressFiling.first_name,
                CongressFiling.last_name,
                CongressFiling.office,
            )
            .where(CongressFiling.filed_name_id.is_(None))
            .order_by(CongressFiling.filing_date, CongressFiling.id)
        )
    ).all()
    if not unlinked:
        return Linked()

    by_key: dict[tuple[str, str | None, str], list[int]] = {}
    spelled: dict[tuple[str, str | None, str], tuple[str | None, str]] = {}
    for filing_id, chamber, first_name, last_name, office in unlinked:
        key = (chamber, filed_state(chamber, office), name_key(first_name, last_name))
        by_key.setdefault(key, []).append(filing_id)
        spelled.setdefault(key, (first_name, last_name))  # the earliest filing's

    rows = [
        {
            "chamber": chamber,
            "state": state,
            "name_key": key,
            "first_name": spelled[(chamber, state, key)][0],
            "last_name": spelled[(chamber, state, key)][1],
            "status": NameStatus.UNMATCHED.value,
        }
        for chamber, state, key in by_key
    ]
    new: list[int] = []
    for start in range(0, len(rows), _BATCH):
        statement = pg_insert(CongressFiledName).values(rows[start : start + _BATCH])
        new.extend(
            (
                await session.scalars(
                    statement.on_conflict_do_nothing(
                        index_elements=["chamber", "state", "name_key"]
                    ).returning(CongressFiledName.id)
                )
            ).all()
        )

    ids = {
        (chamber, state, key): name_id
        for name_id, chamber, state, key in await session.execute(
            select(
                CongressFiledName.id,
                CongressFiledName.chamber,
                CongressFiledName.state,
                CongressFiledName.name_key,
            ).where(CongressFiledName.name_key.in_(sorted({key for _, _, key in by_key})))
        )
    }
    for key, filing_ids in by_key.items():
        await session.execute(
            update(CongressFiling)
            .where(CongressFiling.id.in_(filing_ids))
            .values(filed_name_id=ids[key])
        )
    return Linked(filings=len(unlinked), new_names=tuple(new))


# --- matching ------------------------------------------------------------------------


@dataclass(slots=True)
class MatchPass:
    """What one pass over the names did."""

    names: int = 0
    """Names asked: every one but a person's, or only the new."""
    tally: Tally = field(default_factory=Tally)
    changed: int = 0
    """Names whose member, status or candidates changed."""
    candidates_added: int = 0
    candidates_closed: int = 0
    registry: int = 0
    """Members in the registry. None means every name is unmatched."""


@dataclass(slots=True)
class _Name:
    row: CongressFiledName
    first_filed: date
    last_filed: date
    pending: dict[int, CongressMemberCandidate] = field(default_factory=dict)
    rejected: set[int] = field(default_factory=set)


async def match_names(
    session: AsyncSession, registry: Registry, *, only: Collection[int] | None = None
) -> MatchPass:
    """Match every filed name a person has not, or only the names ``only``.

    A name with no filings left (every one relinked to another spelling) is
    skipped: there is nothing to say it is anyone.
    """
    passed = MatchPass(registry=len(registry))
    if only is not None and not only:
        return passed
    where = [CongressFiledName.method.is_distinct_from(Method.MANUAL.value)]
    if only is not None:
        where.append(CongressFiledName.id.in_(list(only)))
    dated = (
        select(
            CongressFiling.filed_name_id.label("id"),
            func.min(CongressFiling.filing_date).label("first_filed"),
            func.max(CongressFiling.filing_date).label("last_filed"),
        )
        .group_by(CongressFiling.filed_name_id)
        .subquery()
    )
    found = await session.execute(
        select(CongressFiledName, dated.c.first_filed, dated.c.last_filed)
        .join(dated, dated.c.id == CongressFiledName.id)
        .where(*where)
        .order_by(CongressFiledName.id)
        .with_for_update(of=CongressFiledName)
    )
    names = {row.id: _Name(row, first, last) for row, first, last in found.tuples()}
    if not names:
        return passed
    for candidate in await session.scalars(
        select(CongressMemberCandidate).where(
            CongressMemberCandidate.filed_name_id.in_(list(names)),
            CongressMemberCandidate.status.in_(
                (CandidateStatus.PENDING.value, CandidateStatus.REJECTED.value)
            ),
        )
    ):
        name = names[candidate.filed_name_id]
        if candidate.status == CandidateStatus.PENDING.value:
            name.pending[candidate.member_id] = candidate
        else:
            name.rejected.add(candidate.member_id)

    for name in names.values():
        row = name.row
        found_match = match(
            FiledName(
                chamber=row.chamber,
                first_name=row.first_name,
                last_name=row.last_name,
                state=row.state,
                first_filed=name.first_filed,
                last_filed=name.last_filed,
            ),
            registry,
            rejected=name.rejected,
        )
        passed.names += 1
        passed.tally.add(found_match)
        if _apply(session, name, found_match, passed):
            passed.changed += 1
    await session.flush()
    return passed


def _apply(session: AsyncSession, name: _Name, found: Match, passed: MatchPass) -> bool:
    """Write ``found`` onto ``name``. Returns whether anything changed."""
    row = name.row
    accepted = found.accepted
    status = found.status
    member_id = accepted.member_id if accepted is not None else None
    method = accepted.method.value if accepted is not None else None
    confidence = found.confidence

    changed = False
    if (row.status, row.member_id) != (status, member_id):
        row.status, row.member_id = status, member_id
        row.matched_at = func.now()
        changed = True
    if (row.method, row.confidence) != (method, confidence):
        row.method, row.confidence = method, confidence
        changed = True

    # A matched name needs no candidates; otherwise, keep the ones still
    # suggested and close the rest.
    wanted: dict[int, Candidate] = (
        {} if accepted is not None else {c.member_id: c for c in found.candidates}
    )
    for member_id_, pending in name.pending.items():
        if member_id_ not in wanted:
            pending.status = CandidateStatus.SUPERSEDED.value
            pending.closed_at = func.now()
            passed.candidates_closed += 1
            changed = True
        elif (pending.method, pending.confidence) != (
            wanted[member_id_].method.value,
            wanted[member_id_].confidence,
        ):
            pending.method = wanted[member_id_].method.value
            pending.confidence = wanted[member_id_].confidence
            changed = True
    for member_id_, candidate in wanted.items():
        if member_id_ not in name.pending:
            session.add(
                CongressMemberCandidate(
                    filed_name_id=row.id,
                    member_id=member_id_,
                    method=candidate.method.value,
                    confidence=candidate.confidence,
                )
            )
            passed.candidates_added += 1
            changed = True
    return changed


# --- both ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberPass:
    linked: Linked
    matched: MatchPass


async def link_and_match(session: AsyncSession, *, every_name: bool = False) -> MemberPass:
    """:func:`link_filings`, then :func:`match_names` over the names it made —
    or, with ``every_name``, over every name a person has not matched."""
    linked = await link_filings(session)
    registry = await load_registry(session)
    if not len(registry):
        logger.warning("congress_members.no_registry", names=len(linked.new_names))
    matched = await match_names(session, registry, only=None if every_name else linked.new_names)
    logger.info(
        "congress_members.matched",
        filings_linked=linked.filings,
        new_names=len(linked.new_names),
        names=matched.names,
        changed=matched.changed,
        review=matched.tally.review,
        unmatched=matched.tally.unmatched,
    )
    return MemberPass(linked=linked, matched=matched)
