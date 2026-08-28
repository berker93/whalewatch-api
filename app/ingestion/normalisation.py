"""Whole dollars, and the guards that decide whether we believe them.

The step between :mod:`app.ingestion.parsers.thirteen_f`, which reads a document
and does not interpret it, and the loader, which writes rows. Everything here is
a pure function of the parsed documents plus one fact about the submission —
``filed_at`` — and none of it touches a database, a network or a clock.

Why this is not in the parser
-----------------------------
The units a 13F's ``value`` column is in are not a property of the information
table. They are a property of the *submission*: filings accepted before
2023-01-03 report thousands of dollars, filings accepted on or after it report
whole dollars, and neither document says which. The deciding fact — ``filed_at``
— arrives from EDGAR's index, so a parser that scaled values would have to be
handed a fact it cannot read, and the checksum against ``tableValueTotal`` would
have to be performed against a moving target. Keeping the parser raw leaves both
sides of that comparison in the filing's own units, where they are comparable.

Why the guards are here rather than in the database
---------------------------------------------------
A check constraint can say ``value_usd >= 0``. It cannot say "this share price
is not a share price", because that judgement is about a row's relationship to
the filing it came from, and by the time a row reaches the table it has no
filing context left. It also must not *reject*: a filing that fails a guard is
still the only disclosure that manager made for that quarter, and dropping it
leaves a hole that looks exactly like a manager who filed nothing. So the guards
mark, and the loader writes anyway — see
:class:`~app.db.models.filing.ParseStatus`.

The three guards, and what each one actually catches
----------------------------------------------------
**Implied price.** ``value_usd / shares`` outside $0.01-$100,000. The only guard
that checks our arithmetic against the world rather than against the filer's own
arithmetic, and so the only one that catches a units error the filer made
*consistently* — a manager who kept filing in thousands after the cutover
produces a document whose every internal total agrees with itself and whose
every share price is off by 1000. Several did exactly that in 2023 and had to
amend.

**Entry count.** ``len(rows)`` against ``tableEntryTotal``. Catches a truncated
download and a parser that skipped a malformed row, both of which produce a
portfolio that is merely *smaller* than the real one — indistinguishable, in the
data, from a fund that sold.

**Value total.** The summed value against ``tableValueTotal``, within 1%.
Catches the same two failures when they land on a large position rather than a
small one, and catches a value column misread in a way that preserves the row
count.

None of the three is redundant with the others, and the first is the one that
would survive if only one could be kept.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from app.db.models.filing import ParseStatus
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    InfoTableRow,
    PrimaryDoc,
)

#: The day the 13F ``value`` column changed units. Filings accepted on or after
#: this date report whole dollars; before it, thousands.
DOLLAR_CUTOVER: Final = date(2023, 1, 3)

#: The cutover as an instant, because a date alone cannot order timestamps.
#:
#: EDGAR's clock is Eastern — its "filing date" is the date in New York, and it
#: accepts submissions until 22:00 there. A submission accepted at 20:00 ET on
#: 2 January is 01:00 UTC on the 3rd, so ``filed_at.date()`` on a UTC timestamp
#: puts it on the far side of a line EDGAR puts it on the near side of, and
#: values in thousands get loaded as dollars. Three hours of one day, on the one
#: day this module exists to get right.
#:
#: A fixed -05:00 rather than ``ZoneInfo("America/New_York")``: the cutover is in
#: January, which is never daylight time, so the offset is not an approximation
#: here — and a fixed offset needs no tz database in the container.
_CUTOVER_INSTANT: Final = datetime.combine(
    DOLLAR_CUTOVER, time.min, tzinfo=timezone(timedelta(hours=-5))
)

#: Below this, a "share price" is not one. Wide enough for genuine sub-penny
#: names, which do get reported, and narrow enough that a value column divided
#: by 1000 falls through it for anything that trades under about $10.
MIN_IMPLIED_PRICE: Final = Decimal("0.01")

#: Above this, likewise. Berkshire's class A is the highest-priced US equity
#: there has ever been and has never reached $1,000,000; a post-cutover filing
#: multiplied by 1000 in error puts every ordinary position past this line.
MAX_IMPLIED_PRICE: Final = Decimal(100_000)

#: How far the summed value may sit from the cover page's own total. One
#: percent rather than exact equality because filers round: the summary page is
#: often computed from a spreadsheet that carried more precision than the rows
#: it was printed from, and a handful of dollars across a 3,000-row filing is
#: not a finding. A 1000x error is not within 1% of anything.
CHECKSUM_TOLERANCE: Final = Decimal("0.01")

#: How many offending rows one guard may name before the rest are summarised.
#:
#: The failure this bounds is the interesting one: a filing whose *units* are
#: wrong has every row outside the price range, so the note that explains it
#: would otherwise be one JSON object per position — 3,000 of them, on the very
#: filings someone is most likely to open. Twenty-five names the pattern; the
#: raw document names the rest.
MAX_NOTED_ROWS: Final = 25

#: `holding.value_usd` is `numeric(20, 2)`. Quantising here rather than letting
#: Postgres do it means the value this module reports in a note is the value the
#: column holds, to the cent.
_CENTS: Final = Decimal("0.01")


def resolve_value_multiplier(filed_at: datetime) -> int:
    """What the filing's ``value`` column must be multiplied by: 1 or 1000.

    :param filed_at: When EDGAR accepted the submission. Timezone-aware,
        always — see below.
    :returns: ``1`` for a filing accepted on or after :data:`DOLLAR_CUTOVER`,
        ``1000`` for one accepted before it.
    :raises ValueError: If ``filed_at`` is naive. A timestamp with no zone
        cannot be placed on either side of a line, and the two available guesses
        — "it is UTC" and "it is Eastern" — differ by exactly the hours where
        the answer changes. Guessing here would be a silent 1000x error, which
        is the one thing this function exists to prevent.

    **Keyed on the filing date, never the period.** The convention follows the
    submission, so an amendment filed in 2024 for a 2019 quarter is in whole
    dollars even though the original filing for that same quarter was in
    thousands. A ``period_of_report < 2023`` test gets exactly the amendments
    wrong, and amendments are the filings nobody is watching.
    """
    if filed_at.tzinfo is None or filed_at.tzinfo.utcoffset(filed_at) is None:
        raise ValueError(
            f"filed_at must be timezone-aware to be placed against the cutover: {filed_at!r}"
        )
    return 1 if filed_at >= _CUTOVER_INSTANT else 1000


class NoteKind(StrEnum):
    """Which guard produced a :class:`ParseNote`.

    A closed vocabulary rather than free text because these are what
    ``parse_notes`` gets queried by: "every filing the implied-price guard fired
    on, this backfill" is a containment lookup on this field, and a sentence
    is not.
    """

    IMPLIED_PRICE = "implied_price"
    ENTRY_COUNT = "entry_count"
    VALUE_TOTAL = "value_total"
    DROPPED_ROW = "dropped_row"


class ParseNote(BaseModel):
    """One thing a guard found, in the form it is stored in ``filing.parse_notes``.

    Both a machine-readable finding and a sentence, because the two audiences
    are different: a query filters on :attr:`kind` and :attr:`cusip`, and a
    person reads :attr:`detail` and decides whether to open the raw document.
    Writing only the sentence makes the first impossible; writing only the
    fields makes the second an exercise in remembering what ``expected`` meant
    for this particular guard.
    """

    model_config = ConfigDict(frozen=True)

    kind: NoteKind
    """Which guard fired."""

    detail: str
    """The finding as a sentence, with its numbers spelled out."""

    row: int | None = None
    """1-based position of the ``<infoTable>`` in the document, for row findings.

    Not an identifier of anything — it is how the element is found in the raw
    XML, which is the only place the truth about it lives.
    """

    cusip: str | None = None
    """The security the offending row named, when the finding is about a row."""

    observed: Decimal | None = None
    """What we computed: an implied price, a row count, a summed value."""

    expected: Decimal | None = None
    """What it was checked against, when the check had a single right answer.

    ``None`` for :attr:`NoteKind.IMPLIED_PRICE`, where the expectation is a
    range rather than a number and lives in :attr:`detail`.
    """


class NormalisedHolding(BaseModel):
    """One parsed row, plus the dollar value derived from it.

    Composition rather than a flattened copy of :class:`InfoTableRow`. The row
    stays exactly as filed — it is the record — and :attr:`value_usd` is
    visibly a derived figure sitting next to the number it was derived from,
    which is what makes ``value_usd == row.value * multiplier`` checkable by
    eye at a breakpoint. Flattening would produce a second object with a
    ``value`` field whose units are a matter of which class you are holding.
    """

    model_config = ConfigDict(frozen=True)

    row: InfoTableRow
    """The row as filed, in the filing's own units."""

    value_usd: Decimal
    """Whole dollars, quantised to the cent, for every filing on either side of
    the cutover. What :attr:`~app.db.models.holding.Holding.value_usd` receives."""

    @property
    def implied_price(self) -> Decimal | None:
        """``value_usd / shares``, or ``None`` when that is not a price.

        Two cases return ``None`` and neither is a finding:

        * **No shares.** Nothing divides by zero, and a row with no quantity
          says nothing about units either way.
        * **Zero value.** A position worth less than $500 rounds to ``0`` in
          the thousands convention, so a zero here is the *pre-cutover* format
          working as designed. Reading it as a $0.00 share price would make a
          large fraction of every pre-2023 filing suspect and teach everyone to
          ignore the flag.

        For a ``PRN`` row this is dollars per dollar of face value rather than a
        share price — around 1 for a note near par, and comfortably inside the
        same bounds. For an option it is the underlying's price, because the
        value is notional and the quantity is the underlying shares. Both stay
        in the check: a 1000x error moves them out of range exactly as it moves
        a common-stock row out of range.
        """
        if self.row.shares == 0 or self.value_usd == 0:
            return None
        return self.value_usd / self.row.shares


