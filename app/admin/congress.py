"""The PTR review queue: the lines a PDF reader was not sure of, and a person's call.

``ingest-house-ptrs`` keeps a line it could not read in full as
``needs_review`` (:mod:`app.db.models.congress_transaction`). Here an admin
sees each one beside the filing it is from — the member, the date, the link
to the original — and either:

- **accepts** it, as read or with corrections: it becomes ``parsed``, and the
  feed has it. A line is only accepted complete: an asset, a type, a date, and
  one of the form's amount bands.
- **rejects** it: not a trade at all (OCR read a heading, or a stamp). It
  becomes ``rejected``, kept and out of the feed.

Either way the row records who and when, and ``admin_action`` records the
change in the same transaction. A reviewed filing is never read again by a run,
so the call stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import log
from app.db.models import AdminActionKind, CongressFiling, CongressTransaction
from app.ingestion.congress.ptr import AMOUNT_BANDS, OWNERS, RowStatus, TransactionType

#: What a correction may change: the fields of the trade itself.
CORRECTABLE: Final = (
    "owner",
    "asset_name",
    "ticker",
    "asset_type",
    "transaction_type",
    "transaction_date",
    "notification_date",
    "amount_low",
    "amount_high",
    "amount_exact",
)


class ReviewError(Exception):
    """Why a review was refused: ``not_found``, ``not_in_review`` or ``validation_error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QueueRow:
    """One line, with the filing it is from."""

    row: CongressTransaction
    filing: CongressFiling


async def review_queue(
    session: AsyncSession,
    *,
    status: Literal["needs_review", "placeholder"] = "needs_review",
    chamber: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QueueRow], int]:
    """Lines in ``status``, newest filing first, and how many there are."""
    where = [CongressTransaction.parse_status == status]
    if chamber is not None:
        where.append(CongressFiling.chamber == chamber)
    joined = select(CongressTransaction, CongressFiling).join(
        CongressFiling, CongressFiling.id == CongressTransaction.filing_id
    )
    total = await session.scalar(
        select(func.count())
        .select_from(CongressTransaction)
        .join(CongressFiling, CongressFiling.id == CongressTransaction.filing_id)
        .where(*where)
    )
    rows = await session.execute(
        joined.where(*where)
        .order_by(
            CongressFiling.filing_date.desc(),
            CongressFiling.id.desc(),
            CongressTransaction.line,
        )
        .limit(limit)
        .offset(offset)
    )
    return [QueueRow(row, filing) for row, filing in rows.tuples()], total or 0


async def review(
    session: AsyncSession,
    transaction_id: int,
    *,
    decision: Literal["accept", "reject"],
    corrections: Mapping[str, Any],
    note: str | None,
    admin_id: int,
    request_id: str | None,
) -> QueueRow:
    """Settle one ``needs_review`` line. Does not commit.

    :param corrections: Fields to set before accepting, by name, from
        :data:`CORRECTABLE`; ``None`` clears one. Only with ``accept``.
    :raises ReviewError: No such line, a line not in review, or an accept that
        would leave it incomplete.
    """
    found = (
        await session.execute(
            select(CongressTransaction, CongressFiling)
            .join(CongressFiling, CongressFiling.id == CongressTransaction.filing_id)
            .where(CongressTransaction.id == transaction_id)
            .with_for_update(of=CongressTransaction)
        )
    ).first()
    if found is None:
        raise ReviewError("not_found", f"No PTR line {transaction_id}.")
    row, filing = found._tuple()
    if row.parse_status != RowStatus.NEEDS_REVIEW.value:
        raise ReviewError(
            "not_in_review", f"PTR line {transaction_id} is {row.parse_status}, not in review."
        )
    if decision == "reject" and corrections:
        raise ReviewError("validation_error", "A rejected line takes no corrections.")

    unknown = set(corrections) - set(CORRECTABLE)
    if unknown:
        raise ReviewError("validation_error", f"Not correctable: {', '.join(sorted(unknown))}.")
    for name, value in corrections.items():
        setattr(row, name, value)
    if decision == "accept":
        _check_complete(row)

    row.parse_status = RowStatus.PARSED.value if decision == "accept" else RowStatus.REJECTED.value
    row.reviewed_at = func.now()
    row.reviewed_by = admin_id
    row.review_note = note
    await log.record(
        session,
        AdminActionKind.PTR_ROW_ACCEPTED
        if decision == "accept"
        else AdminActionKind.PTR_ROW_REJECTED,
        admin_id=admin_id,
        request_id=request_id,
        detail={
            "transaction_id": row.id,
            "chamber": filing.chamber,
            "doc_id": filing.doc_id,
            "line": row.line,
            "corrected": sorted(corrections),
        },
    )
    await session.flush()
    await session.refresh(row)
    return QueueRow(row, filing)


def _check_complete(row: CongressTransaction) -> None:
    """What the ``parsed_is_complete`` CHECK demands, said as a 422 rather than a 500."""
    missing = [
        name
        for name in ("asset_name", "transaction_type", "transaction_date")
        if getattr(row, name) in (None, "")
    ]
    if row.amount_low is None and row.amount_exact is None:
        missing.append("amount_low or amount_exact")
    if missing:
        raise ReviewError(
            "validation_error", f"An accepted line needs {', '.join(missing)}: correct it first."
        )
    if row.amount_exact is not None:
        if row.amount_low is not None or row.amount_high is not None:
            raise ReviewError("validation_error", "A line has a band or an exact amount, not both.")
        if row.amount_exact <= 0:
            raise ReviewError("validation_error", "An exact amount is more than $0.")
    elif (row.amount_low, row.amount_high) not in AMOUNT_BANDS:
        raise ReviewError(
            "validation_error",
            f"${row.amount_low:,} to {_dollars(row.amount_high)} is not one of the form's bands.",
        )
    if row.owner is not None and row.owner not in OWNERS:
        raise ReviewError("validation_error", f"Owner {row.owner!r} is not SP, JT or DC.")
    if row.transaction_type not in {kind.value for kind in TransactionType}:
        raise ReviewError("validation_error", f"{row.transaction_type!r} is not a type.")
    if (
        isinstance(row.notification_date, date)
        and isinstance(row.transaction_date, date)
        and row.notification_date < row.transaction_date
    ):
        raise ReviewError(
            "validation_error", "The notification date is before the transaction date."
        )


def _dollars(amount: int | None) -> str:
    return "no limit" if amount is None else f"${amount:,}"
