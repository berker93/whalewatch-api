"""The admin console's PTR review queue (:mod:`app.admin.congress`).

``GET /admin/congress/review``
    The lines a PDF reader was not sure of, newest filing first, each beside
    its filing and a link to the original. ``status=placeholder`` lists the
    filings no line could be read from instead.
``POST /admin/congress/review/{id}``
    Accept a line, as read or corrected, or reject it as not a trade.
``GET /admin/congress/coverage``
    How much of each chamber's PTRs has been read.

On the admin session, as the rest of ``/admin`` is (:mod:`app.api.routers.admin`).
"""

from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Path, Query, Request, status

from app.admin import congress
from app.api.admin_auth import AdminDep, request_id
from app.api.auth import NoStoreDep
from app.api.deps import SessionDep
from app.api.errors import INVALID, ApiError, Responses, not_found
from app.api.schemas.admin_congress import (
    AdminPtrCoverage,
    AdminPtrFiling,
    AdminPtrPage,
    AdminPtrReview,
    AdminPtrRow,
)
from app.api.schemas.error import ErrorCode, Problem
from app.db.models.congress_filing import CHAMBERS
from app.ingestion.congress.ptrs import table_coverage

router = APIRouter(prefix="/admin/congress", tags=["admin"], dependencies=[NoStoreDep])

_UNAUTHORIZED: Final[Responses] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": Problem,
        "description": "Not signed in to the console: `unauthenticated` or `session_expired`.",
    }
}
_STATUS_OF: Final = {"not_found": 404, "not_in_review": 409, "validation_error": 422}

LineId = Annotated[int, Path(ge=1, description="The line's id, from the queue.")]


def _shown(found: congress.QueueRow) -> AdminPtrRow:
    filing = found.filing
    member = " ".join(
        part for part in (filing.prefix, filing.first_name, filing.last_name, filing.suffix) if part
    )
    return AdminPtrRow.model_validate(
        {
            **{
                name: getattr(found.row, name)
                for name in AdminPtrRow.model_fields
                if name != "filing"
            },
            "filing": AdminPtrFiling(
                id=filing.id,
                chamber=filing.chamber,
                doc_id=filing.doc_id,
                member=member,
                office=filing.office,
                filing_date=filing.filing_date,
                document_url=filing.document_url,
                extraction=filing.extraction,
                pages=filing.pages,
                parse_error=filing.parse_error,
            ),
        }
    )


@router.get(
    "/review",
    operation_id="adminPtrReviewQueue",
    response_model=AdminPtrPage,
    summary="The PTR lines waiting for a person",
    responses={**_UNAUTHORIZED, **INVALID},
)
async def review_queue(
    _: AdminDep,
    session: SessionDep,
    line_status: Annotated[
        Literal["needs_review", "placeholder"],
        Query(
            alias="status",
            description=(
                "`needs_review`: lines read in part, or by OCR without confidence. "
                "`placeholder`: filings no line could be read from."
            ),
        ),
    ] = "needs_review",
    chamber: Annotated[
        Literal["house", "senate"] | None, Query(description="One chamber's only.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Lines per page.")] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000, description="How many to skip.")] = 0,
) -> AdminPtrPage:
    """Newest filing first, a filing's lines in order. Each carries what was read
    (`raw_text`), why it is here (`problems`), OCR's `confidence`, and its filing's
    `document_url`, for reading it against the original."""
    rows, total = await congress.review_queue(
        session, status=line_status, chamber=chamber, limit=limit, offset=offset
    )
    return AdminPtrPage(
        items=[_shown(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "/review/{line_id}",
    operation_id="adminPtrReview",
    response_model=AdminPtrRow,
    summary="Accept or reject a PTR line in review",
    responses={
        **_UNAUTHORIZED,
        **not_found("`not_found`: no such line."),
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": "`not_in_review`: the line is not `needs_review`, or not any more.",
        },
        **INVALID,
    },
)
async def review_line(
    line_id: LineId,
    body: AdminPtrReview,
    request: Request,
    admin: AdminDep,
    session: SessionDep,
) -> AdminPtrRow:
    """`accept` makes it `parsed`, with `corrections` applied first; it must then have
    an asset, a type, a transaction date and one of the form's amount bands, or it is
    a 422 `validation_error`. `reject` makes it `rejected`: kept, and out of the feed.
    Either is logged, and the filing is never read again over it."""
    corrections = (
        body.corrections.model_dump(exclude_unset=True) if body.corrections is not None else {}
    )
    if corrections.get("amount_exact") is not None:
        # A string on the wire, as every decimal here is: a JSON number would round it.
        corrections["amount_exact"] = Decimal(corrections["amount_exact"])
    try:
        found = await congress.review(
            session,
            line_id,
            decision=body.decision,
            corrections=corrections,
            note=body.note,
            admin_id=admin.admin.id,
            request_id=request_id(request),
        )
    except congress.ReviewError as exc:
        raise ApiError(_STATUS_OF[exc.code], ErrorCode(exc.code), exc.message) from None
    await session.commit()
    return _shown(found)


@router.get(
    "/coverage",
    operation_id="adminPtrCoverage",
    response_model=list[AdminPtrCoverage],
    summary="How much of each chamber's PTRs has been read",
    responses=_UNAUTHORIZED,
)
async def coverage(_: AdminDep, session: SessionDep) -> list[AdminPtrCoverage]:
    """Per chamber: filings tried, read by text or OCR, given a placeholder, or still
    missing a PDF; lines parsed and in review. A filing no run has tried yet is not
    counted."""
    shown: list[AdminPtrCoverage] = []
    for chamber in CHAMBERS:
        counted = await table_coverage(session, chamber)
        shown.append(
            AdminPtrCoverage(
                chamber=chamber,
                filings_seen=counted.seen,
                filings_read=counted.read,
                review_only=counted.review_only,
                by_text=counted.by_text,
                by_ocr=counted.by_ocr,
                by_html=counted.by_html,
                placeholders=counted.placeholders,
                missing=counted.missing,
                rows_parsed=counted.rows_parsed,
                rows_needs_review=counted.rows_needs_review,
                coverage_pct=counted.percent,
            )
        )
    return shown
