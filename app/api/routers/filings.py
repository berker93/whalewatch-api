"""Filing endpoints: one filing, by accession number.

The first read path in the service, and for a while the only one — which makes
it the window every other epic is debugged through. When a fund's total is out
by three orders of magnitude, or a position is attributed to the wrong issuer,
this is the endpoint that says whether the parse, the normalisation or the
enrichment is the thing that went wrong: it returns the holdings *and* the
decisions ingestion made about them, in one response, without a psql prompt.

That audience is why the response carries columns a public API would not:
``value_multiplier``, ``parse_status``, ``parse_notes``, ``raw_key``. None of
them are secrets — they are facts about a public document — and each one exists
precisely so that a wrong number is diagnosable from outside the container.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from pydantic import BeforeValidator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import SessionDep
from app.api.schemas.filing import FilingRead, HoldingRead
from app.core.accession import ACCESSION_SHAPE, normalise_accession
from app.db.models import Filer, Filing, Holding, Security

router = APIRouter(tags=["filings"])


def _accession_path_param(value: str) -> str:
    """Accept either spelling in the URL; query with the one the column holds.

    A ``BeforeValidator`` rather than a ``replace`` inside the handler, so the
    normalisation happens once, in front of every use of the value — the query,
    the 404 message, the log line — and cannot be skipped by a handler added
    later. Raising ``ValueError`` here is what turns a malformed accession into
    a 422 naming the shape expected, instead of a 404 that says the filing is
    not in the database when the truth is that nothing was ever looked up.
    """
    return normalise_accession(value)


AccessionNo = Annotated[
    str,
    BeforeValidator(_accession_path_param),
    Path(
        description=(
            f"EDGAR accession number, with or without dashes: `{ACCESSION_SHAPE}`. "
            "Both spellings are in circulation — dashed in EDGAR's indexes, "
            "undashed in archive URLs — and both reach the same filing."
        ),
        examples=["0001067983-24-000011"],
    ),
]

IncludeOptions = Annotated[
    bool,
    Query(
        description=(
            "Set `false` to drop option lines (`put_call` is not null). Their "
            "`value_usd` is the notional value of the underlying rather than a "
            "premium, so a portfolio total that includes them is inflated by the "
            "whole exposure."
        )
    ),
]


@router.get(
    "/filings/{accession_no}",
    response_model=FilingRead,
    summary="One filing, with its holdings",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No filing with that accession number."},
    },
)
async def read_filing(
    accession_no: AccessionNo,
    session: SessionDep,
    include_options: IncludeOptions = True,
) -> FilingRead:
    """Read a filing and every position it reports, largest first.

    Two queries rather than one: a filing joined to its holdings repeats every
    filing column on every row, and a large 13F is three thousand rows. The
    second query is a lookup on ``ix_holding_filing_id``.
    """
    amended = aliased(Filing)
    row = (
        await session.execute(
            select(
                Filing,
                Filer.name.label("filer_name"),
                Filer.slug.label("filer_slug"),
                amended.accession_no.label("amends_accession_no"),
            )
            # Outer, both of them: filer_id is null until the CIK is resolved,
            # and amends_id is null for everything that is not an amendment.
            # An inner join here would turn "not yet resolved" into a 404.
            .outerjoin(Filer, Filer.id == Filing.filer_id)
            .outerjoin(amended, amended.id == Filing.amends_id)
            .where(Filing.accession_no == accession_no)
        )
    ).one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            # Says what to do about it. The overwhelmingly likely cause is that
            # nothing has ingested this filing yet, not that it does not exist —
            # so the message names the command that would fix that rather than
            # leaving the reader to wonder whether they mistyped.
            detail=(
                f"No filing {accession_no}. If it exists on EDGAR it has not been "
                f"ingested: python -m app.cli ingest-filing {accession_no} --cik <cik>"
            ),
        )

    holdings = await _read_holdings(
        session, filing_id=row.Filing.id, include_options=include_options
    )

    # Validated from the ORM object, then the four joined columns and the
    # holdings put on top. model_copy skips validation, which is safe for
    # values that came out of typed columns, and means a column added to Filing
    # and to FilingRead needs no third edit here.
    return FilingRead.model_validate(row.Filing).model_copy(
        update={
            "filer_name": row.filer_name,
            "filer_slug": row.filer_slug,
            "amends_accession_no": row.amends_accession_no,
            "holdings": holdings,
        }
    )


async def _read_holdings(
    session: AsyncSession, *, filing_id: int, include_options: bool
) -> list[HoldingRead]:
    """The filing's positions, biggest first, joined to what we resolved them to."""
    statement = (
        select(
            Holding.cusip,
            Security.name.label("issuer_name"),
            Security.ticker,
            Holding.value_usd,
            Holding.shares,
            Holding.sshprnamt_type,
            Holding.put_call,
            Holding.investment_discretion,
            Holding.voting_sole,
            Holding.voting_shared,
            Holding.voting_none,
        )
        # Inner: security_id is NOT NULL and the FK is RESTRICT, so a holding
        # without a security cannot exist. If one ever did, losing it here would
        # be a silently short portfolio — worth remembering if that column is
        # ever made nullable.
        .join(Security, Security.id == Holding.security_id)
        .where(Holding.filing_id == filing_id)
        # Value descending is the ordering anyone reading a portfolio wants. The
        # rest of the holding's natural key follows it so that two requests for
        # the same filing return the same order: positions tie at the same value
        # more often than you would guess, and an unordered tie-break makes a
        # response diff that is pure noise.
        .order_by(
            Holding.value_usd.desc(),
            Holding.cusip,
            Holding.put_call,
            Holding.sshprnamt_type,
        )
    )
    if not include_options:
        statement = statement.where(Holding.put_call.is_(None))

    rows = await session.execute(statement)
    return [HoldingRead.model_validate(holding) for holding in rows]
