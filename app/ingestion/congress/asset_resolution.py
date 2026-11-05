"""Resolving what each PTR line traded to a ``security``: SEC-3 again, from free text.

::

    async with sessions.begin() as session:
        passed = await resolve_assets(session)

Against our own ``security`` table, not a ticker list: it has the CUSIP, the
ticker, the issuer's name and the ticker history (``security_alias``), and a
congressional trade resolved here is the same ``security_id`` a 13F holding
of the stock is. That is what lets a stock's page put the two side by side.

In order, for each line (:class:`Resolver`):

1. **An override** for its asset key (``congress_asset_override``): a
   person's class and security, ``manual``. Nothing below overrides it.
2. **The class** (:func:`~app.ingestion.congress.assets.classify`). A
   municipal bond, a Treasury, a mutual fund, crypto, real estate and the
   rest are classified and looked up no further: they have no security, and
   no stock view shows them.
3. **The ticker**, where the line gives one or its name has one
   (:func:`~app.ingestion.congress.assets.extract_ticker`), read as the stock
   page reads one (:func:`~app.db.queries.tickers.lookup_query`): a ticker the
   security traded under on the trade's day, its current ticker, a CUSIP, or
   one it traded under at another time.

   A ticker the filer gave, current on the day, is taken whatever the name
   says — ``GE`` for ``GE Aerospace`` (``GENERAL ELECTRIC``), ``WAB`` for
   ``Westinghouse Air Brake`` (``WABTEC CORP``), an ETF under a name its
   broker abbreviated past recognition — at a lower confidence if the name
   does not agree (:data:`TICKER_AGREES`), and unless the name finds another
   security outright (:data:`NAME_OVERRULES`). A ticker of another time, or
   one read off the end of a scan's name, counts only if the name agrees: it
   may be someone else's now, or no ticker at all.
4. **The name**: the issuer's, by trigram, against both ``security.name`` and
   ``security.filed_name`` — ``pg_trgm`` finds candidates, and
   :func:`~app.ingestion.congress.assets.name_similarity` scores them with the
   share words and legal forms out on both sides. Taken at
   :data:`NAME_ACCEPT` or more. Among equals, a security still listed on the
   trade's day wins, then the one 13F filers held the most of in that year and
   the one before: the stock rather than its convertible or the option CUSIP a
   filer gave it, and the CUSIP of the day after a CUSIP change. A name match
   to a security with another ticker than the filer's — a foreign ordinary
   share's to its ADR — is :data:`OTHER_LISTING` at most.

A line none of these resolves keeps its class and its description as filed:
``asset_name`` and ``ticker`` are never touched. A stock's line with no
security is ``equity`` and unresolved; a scan's line no lookup could place is
``unknown``.

What runs again
---------------
Lines not resolved yet, and those of a resolvable class with no security: a
security that gets its ticker later, or a newly listed stock, then resolves
on the next run. With ``force``, every line. Setting or removing an override
resolves its key's lines at once.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.congress_asset_override import CongressAssetOverride
from app.db.models.congress_filing import CongressFiling
from app.db.models.congress_transaction import CongressTransaction
from app.db.models.security import Security
from app.db.queries.tickers import lookup_query
from app.ingestion.congress.assets import (
    RESOLVABLE,
    AssetClass,
    Classified,
    Method,
    asset_key,
    classify,
    core_name,
    extract_ticker,
    name_similarity,
)
from app.ingestion.congress.ptr import RowStatus

#: How far a ticker's security's name must agree with the line's for full
#: confidence, and for a ticker of another time to count at all. Low: on the
#: 2026 House lines every ticker that hit under 0.45 was right, ETFs above
#: all (``SPYV`` for ``SS SPDR P S&P 500 VALUE ETF``).
TICKER_AGREES: Final = 0.35

#: The same for a ticker read off the end of a scan's name. The lowest of the
#: right ones in 2026 was 0.297 (``SDY``, ``State Street Spdr``).
HINT_AGREES: Final = 0.25

#: The lowest name similarity taken, with no ticker to go on. Between 0.70
#: and 0.75 on the 2026 lines, about half were wrong (a 3-7 year Treasury ETF
#: as the 1-3 year one); above, a handful in two hundred.
NAME_ACCEPT: Final = 0.75

#: A name match this close to another security overrules a disagreeing ticker.
NAME_OVERRULES: Final = 0.9

#: Confidences, by how the security was found. A name match's is its
#: similarity, at most :data:`NAME_CEILING`.
ON_THE_DAY: Final = Decimal("1.000")
"""A ticker it had on the day, its current one, or its CUSIP, and the name agrees."""
FORMER: Final = Decimal("0.900")
"""A ticker it had at another time, or one read off the end of a scan's name."""
DISAGREEING: Final = Decimal("0.800")
"""The filer's ticker, current on the day, whose security's name does not agree."""
NAME_CEILING: Final = Decimal("0.950")
OTHER_LISTING: Final = Decimal("0.800")
"""A name match to a security with a ticker, which is not the filer's."""

