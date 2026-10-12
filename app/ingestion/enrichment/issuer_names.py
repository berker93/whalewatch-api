"""An issuer name for a CUSIP: OpenFIGI's, else the SEC's 13(f) list's.

OpenFIGI's name comes with a ticker and a FIGI, so when it has a match its name
is the one to take. When it has none — a delisted name, an obscure convert, a
CUSIP it has simply never seen — the SEC's Official List of Section 13(f)
Securities usually still has the CUSIP, under the name the SEC gives the
issuer. That name is what lets an unresolved security render as
``ACME CORP · 037833100`` rather than as nine characters alone::

    async with session_scope(settings) as session:
        names = await issuer_names(session, figi_results)

In that order, and only those two. The name the filing wrote
(``security.name``) is the caller's last resort, not this module's: a caller
that has it already does not need a function to return it.

Which list's name
-----------------
The latest quarter's list that has the CUSIP, whatever its status there. A
security deleted from the list in 2026Q1 is still named by the 2026Q1 list,
and that is the newest thing the SEC has said about it. The list prints names
in upper case and cuts them at 28 characters (``AGRICULTURE & NAT SOL ACQ CO``);
they are used as printed.

Nothing writes the result yet. This is the fallback the resolution step that
fills ``security`` is meant to use, not a read path the API takes today.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.sec_13f_security import ListStatus, Sec13FSecurity
from app.ingestion.enrichment.figi import FIGIResult


class IssuerNameSource(StrEnum):
    OPENFIGI = "openfigi"
    SEC_13F_LIST = "13f_list"


@dataclass(frozen=True, slots=True)
class ListedName:
    """A CUSIP's entry on the latest 13(f) list that has it."""

    cusip: str
    issuer_name: str
    period_of_report: date
    status: ListStatus


@dataclass(frozen=True, slots=True)
class IssuerName:
    """A name for one CUSIP, and where it came from."""

    name: str
    source: IssuerNameSource
    period_of_report: date | None = None
    """For a name from the 13(f) list, which quarter's list."""


async def latest_listed_names(
    session: AsyncSession, cusips: Iterable[str]
) -> dict[str, ListedName]:
    """The latest 13(f) list entry for each of ``cusips`` that any list has."""
    wanted = list(dict.fromkeys(cusips))
    if not wanted:
        return {}
    table = Sec13FSecurity
    statement = (
        select(table.cusip, table.issuer_name, table.period_of_report, table.status)
        .where(table.cusip.in_(wanted))
        .distinct(table.cusip)
        .order_by(table.cusip, table.period_of_report.desc())
    )
    rows = (await session.execute(statement)).all()
    return {
        row.cusip: ListedName(
            row.cusip, row.issuer_name, row.period_of_report, ListStatus(row.status)
        )
        for row in rows
    }


def issuer_name(figi: FIGIResult | None, listed: ListedName | None) -> IssuerName | None:
    """OpenFIGI's name if it matched with one, else the 13(f) list's, else ``None``.

    OpenFIGI's is the first match that carries a name. The matches are usually
    one listing per exchange, and they name the same issuer.
    """
    if figi is not None and figi.found:
        for match in figi.matches:
            if match.name and match.name.strip():
                return IssuerName(match.name.strip(), IssuerNameSource.OPENFIGI)
    if listed is not None:
        return IssuerName(
            listed.issuer_name, IssuerNameSource.SEC_13F_LIST, listed.period_of_report
        )
    return None


async def issuer_names(
    session: AsyncSession,
    figi_results: Mapping[str, FIGIResult | None],
) -> dict[str, IssuerName]:
    """:func:`issuer_name` for every CUSIP in ``figi_results`` that gets one.

    ``figi_results`` is keyed by CUSIP, as
    :meth:`~app.ingestion.enrichment.figi.OpenFIGIClient.map_cusips` returns
    it; ``None`` for a CUSIP OpenFIGI was not asked about. The list is read
    only for the CUSIPs OpenFIGI could not name.
    """
    named = {
        cusip: name
        for cusip, result in figi_results.items()
        if (name := issuer_name(result, None)) is not None
    }
    listed = await latest_listed_names(session, (c for c in figi_results if c not in named))
    for cusip, entry in listed.items():
        named[cusip] = IssuerName(
            entry.issuer_name, IssuerNameSource.SEC_13F_LIST, entry.period_of_report
        )
    # In the order asked, as map_cusips returns its results.
    return {cusip: named[cusip] for cusip in figi_results if cusip in named}