class NormalisedFiling(BaseModel):
    """Everything the loader needs that the parser could not decide alone.

    Frozen, and a value rather than a set of writes: what makes this testable is
    that "did the guards fire, and why" is answerable without a database.
    """

    model_config = ConfigDict(frozen=True)

    value_multiplier: int
    """1 or 1000. Written to :attr:`~app.db.models.filing.Filing.value_multiplier`.

    Stored on the filing rather than inferred again later, so that a 1000x error
    is diagnosable from the row — ``SELECT value_multiplier, filed_at`` — rather
    than by re-deriving the decision that produced it.
    """

    holdings: tuple[NormalisedHolding, ...]
    """The rows, in document order, each with its dollar value.

    Every row the parser returned is here, including the ones a guard named. A
    suspect filing loads in full; that is the point of flagging rather than
    rejecting.
    """

    parse_status: ParseStatus
    """:attr:`~app.db.models.filing.ParseStatus.OK` or ``SUSPECT``.

    Never ``PENDING`` or ``FAILED``: both of those describe a filing that did
    not get this far.
    """

    parse_notes: tuple[ParseNote, ...]
    """What the guards found, in guard order. Empty when nothing fired.

    Non-empty does not imply ``SUSPECT``: a dropped row is recorded here for
    whoever has to find it, and on a filing with no declared totals to check it
    against there is nothing to be suspicious *of*.
    """

    @property
    def parse_notes_json(self) -> list[dict[str, Any]] | None:
        """:attr:`parse_notes` as JSON-ready dicts, or ``None`` when empty.

        ``None`` rather than ``[]`` so that "the guards found nothing" has one
        spelling in the column rather than two that every query has to handle.

        ``mode="json"`` renders each ``Decimal`` as a string. A JSON number is
        an IEEE 754 double, and a column whose job is to record a suspected
        1000x error is the last place to introduce a second rounding of the
        figure in question.
        """
        if not self.parse_notes:
            return None
        return [note.model_dump(mode="json", exclude_none=True) for note in self.parse_notes]