#: Candidates ``pg_trgm`` hands the scoring, and the word similarity it
#: finds them at: loose, since the scoring is what decides.
CANDIDATES: Final = 20
WORD_FLOOR: Final = 0.5

#: Lines read and written per statement.
_BATCH: Final = 1_000

#: The lines that are trades, or may be: not a placeholder, not rejected.
_LINES: Final = (RowStatus.PARSED.value, RowStatus.NEEDS_REVIEW.value)

_CLASS_LETTER: Final = re.compile(
    r"\b(?:CLASS|CL|SERIES|SER)\s*([A-C])\b|\bCL([ABC])\b|-\s*(?:CL\s*)?([ABC])\s*$",
    re.IGNORECASE,
)


def share_class(name: str | None) -> str | None:
    """``A`` for ``Alphabet Inc. - Class A Common Stock`` and ``ALPHABET INC-CL A``."""
    if not name:
        return None
    match = _CLASS_LETTER.search(name)
    return None if match is None else next(group for group in match.groups() if group).upper()


@dataclass(frozen=True, slots=True)
class Line:
    """What resolution reads of a line."""

    id: int
    asset_type: str | None
    asset_name: str | None
    ticker: str | None
    description: str | None
    on: date
    """The trade's day, else the filing's: the date a ticker is read on."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a line traded."""

    asset_class: AssetClass
    security_id: int | None
    method: Method | None
    confidence: Decimal | None
    key: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A security a name may be."""

    security_id: int
    cusip: str
    ticker: str | None
    name: str | None
    filed_name: str | None
    delisted_by: date | None
    held: Decimal
    """Its value across 13F filers' positions in the trade's year and the one
    before, every quarter added."""
    score: float
    """:func:`~app.ingestion.congress.assets.name_similarity` to the line's name."""

    def listed_on(self, on: date) -> bool:
        return self.delisted_by is None or self.delisted_by > on


@dataclass(frozen=True, slots=True)
class _Retrieved:
    """A candidate as ``pg_trgm`` found it, for any line with that core name."""

    security_id: int
    cusip: str
    ticker: str | None
    name: str | None
    filed_name: str | None
    delisted_by: date | None
    held_by_year: dict[int, Decimal]


@dataclass(frozen=True, slots=True)
class _Found:
    security_id: int
    method: Method
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class _TickerHit:
    security_id: int
    via: int
    """:func:`~app.db.queries.tickers.lookup_query`'s tier: 0 on the day, 1
    current, 2 another time, 3 CUSIP."""
    name: str | None
    filed_name: str | None


@dataclass(frozen=True, slots=True)
class Override:
    asset_class: AssetClass
    security_id: int | None


_CANDIDATES: Final = text(
    """
    SELECT s.id, s.cusip, s.ticker, s.name, s.filed_name, s.delisted_by
    FROM security s
    WHERE s.name %> :q OR s.filed_name %> :q
    ORDER BY greatest(word_similarity(:q, s.name), word_similarity(:q, s.filed_name)) DESC,
             greatest(similarity(:q, s.name), similarity(:q, s.filed_name)) DESC,
             s.id DESC
    LIMIT :limit
    """
)

_HELD: Final = text(
    """
    SELECT security_id, extract(year FROM period_of_report)::int AS year, sum(value_usd) AS held
    FROM position_snapshot
    WHERE security_id = ANY(:ids)
    GROUP BY 1, 2
    """
)


