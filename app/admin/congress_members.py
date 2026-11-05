"""The member review queue: filed names no run would match, and a person's call.

A run matches a filed name to a member only when it is exact or normalised and
one member in office fits (:mod:`app.ingestion.congress.member_match`). Every
other name with candidates is ``review``; one without is ``unmatched``. Here an
admin sees each with its filings and candidates, and either:

- **accepts** a candidate: the name is that member.
- **assigns** a member by Bioguide id: for a name with no right candidate, or
  to correct any match, a person's included.
- **rejects** the candidates: it is none of them. The name is ``unmatched``,
  and they are never suggested for it again. A later seed may still match it
  to someone new.

An accept or assign makes the match ``manual``, which no run changes — not a
re-read of the index, not a re-seed — and which covers every filing under the
name, past and future. Each decision is logged in ``admin_action`` in the same
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import log
from app.admin.congress import ReviewError
from app.db.models import AdminActionKind
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_member import (
    CandidateStatus,
    CongressFiledName,
    CongressMember,
    CongressMemberCandidate,
    NameStatus,
)
from app.ingestion.congress.member_match import Method


@dataclass(frozen=True, slots=True)
class QueuedCandidate:
    candidate: CongressMemberCandidate
    member: CongressMember


@dataclass(frozen=True, slots=True)
class QueuedName:
    """One filed name, with what it is filed on and who it might be."""

    name: CongressFiledName
    member: CongressMember | None
    filings: int
    first_filed: date | None
    last_filed: date | None
    document_url: str | None
    """The newest filing's original, to read the name off."""
    candidates: list[QueuedCandidate]


