r"""``GET /v1/search``: investors and stocks by name or ticker, for the ⌘K palette.

Ranking
-------
Every match is scored, and each group is sorted by its score:

=======  ==============================================================
100      the ticker is ``q`` (stocks only, ignoring case)
90       the ticker starts with ``q``
80       a name starts with ``q``: the issuer's, the investor's or the manager's
0 to 70  otherwise, 70 times how alike ``q`` is to the most alike column
=======  ==============================================================

Ties, which are common among the prefix matches, go to the most dollars held
in the latest published period: a stock's across every tracked filer, an
investor's own portfolio. That puts APPLE INC above Apple Hospitality REIT for
``apple``, and above the two other CUSIPs named APPLE INC, which nobody held
in the latest period.

How alike
---------
``pg_trgm`` has three measures, and on the dev database's names each ranks some
search wrong on its own:

- ``similarity``, the whole name against ``q``, punishes a long name for its
  other words. ``square`` is 0.20 like Pershing Square Capital Management,
  under the 0.3 threshold, so the fund is not found at all.
- ``word_similarity``, ``q`` against the closest stretch of the name, scores
  ``microsft`` 0.67 like MICROSOFT CORP, ALLEGRO MICROSYSTEMS and MICROSTRATEGY
  alike, and ``aple`` higher like MAPLE than like APPLE.
- ``strict_word_similarity``, ``q`` against whole words, ranks those two right
  but finds a half-typed word less like its word than like a shorter one:
  ``hath`` is 0.5 like BLUE HAT and 0.4 like BERKSHIRE HATHAWAY.

The score is the mean of the last two, which orders all of those the way a
person would: MICROSOFT 0.63 to 0.53, APPLE 0.57 to 0.49, HATHAWAY 0.60 to
0.55. Where ``q`` is a whole word of the name, or whole words, both are 1, and
that is decided by a regular expression instead (below).

Matching is not scoring
-----------------------
A row is a candidate only through an operator a GIN trigram index can answer:
``ILIKE 'q%'`` for the prefixes, and for the rest ``q <% column`` or
``q <<% column`` (written ``column %> q``, see :func:`_fuzzy`),
``word_similarity`` and ``strict_word_similarity`` at the extension's
thresholds of 0.6 and 0.5. The scoring functions are only ever applied to
those candidates. Called in the ``WHERE`` clause, a function would be applied
to every row in the table.

Not ``column % q``, plain ``similarity`` at 0.3, though that is the usual
operator. For fifteen misspellings and partial names tried on the dev
database, it found nothing the other two did not. And for a short word it
shares too few trigrams to narrow anything: the index hands back 6,000
candidates for ``corp``, a quarter of them to be thrown away by computing
``similarity`` for each.

A score of 70 or less is always below a prefix match, so the fuzzy matches
are a second query, run only when the prefix matches leave the group short of
``limit``. That leaves the whole common words. 6,557 names have INC in them,
and only nine start with it, so at ``limit=10`` the second query has 6,800
candidates, and two trigram calls each for the score. But nearly all of them
score 70, the most a fuzzy match can, and a regular expression for ``q`` as
whole words (``~* '\minc\M'``) says so for a fraction of the cost. The
trigram calls are made only where it does not match. With that and
:func:`_fuzzy`'s operators, the whole request for ``inc`` went from 48 ms to
28 ms on the dev database, and it is the slowest search found: most take 4 ms
to 6 ms. docs/query-performance.md has the measurements.

A query of two characters has too few trigrams to compare usefully, and is
matched by its prefixes only.
"""