class Resolver:
    """Resolves lines one at a time, remembering each ticker and name it looked
    up for the length of a pass: a pass sees ``MICROSOFT CORP`` a few hundred
    times."""

    def __init__(self, session: AsyncSession, overrides: dict[str, Override]) -> None:
        self._session = session
        self._overrides = overrides
        self._tickers: dict[tuple[str, date], _TickerHit | None] = {}
        self._names: dict[str, list[_Retrieved]] = {}
        self._threshold_set = False

    @classmethod
    async def load(cls, session: AsyncSession) -> Resolver:
        rows = await session.execute(
            select(
                CongressAssetOverride.asset_key,
                CongressAssetOverride.asset_class,
                CongressAssetOverride.security_id,
            )
        )
        return cls(
            session,
            {row.asset_key: Override(AssetClass(row.asset_class), row.security_id) for row in rows},
        )

    async def resolve(self, line: Line) -> Resolution:
        key = asset_key(line.asset_type, line.ticker, line.asset_name)
        override = self._overrides.get(key)
        if override is not None:
            return Resolution(
                override.asset_class,
                override.security_id,
                Method.MANUAL,
                None if override.security_id is None else ON_THE_DAY,
                key,
            )
        classified = classify(line.asset_type, line.asset_name, line.ticker, line.description)
        if not classified.lookup:
            return Resolution(classified.asset_class, None, None, None, key)
        found = await self._find(line, classified)
        if found is None:
            return Resolution(classified.otherwise, None, None, None, key)
        return Resolution(
            classified.asset_class, found.security_id, found.method, found.confidence, key
        )

    async def _find(self, line: Line, classified: Classified) -> _Found | None:
        disagreeing: _TickerHit | None = None
        ticker = extract_ticker(line.ticker, line.asset_name)
        if ticker is not None:
            hit = await self._ticker(ticker.ticker, line.on)
            if hit is not None:
                agreement = (
                    1.0
                    if line.asset_name is None
                    else name_similarity(line.asset_name, hit.name, hit.filed_name)
                )
                if ticker.hinted or hit.via == 2:
                    if agreement >= (HINT_AGREES if ticker.hinted else TICKER_AGREES):
                        method = Method.FORMER_TICKER if hit.via == 2 else Method.TICKER
                        return _Found(hit.security_id, method, FORMER)
                elif agreement >= TICKER_AGREES:
                    return _Found(hit.security_id, Method.TICKER, ON_THE_DAY)
                else:
                    disagreeing = hit
        best = None
        if classified.by_name and line.asset_name:
            best = await self.best_candidate(line.asset_name, line.on)
        if disagreeing is not None:
            overruled = (
                best is not None
                and best.score >= NAME_OVERRULES
                and best.security_id != disagreeing.security_id
            )
            if best is not None and overruled:
                return _Found(best.security_id, Method.NAME, _name_confidence(best, None))
            return _Found(disagreeing.security_id, Method.TICKER, DISAGREEING)
        if best is not None and best.score >= NAME_ACCEPT:
            given = None if ticker is None or ticker.hinted else ticker.ticker
            return _Found(best.security_id, Method.NAME, _name_confidence(best, given))
        return None

    async def _ticker(self, ticker: str, on: date) -> _TickerHit | None:
        cached = (ticker, on)
        if cached not in self._tickers:
            row = (
                await self._session.execute(lookup_query(ticker, cusip=len(ticker) == 9, on=on))
            ).first()
            hit = None
            if row is not None:
                filed_name = await self._session.scalar(
                    select(Security.filed_name).where(Security.id == row.id)
                )
                hit = _TickerHit(row.id, row.via, row.name, filed_name)
            self._tickers[cached] = hit
        return self._tickers[cached]

    async def _retrieve(self, core: str) -> list[_Retrieved]:
        if core not in self._names:
            if not self._threshold_set:
                # Local to the transaction, as everything here runs in one.
                await self._session.execute(
                    text("SELECT set_config('pg_trgm.word_similarity_threshold', :floor, true)"),
                    {"floor": str(WORD_FLOOR)},
                )
                self._threshold_set = True
            rows = (
                await self._session.execute(_CANDIDATES, {"q": core, "limit": CANDIDATES})
            ).all()
            held: dict[int, dict[int, Decimal]] = {row.id: {} for row in rows}
            if rows:
                for found in await self._session.execute(_HELD, {"ids": list(held)}):
                    held[found.security_id][found.year] = found.held
            self._names[core] = [
                _Retrieved(
                    security_id=row.id,
                    cusip=row.cusip,
                    ticker=row.ticker,
                    name=row.name,
                    filed_name=row.filed_name,
                    delisted_by=row.delisted_by,
                    held_by_year=held[row.id],
                )
                for row in rows
            ]
        return self._names[core]

    async def candidates(self, asset_name: str, on: date) -> list[Candidate]:
        """The securities ``asset_name`` may be on ``on``, best first; see the
        module docstring."""
        core = core_name(asset_name)
        if len(core.replace(" ", "")) < 3:
            return []
        wanted = share_class(asset_name)
        scored = [
            Candidate(
                security_id=found.security_id,
                cusip=found.cusip,
                ticker=found.ticker,
                name=found.name,
                filed_name=found.filed_name,
                delisted_by=found.delisted_by,
                held=sum(
                    (found.held_by_year.get(year, Decimal(0)) for year in (on.year - 1, on.year)),
                    Decimal(0),
                ),
                score=name_similarity(asset_name, found.name, found.filed_name),
            )
            for found in await self._retrieve(core)
            if not _other_class(wanted, found)
        ]
        # Scores to a hundredth, so that listing and holdings decide between
        # the stock and its own bonds rather than a trigram's difference.
        return sorted(
            scored,
            key=lambda found: (
                round(found.score, 2),
                found.listed_on(on),
                found.held,
                found.ticker is not None,
            ),
            reverse=True,
        )

    async def best_candidate(self, asset_name: str, on: date) -> Candidate | None:
        found = await self.candidates(asset_name, on)
        return found[0] if found else None