def normalise_filing(
    *,
    filed_at: datetime,
    cover: PrimaryDoc,
    table: InformationTable,
) -> NormalisedFiling:
    """Scale a parsed 13F to whole dollars and run every guard over the result.

    :param filed_at: When EDGAR accepted the submission, timezone-aware. The
        only input that is not one of the two documents, and the one that
        decides the multiplier.
    :param cover: The parsed ``primary_doc.xml``. Its declared totals are the
        only independent check on the information table; when it has none —
        a ``13F NOTICE`` has no summary page — the checksum guards report
        nothing rather than treating a missing total as zero.
    :param table: The parsed information table, in the filing's own units.
    :returns: The rows in dollars, the multiplier used, and a status with its
        findings. Never raises on a bad filing: a filing that fails every guard
        still comes back loadable, flagged.
    """
    multiplier = resolve_value_multiplier(filed_at)
    holdings = tuple(
        NormalisedHolding(row=row, value_usd=(row.value * multiplier).quantize(_CENTS))
        for row in table.rows
    )

    price_notes = _implied_price_notes(holdings)
    checksum_notes = _checksum_notes(cover=cover, holdings=holdings, multiplier=multiplier)

    # Dropped rows are diagnostics, not a verdict. They are almost always
    # accompanied by an entry-count finding — a row the parser could not read is
    # a row missing from the count — and this is what turns that finding from
    # "two rows short" into "these two rows, this CUSIP, this field".
    notes = (*price_notes, *checksum_notes, *_dropped_row_notes(table))
    suspect = bool(price_notes or checksum_notes)

    return NormalisedFiling(
        value_multiplier=multiplier,
        holdings=holdings,
        parse_status=ParseStatus.SUSPECT if suspect else ParseStatus.OK,
        parse_notes=notes,
    )


