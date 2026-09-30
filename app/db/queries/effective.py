"""Handles on the ``effective_filing`` views, for building queries against them.

The views themselves are defined in migration ``0006`` — see its docstring for
the rules. These are lightweight ``table()`` constructs, deliberately *not*
registered on ``Base.metadata``: a view declared there is a table as far as
autogenerate is concerned, and the next ``make revision`` would draft a
``CREATE TABLE effective_filing``.

**Every read that sums holdings per filer goes through** :data:`EFFECTIVE_FILING`.
``holding.filer_id`` and ``holding.period_of_report`` make it tempting to
group on those directly, and that answer is wrong twice over: it counts a
restated filing alongside its restatement, and it counts a filer whose old and
new entity both filed for a quarter twice::

    select(func.sum(Holding.value_usd))
    .join(EFFECTIVE_FILING, EFFECTIVE_FILING.c.filing_id == Holding.filing_id)
    .where(EFFECTIVE_FILING.c.filer_id == filer_id)

For one ``(filer, period)`` the filings themselves, rather than a join, are
:func:`resolved_filings`.
"""

from collections.abc import Sequence
from datetime import date
from typing import Final

from sqlalchemy import BigInteger, Date, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import CHAR

from app.db.models.filing import Filing

EFFECTIVE_FILING: Final = table(
    "effective_filing",
    column("filing_id", BigInteger),
    column("filer_id", BigInteger),
    column("cik", CHAR(10)),
    column("period_of_report", Date),
)
"""The filings that count for each ``(filer, period)``, after both amendments
and the filer's overlap policy. What the read path joins through."""

EFFECTIVE_FILING_BY_CIK: Final = table(
    "effective_filing_by_cik",
    column("filing_id", BigInteger),
    column("filer_id", BigInteger),
    column("cik", CHAR(10)),
    column("period_of_report", Date),
)
"""The same, per CIK, *before* the overlap policy. Only for diagnosing overlaps
— reading holdings through this double counts exactly the periods the policy
exists for."""


async def resolved_filings(
    session: AsyncSession, *, filer_id: int, period: date
) -> Sequence[Filing]:
    """The filings whose holdings make up ``filer_id``'s portfolio for ``period``.

    Either one whole-period filing — the original, or the latest restatement,
    which replaces it — alone or followed by the ``new_holdings`` amendments
    filed after it; or, while nothing whole-period is loaded, just those
    additions. Under a ``sum`` overlap policy, one such set per CIK.

    Oldest first, so within a CIK the whole-period filing leads and each
    addition follows in the order it was filed. Empty when nothing for the
    period has loaded holdings. The rules are the view's; this is only the per-period handle on it.
    """
    result = await session.scalars(
        select(Filing)
        .join(EFFECTIVE_FILING, EFFECTIVE_FILING.c.filing_id == Filing.id)
        .where(EFFECTIVE_FILING.c.filer_id == filer_id)
        .where(EFFECTIVE_FILING.c.period_of_report == period)
        .order_by(Filing.filed_at, Filing.accession_no)
    )
    return result.all()