def _name_confidence(best: Candidate, given: str | None) -> Decimal:
    confidence = min(Decimal(str(best.score)).quantize(Decimal("0.001")), NAME_CEILING)
    if given is not None and best.ticker is not None and not _same_listing(given, best.ticker):
        confidence = min(confidence, OTHER_LISTING)
    return confidence


def _same_listing(given: str, ticker: str) -> bool:
    """``HONAV`` is ``HONA`` when issued: a spin-off's ticker before it trades
    regular way, which the filer wrote and our history never had."""
    return given == ticker or (len(given) >= 4 and given.endswith("V") and given[:-1] == ticker)


def _other_class(wanted: str | None, found: _Retrieved) -> bool:
    """The line says which class, and the candidate is another one."""
    if wanted is None:
        return False
    theirs = share_class(found.name) or share_class(found.filed_name)
    return theirs is not None and theirs != wanted


# --- a pass ------------------------------------------------------------------------


@dataclass(slots=True)
class ResolutionPass:
    """What one pass did."""

    lines: int = 0
    by_class: Counter[str] = field(default_factory=Counter)
    by_method: Counter[str] = field(default_factory=Counter)
    unresolved: int = 0
    """Lines of a resolvable class left with no security."""

    @property
    def resolved(self) -> int:
        return sum(self.by_method.values())


async def resolve_assets(
    session: AsyncSession,
    *,
    transaction_ids: Sequence[int] | None = None,
    keys: Iterable[str] | None = None,
    force: bool = False,
) -> ResolutionPass:
    """Resolve the lines that need it; see the module docstring. Does not commit.

    :param transaction_ids: Only these lines, whatever their state: a line a
        person just accepted.
    :param keys: Only the lines under these asset keys, whatever their state:
        an override just set or removed.
    :param force: Every line, resolved or not.
    """
    resolver = await Resolver.load(session)
    passed = ResolutionPass()
    transaction = CongressTransaction
    where: list[Any] = [transaction.parse_status.in_(_LINES)]
    if transaction_ids is not None:
        where.append(transaction.id.in_(list(transaction_ids)))
    elif keys is not None:
        where.append(transaction.asset_key.in_(list(keys)))
    elif not force:
        where.append(
            or_(
                transaction.resolved_at.is_(None),
                transaction.security_id.is_(None)
                & transaction.asset_class.in_([kind.value for kind in RESOLVABLE]),
            )
        )

    after = 0
    while True:
        rows = (
            await session.execute(
                select(
                    transaction.id,
                    transaction.asset_type,
                    transaction.asset_name,
                    transaction.ticker,
                    transaction.description,
                    func.coalesce(transaction.transaction_date, CongressFiling.filing_date).label(
                        "on"
                    ),
                )
                .join(CongressFiling, CongressFiling.id == transaction.filing_id)
                .where(*where, transaction.id > after)
                .order_by(transaction.id)
                .limit(_BATCH)
            )
        ).all()
        if not rows:
            break
        after = rows[-1].id
        written = []
        now = datetime.now(UTC)
        for row in rows:
            resolved = await resolver.resolve(Line(*row))
            passed.lines += 1
            passed.by_class[resolved.asset_class.value] += 1
            if resolved.security_id is not None and resolved.method is not None:
                passed.by_method[resolved.method.value] += 1
            elif resolved.asset_class in RESOLVABLE:
                passed.unresolved += 1
            written.append(
                {
                    "id": row.id,
                    "asset_class": resolved.asset_class.value,
                    "security_id": resolved.security_id,
                    "resolution_method": None if resolved.method is None else resolved.method.value,
                    "resolution_confidence": resolved.confidence,
                    "asset_key": resolved.key,
                    "resolved_at": now,
                }
            )
        # By primary key, in one executemany.
        await session.execute(update(CongressTransaction), written)
    return passed