def _implied_price_notes(holdings: tuple[NormalisedHolding, ...]) -> tuple[ParseNote, ...]:
    """One note per row whose ``value_usd / shares`` is not a plausible price."""
    notes = [
        ParseNote(
            kind=NoteKind.IMPLIED_PRICE,
            # No "a share": on a PRN row the quantity is a face value in
            # dollars, so this ratio is a price per dollar of principal, and
            # calling it a share price in the note would send whoever reads it
            # looking for a stock that does not exist.
            detail=(
                f"{holding.row.cusip}: ${holding.value_usd} over {holding.row.shares} "
                f"{holding.row.sh_prn_type} implies ${price}, outside "
                f"${MIN_IMPLIED_PRICE}-${MAX_IMPLIED_PRICE}"
            ),
            row=position,
            cusip=holding.row.cusip,
            observed=price,
        )
        for position, holding in enumerate(holdings, start=1)
        if (price := holding.implied_price) is not None
        and not (MIN_IMPLIED_PRICE <= price <= MAX_IMPLIED_PRICE)
    ]
    return _capped(notes, kind=NoteKind.IMPLIED_PRICE, of=len(holdings))


def _checksum_notes(
    *,
    cover: PrimaryDoc,
    holdings: tuple[NormalisedHolding, ...],
    multiplier: int,
) -> tuple[ParseNote, ...]:
    """The cover page's own count and total, against what we parsed.

    Both comparisons are made in whole dollars, the declared total scaled by the
    same multiplier as the rows. The ratio is identical either way — scaling
    both sides cannot change it — and reporting dollars keeps every figure in
    ``parse_notes`` in one unit, which is worth more than showing the number the
    document printed.
    """
    notes: list[ParseNote] = []

    if cover.table_entry_total is not None and len(holdings) != cover.table_entry_total:
        notes.append(
            ParseNote(
                kind=NoteKind.ENTRY_COUNT,
                detail=(
                    f"parsed {len(holdings)} rows, cover page declares {cover.table_entry_total}"
                ),
                observed=Decimal(len(holdings)),
                expected=Decimal(cover.table_entry_total),
            )
        )

    if cover.table_value_total is not None:
        declared = (Decimal(cover.table_value_total) * multiplier).quantize(_CENTS)
        summed = sum((holding.value_usd for holding in holdings), start=Decimal(0))
        if abs(summed - declared) > declared * CHECKSUM_TOLERANCE:
            notes.append(
                ParseNote(
                    kind=NoteKind.VALUE_TOTAL,
                    detail=(
                        f"rows sum to {summed}, cover page declares {declared} "
                        f"(x{multiplier}), outside {CHECKSUM_TOLERANCE:%}"
                    ),
                    observed=summed,
                    expected=declared,
                )
            )

    return tuple(notes)


def _dropped_row_notes(table: InformationTable) -> tuple[ParseNote, ...]:
    """The parser's own account of rows it could not read.

    Only the dropped ones. A tolerated warning — today a malformed ``<figi>``,
    which is nulled while the position it belongs to is kept — costs no value
    and no share count, and putting it here would fill the column that exists
    for missing money with findings about enrichment.
    """
    dropped = [warning for warning in table.warnings if warning.dropped]
    notes = [
        ParseNote(
            kind=NoteKind.DROPPED_ROW,
            detail=f"{warning.field}: {warning.reason}"
            + (f" (got {warning.value!r})" if warning.value is not None else ""),
            row=warning.row,
            cusip=warning.cusip,
        )
        for warning in dropped
    ]
    return _capped(notes, kind=NoteKind.DROPPED_ROW, of=len(table.rows) + len(dropped))


def _capped(notes: list[ParseNote], *, kind: NoteKind, of: int) -> tuple[ParseNote, ...]:
    """The first :data:`MAX_NOTED_ROWS` notes, plus one saying how many there were.

    The summary note carries the full count in ``observed``, so a query can rank
    filings by how badly a guard fired without the column having to hold a row
    per position.
    """
    if len(notes) <= MAX_NOTED_ROWS:
        return tuple(notes)
    return (
        *notes[:MAX_NOTED_ROWS],
        ParseNote(
            kind=kind,
            detail=(
                f"{len(notes)} of {of} rows, of which "
                f"{len(notes) - MAX_NOTED_ROWS} are not listed here"
            ),
            observed=Decimal(len(notes)),
            expected=Decimal(of),
        ),
    )