import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from pydantic import StringConstraints
from sqlalchemy import (
    ColumnElement,
    Float,
    Integer,
    Row,
    Select,
    and_,
    case,
    false,
    func,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from app.api.cache import Lifetime, cached
from app.api.deps import SessionDep
from app.api.errors import INVALID
from app.api.routers.investors import _escape_like
from app.api.schemas.search import InvestorMatch, SearchResults, StockMatch
from app.db.models import Filer, Security
from app.derived.views import CONSENSUS_HOLDINGS, FILER_SUMMARY

router = APIRouter(tags=["search"])

#: Shorter, and nearly every name would start with it.
MIN_QUERY: Final = 2
MAX_QUERY: Final = 100
DEFAULT_LIMIT: Final = 5
MAX_LIMIT: Final = 20
#: Below this, ``q`` has too few trigrams to be alike anything in particular.
_MIN_FUZZY: Final = 3

EXACT_TICKER: Final = 100
TICKER_PREFIX: Final = 90
NAME_PREFIX: Final = 80
FUZZY_MAX: Final = 70


def _constant(value: int | float) -> ColumnElement[Any]:
    """``value`` written into the SQL rather than bound. A bound constant in a
    ``CASE`` has no type for Postgres to infer when the statement is sent as
    text, as ``make explain`` sends it."""
    return literal_column(repr(value), Integer if isinstance(value, int) else Float)


_DISPLAY_NAME: Final = func.coalesce(Filer.display_name, Filer.name)

#: A searched column.
Column = QueryableAttribute[str | None]


def _starts(column: Column, q: str) -> ColumnElement[bool]:
    """``column ILIKE 'q%'``, with ``q`` taken literally. Served by the
    column's trigram index, under a generic plan too, where a B-tree's
    ``text_pattern_ops`` cannot match a bound pattern."""
    return column.ilike(f"{_escape_like(q)}%", escape="\\")


def _not_starts(column: Column, q: str) -> ColumnElement[bool]:
    """Not :func:`_starts`, counting a null column as not starting with it."""
    return not_(func.coalesce(_starts(column, q), false()))


def _whole_words(q: str) -> str:
    """A regular expression for ``q`` as whole words of a name, whatever the
    space between them."""
    return r"\m" + r"\s+".join(re.escape(word) for word in q.split()) + r"\M"


def _alike(column: Column, q: str) -> ColumnElement[float]:
    """``q`` against ``column``, 0 to 1. Null when the column is.

    1 where ``q`` is whole words of it, which is also what the trigram
    measures would say, and is all they would say for most candidates of a
    common word.
    """
    return case(
        (column.regexp_match(_whole_words(q), flags="i"), _constant(1.0)),
        else_=(
            func.word_similarity(q, column, type_=Float)
            + func.strict_word_similarity(q, column, type_=Float)
        )
        * _constant(0.5),
    )


def _fuzzy(column: Column, q: str) -> ColumnElement[bool]:
    """Whether ``column`` is like ``q``, by the operators the index answers.

    ``column %> q`` rather than the ``q <% column`` it means, which is the way
    round the index takes it. Written the other way, Postgres commutes it for
    the index and then cannot tell the two are the same condition, so it
    checks it twice for each row: once as the index's recheck, once as a
    filter. On ``inc``, that is 33 ms instead of 16. ``%>`` first: on a
    common word it is true for most candidates, and ``OR`` then skips the
    second.
    """
    return or_(
        column.op("%>", is_comparison=True)(q),
        column.op("%>>", is_comparison=True)(q),
    )


# --- investors ------------------------------------------------------------------


def _investors(
    score: ColumnElement[Any] | None, where: ColumnElement[bool], limit: int
) -> Select[Any]:
    """``score`` is None where every row has the same one: a constant in an
    ``ORDER BY`` would be read as a column number."""
    summary = FILER_SUMMARY
    # One index probe per matching filer, and there are a hundred filers.
    portfolio_value = (
        select(summary.c.portfolio_value_usd)
        .where(summary.c.filer_id == Filer.id)
        .order_by(summary.c.period_of_report.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(
            Filer.slug,
            _DISPLAY_NAME.label("display_name"),
            Filer.manager_name,
            Filer.category,
        )
        .where(where)
        .order_by(
            *([score.desc()] if score is not None else []),
            func.coalesce(portfolio_value, -1).desc(),
            _DISPLAY_NAME,
            Filer.slug,
        )
        .limit(limit)
    )


def investor_prefix_query(q: str, limit: int) -> Select[Any]:
    """Investors whose name or manager's name starts with ``q``, all scoring
    :data:`NAME_PREFIX`."""
    return _investors(
        None,
        or_(_starts(Filer.display_name, q), _starts(Filer.manager_name, q)),
        limit,
    )


def investor_fuzzy_query(q: str, limit: int) -> Select[Any]:
    """Investors like ``q`` that :func:`investor_prefix_query` does not return."""
    return _investors(
        _constant(FUZZY_MAX)
        * func.greatest(_alike(Filer.display_name, q), _alike(Filer.manager_name, q)),
        and_(
            or_(_fuzzy(Filer.display_name, q), _fuzzy(Filer.manager_name, q)),
            _not_starts(Filer.display_name, q),
            _not_starts(Filer.manager_name, q),
        ),
        limit,
    )


# --- stocks ---------------------------------------------------------------------


def _securities(score: ColumnElement[Any], where: ColumnElement[bool], limit: int) -> Select[Any]:
    consensus = CONSENSUS_HOLDINGS
    latest = select(func.max(consensus.c.period_of_report)).scalar_subquery()
    return (
        select(Security.cusip, Security.ticker, Security.name)
        .outerjoin(
            consensus,
            and_(
                consensus.c.security_id == Security.id,
                consensus.c.period_of_report == latest,
            ),
        )
        .where(where)
        .order_by(
            score.desc(),
            func.coalesce(consensus.c.total_value_usd, -1).desc(),
            Security.id,
        )
        .limit(limit)
    )


def security_prefix_query(q: str, limit: int) -> Select[Any]:
    """Stocks whose ticker is ``q``, or whose ticker or name starts with it."""
    score = case(
        (func.upper(Security.ticker) == q.upper(), _constant(EXACT_TICKER)),
        (_starts(Security.ticker, q), _constant(TICKER_PREFIX)),
        else_=_constant(NAME_PREFIX),
    )
    return _securities(score, or_(_starts(Security.ticker, q), _starts(Security.name, q)), limit)


def security_fuzzy_query(q: str, limit: int) -> Select[Any]:
    """Stocks like ``q`` that :func:`security_prefix_query` does not return."""
    return _securities(
        _constant(FUZZY_MAX) * func.greatest(_alike(Security.name, q), _alike(Security.ticker, q)),
        and_(
            or_(_fuzzy(Security.name, q), _fuzzy(Security.ticker, q)),
            _not_starts(Security.ticker, q),
            _not_starts(Security.name, q),
        ),
        limit,
    )


async def _matches(
    session: AsyncSession, q: str, limit: int, prefix: Any, fuzzy: Any
) -> list[Row[Any]]:
    """The prefix matches, then as many fuzzy ones as there is room for.

    In that order, with nothing to merge: every prefix score is above every
    fuzzy one, and the fuzzy query leaves the prefix matches out.
    """
    rows = list(await session.execute(prefix(q, limit)))
    if len(rows) < limit and len(q) >= _MIN_FUZZY:
        rows += await session.execute(fuzzy(q, limit - len(rows)))
    return rows


QueryParam = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=MIN_QUERY, max_length=MAX_QUERY),
    Query(
        description=(
            f"A ticker, or part of a stock's, an investor's or a manager's name. At "
            f"least {MIN_QUERY} characters once trimmed; from {_MIN_FUZZY}, misspellings "
            "and words inside a name match too."
        ),
        examples=["berkshire"],
    ),
]
LimitParam = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_LIMIT,
        description=(f"Most results in each group. Above {MAX_LIMIT} is refused, not cut down."),
    ),
]


@router.get(
    "/search",
    operation_id="search",
    response_model=SearchResults,
    summary="Investors and stocks by name or ticker",
    responses=INVALID,
)
@cached(Lifetime.SEARCH)
async def search(
    session: SessionDep, q: QueryParam, limit: LimitParam = DEFAULT_LIMIT
) -> SearchResults:
    """Best first in each group. Not paged: a palette shows the top few, and
    whoever wants more types more."""
    investors = await _matches(session, q, limit, investor_prefix_query, investor_fuzzy_query)
    securities = await _matches(session, q, limit, security_prefix_query, security_fuzzy_query)
    return SearchResults(
        query=q,
        investors=[
            InvestorMatch(
                slug=row.slug,
                display_name=row.display_name,
                manager_name=row.manager_name,
                category=row.category,
            )
            for row in investors
        ],
        securities=[
            StockMatch(cusip=row.cusip, ticker=row.ticker, issuer_name=row.name)
            for row in securities
        ],
    )