# --- coverage --------------------------------------------------------------------

#: Under this, a resolved line counts as low-confidence in coverage: a ticker
#: whose name disagrees, a name match to another listing, a loose name match.
LOW_CONFIDENCE: Final = Decimal("0.900")


def _percent(part: int, whole: int) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(100 * part) / whole).quantize(Decimal("0.1"), ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class AssetCoverage:
    """How much of a chamber's parsed lines resolved: by line and by filing.

    Counts ``parsed`` lines only, the feed's: a line in review is not a trade
    yet. The denominators are the :data:`~app.ingestion.congress.assets.RESOLVABLE`
    lines — stocks, options and ``unknown`` — since a bond or a fund has no
    security to find.
    """

    chamber: str
    lines: int = 0
    """Parsed lines."""
    pending: int = 0
    """Not resolved by any run yet."""
    by_class: dict[str, int] = field(default_factory=dict)
    resolvable: int = 0
    resolved: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    low_confidence: int = 0
    """Resolved under :data:`LOW_CONFIDENCE`."""
    filings: int = 0
    """Filings with a resolvable parsed line."""
    filings_resolved: int = 0
    """Of those, with every one resolved."""
    filings_partly: int = 0
    """With some resolved and some not."""

    @property
    def percent(self) -> Decimal | None:
        """Resolvable lines resolved, in percent, to a tenth."""
        return _percent(self.resolved, self.resolvable)

    @property
    def filing_percent(self) -> Decimal | None:
        """Filings with a resolvable line whose every one resolved, in percent."""
        return _percent(self.filings_resolved, self.filings)


async def asset_coverage(session: AsyncSession, chamber: str) -> AssetCoverage:
    """:class:`AssetCoverage` of ``chamber``'s lines as they stand."""
    line, filing = CongressTransaction, CongressFiling
    resolvable = line.asset_class.in_([kind.value for kind in RESOLVABLE])
    in_chamber = (
        select(line)
        .join(filing, filing.id == line.filing_id)
        .where(filing.chamber == chamber, line.parse_status == RowStatus.PARSED.value)
        .subquery()
    )
    found = in_chamber.c.security_id.is_not(None)
    grouped = await session.execute(
        select(
            in_chamber.c.asset_class,
            in_chamber.c.resolution_method,
            found.label("found"),
            func.count().filter(in_chamber.c.resolution_confidence < LOW_CONFIDENCE).label("low"),
            func.count().label("lines"),
        ).group_by(in_chamber.c.asset_class, in_chamber.c.resolution_method, found)
    )
    by_class: Counter[str] = Counter()
    by_method: Counter[str] = Counter()
    lines = pending = resolvable_lines = resolved = low = 0
    for row in grouped:
        lines += row.lines
        if row.asset_class is None:
            pending += row.lines
            continue
        by_class[row.asset_class] += row.lines
        if AssetClass(row.asset_class) in RESOLVABLE:
            resolvable_lines += row.lines
        if row.found:
            resolved += row.lines
            low += row.low
            by_method[row.resolution_method] += row.lines

    per_filing = (
        select(
            line.filing_id,
            func.count().filter(resolvable).label("resolvable"),
            func.count().filter(resolvable, line.security_id.is_not(None)).label("resolved"),
        )
        .join(filing, filing.id == line.filing_id)
        .where(filing.chamber == chamber, line.parse_status == RowStatus.PARSED.value)
        .group_by(line.filing_id)
        .subquery()
    )
    filings, filings_resolved, filings_partly = (
        await session.execute(
            select(
                func.count().filter(per_filing.c.resolvable > 0),
                func.count().filter(
                    per_filing.c.resolvable > 0, per_filing.c.resolved == per_filing.c.resolvable
                ),
                func.count().filter(
                    per_filing.c.resolved > 0, per_filing.c.resolved < per_filing.c.resolvable
                ),
            )
        )
    ).one()
    return AssetCoverage(
        chamber=chamber,
        lines=lines,
        pending=pending,
        by_class=dict(by_class.most_common()),
        resolvable=resolvable_lines,
        resolved=resolved,
        by_method=dict(by_method.most_common()),
        low_confidence=low,
        filings=filings,
        filings_resolved=filings_resolved,
        filings_partly=filings_partly,
    )