async def name_queue(
    session: AsyncSession,
    *,
    status: Literal["review", "unmatched"] = "review",
    chamber: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QueuedName], int]:
    """Names in ``status`` with a filing, most filings first, and how many."""
    filed = (
        select(
            CongressFiling.filed_name_id.label("id"),
            func.count().label("filings"),
            func.max(CongressFiling.filing_date).label("last_filed"),
        )
        .group_by(CongressFiling.filed_name_id)
        .subquery()
    )
    where = [CongressFiledName.status == status]
    if chamber is not None:
        where.append(CongressFiledName.chamber == chamber)
    total = await session.scalar(
        select(func.count())
        .select_from(CongressFiledName)
        .join(filed, filed.c.id == CongressFiledName.id)
        .where(*where)
    )
    ids = (
        await session.scalars(
            select(CongressFiledName.id)
            .join(filed, filed.c.id == CongressFiledName.id)
            .where(*where)
            .order_by(filed.c.filings.desc(), filed.c.last_filed.desc(), CongressFiledName.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [await load_name(session, name_id) for name_id in ids], total or 0


async def load_name(session: AsyncSession, name_id: int) -> QueuedName:
    """One name as the queue shows it.

    :raises ReviewError: ``not_found``.
    """
    name = await session.get(CongressFiledName, name_id)
    if name is None:
        raise ReviewError("not_found", f"No filed name {name_id}.")
    filings, first, last = (
        await session.execute(
            select(
                func.count(),
                func.min(CongressFiling.filing_date),
                func.max(CongressFiling.filing_date),
            ).where(CongressFiling.filed_name_id == name_id)
        )
    ).one()
    document_url = await session.scalar(
        select(CongressFiling.document_url)
        .where(CongressFiling.filed_name_id == name_id)
        .order_by(CongressFiling.filing_date.desc(), CongressFiling.id.desc())
        .limit(1)
    )
    candidates = (
        await session.execute(
            select(CongressMemberCandidate, CongressMember)
            .join(CongressMember, CongressMember.id == CongressMemberCandidate.member_id)
            .where(
                CongressMemberCandidate.filed_name_id == name_id,
                CongressMemberCandidate.status == CandidateStatus.PENDING.value,
            )
            .order_by(CongressMemberCandidate.confidence.desc(), CongressMemberCandidate.id)
        )
    ).tuples()
    member = (
        await session.get(CongressMember, name.member_id) if name.member_id is not None else None
    )
    return QueuedName(
        name=name,
        member=member,
        filings=filings,
        first_filed=first,
        last_filed=last,
        document_url=document_url,
        candidates=[QueuedCandidate(candidate, who) for candidate, who in candidates],
    )


async def decide(
    session: AsyncSession,
    name_id: int,
    *,
    decision: Literal["accept", "assign", "reject"],
    candidate_id: int | None,
    bioguide_id: str | None,
    note: str | None,
    admin_id: int | None,
    request_id: str | None,
) -> QueuedName:
    """Settle one filed name. Does not commit.

    :raises ReviewError: ``not_found`` for no such name, candidate or member;
        ``not_in_review`` to accept or reject a name with no pending
        candidates; ``validation_error`` for a candidate or Bioguide id that
        does not go with the decision.
    """
    name = (
        await session.scalars(
            select(CongressFiledName).where(CongressFiledName.id == name_id).with_for_update()
        )
    ).one_or_none()
    if name is None:
        raise ReviewError("not_found", f"No filed name {name_id}.")
    pending = list(
        await session.scalars(
            select(CongressMemberCandidate)
            .where(
                CongressMemberCandidate.filed_name_id == name_id,
                CongressMemberCandidate.status == CandidateStatus.PENDING.value,
            )
            .with_for_update()
        )
    )
    if decision in ("accept", "reject") and not pending:
        raise ReviewError(
            "not_in_review",
            f"Filed name {name_id} is {name.status}, with no candidates to {decision}.",
        )
    if (decision == "accept") != (candidate_id is not None):
        raise ReviewError("validation_error", "candidate_id goes with accept, and only with it.")
    if (decision == "assign") != (bioguide_id is not None):
        raise ReviewError("validation_error", "bioguide_id goes with assign, and only with it.")

    previous = name.member_id
    member: CongressMember | None = None
    if decision == "accept":
        chosen = next((c for c in pending if c.id == candidate_id), None)
        if chosen is None:
            raise ReviewError(
                "not_found", f"No pending candidate {candidate_id} for filed name {name_id}."
            )
        member = await session.get(CongressMember, chosen.member_id)
    elif decision == "assign":
        member = (
            await session.scalars(
                select(CongressMember).where(CongressMember.bioguide_id == bioguide_id)
            )
        ).one_or_none()
        if member is None:
            raise ReviewError("not_found", f"No member with Bioguide id {bioguide_id}.")

    now = func.now()
    for candidate in pending:
        if member is None:
            closed = CandidateStatus.REJECTED
        elif candidate.member_id == member.id:
            closed = CandidateStatus.ACCEPTED
        else:
            closed = CandidateStatus.SUPERSEDED
        candidate.status, candidate.closed_at = closed.value, now

    if member is not None:
        name.status, name.member_id = NameStatus.MATCHED.value, member.id
        name.method, name.confidence = Method.MANUAL.value, 1.0
    else:
        name.status, name.member_id = NameStatus.UNMATCHED.value, None
        name.method, name.confidence = None, None
    if name.member_id != previous or decision == "reject":
        name.matched_at = now
    name.reviewed_at = now
    name.reviewed_by = admin_id
    name.review_note = note

    was = await session.get(CongressMember, previous) if previous is not None else None
    await log.record(
        session,
        AdminActionKind.MEMBER_MATCHED if member is not None else AdminActionKind.MEMBER_REJECTED,
        admin_id=admin_id,
        request_id=request_id,
        detail={
            "filed_name_id": name.id,
            "chamber": name.chamber,
            "state": name.state,
            "name_key": name.name_key,
            "decision": decision,
            "bioguide_id": member.bioguide_id if member is not None else None,
            "previous_bioguide_id": was.bioguide_id if was is not None else None,
            "rejected": [c.member_id for c in pending] if member is None else [],
        },
    )
    await session.flush()
    await session.refresh(name)
    return await load_name(session, name.id)


@dataclass(frozen=True, slots=True)
class MemberCoverage:
    """A chamber's filings by whether their filer is known."""

    matched: int = 0
    review: int = 0
    unmatched: int = 0
    unlinked: int = 0
    """Not linked to a name yet: filed before the registry was seeded."""


async def member_coverage(session: AsyncSession, chamber: str) -> MemberCoverage:
    status = func.coalesce(CongressFiledName.status, "unlinked")
    counts = dict(
        (
            await session.execute(
                select(status, func.count())
                .select_from(CongressFiling)
                .outerjoin(CongressFiledName, CongressFiledName.id == CongressFiling.filed_name_id)
                .where(CongressFiling.chamber == chamber)
                .group_by(status)
            )
        )
        .tuples()
        .all()
    )
    return MemberCoverage(
        matched=counts.get(NameStatus.MATCHED.value, 0),
        review=counts.get(NameStatus.REVIEW.value, 0),
        unmatched=counts.get(NameStatus.UNMATCHED.value, 0),
        unlinked=counts.get("unlinked", 0),
    )
