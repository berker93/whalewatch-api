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

The five guards, and what each one actually catches
---------------------------------------------------
**Implied price.** ``value_usd / shares`` outside $0.01-$100,000, on ``SH``
rows. The only guard that checks our arithmetic against the world rather than
against the filer's own arithmetic, and so the only one that catches a units
error the filer made *consistently* — a manager who kept filing in thousands
after the cutover produces a document whose every internal total agrees with
itself and whose every share price is off by 1000. Several did exactly that in
2023 and had to amend.

**Entry count.** ``len(rows)`` against ``tableEntryTotal``. Catches a truncated
download and a parser that skipped a malformed row, both of which produce a
portfolio that is merely *smaller* than the real one — indistinguishable, in the
data, from a fund that sold.

**Value total.** The summed value against ``tableValueTotal``, within 1%.
Catches the same two failures when they land on a large position rather than a
small one, and catches a value column misread in a way that preserves the row
count.

**Negative quantity.** A value, share count or voting figure below zero. 13F is
long-only and ``holding`` refuses a negative, so the parser drops the row. This
guard turns that drop into a verdict of its own rather than one line among the
parser's warnings. The entry count usually catches the shortfall too, but only
when the cover page declares one.

**CUSIP format.** Nine letters and digits. The parser drops a row whose CUSIP it
cannot read, and keeps one written with ``*``, ``@`` or ``#``: characters the
CUSIP standard reserves for private placements, which are not Section 13(f)
securities. A holding reaches its security through this string, so a wrong one
is a position attributed to nothing, or to the wrong thing.

None of the first three is redundant with the others, and the first is the one
that would survive if only one could be kept. The last two mostly name a
*cause*. Where the count says a filing lost a row, they say which row, and why.

Every finding carries a :class:`Severity`. An ``error`` makes the filing
``suspect``; a ``warning`` is kept for whoever investigates and is never a
verdict on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import count, islice
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, computed_field

from app.db.models.filing import ParseStatus
from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    InfoTableRow,
    InfoTableWarning,
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

#: The fields the parser reads as quantities, spelled as its warnings report
#: them. A negative in any one of them costs the row: the voting figures are
#: share counts too, and the parser refuses them on the same rule.
_QUANTITY_FIELDS: Final = frozenset({"value", "sshPrnamt", "Sole", "Shared", "None"})


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


class Severity(StrEnum):
    """What a :class:`ParseNote` means for the filing it is on.

    Two levels, because the notes do two jobs. Most are verdicts: the filing
    failed a guard. The rest are evidence. A row the parser could not read is
    recorded so that the entry-count finding it almost always comes with can
    name the row. Flagging a filing on evidence alone would make ``suspect``
    mean "the parser said something", and that is how a flag gets ignored.
    """

    ERROR = "error"
    """A guard failed. One is enough to make the filing ``suspect``."""

    WARNING = "warning"
    """Stored for whoever investigates, and never a verdict on its own."""


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
    NEGATIVE_QUANTITY = "negative_quantity"
    CUSIP_FORMAT = "cusip_format"
    DROPPED_ROW = "dropped_row"
    """A row the parser dropped for a reason no guard above names — a value that
    is not a number, an ``sshPrnamtType`` that is neither ``SH`` nor ``PRN``."""

    @property
    def severity(self) -> Severity:
        """Every guard's finding is an error; a row dropped for no named reason is a warning.

        A dropped row is a lost position, but not a verdict of its own: when the
        cover page declares a count, the entry-count guard has already fired over
        it, and this note is how that finding names the row. The two dropped-row
        causes that *are* verdicts, a negative quantity and an unreadable CUSIP,
        have kinds of their own.
        """
        return Severity.WARNING if self is NoteKind.DROPPED_ROW else Severity.ERROR


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

    # mypy does not follow a decorator stacked on @property; pydantic's
    # documented workaround.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity(self) -> Severity:
        """The kind's :attr:`NoteKind.severity`, stored with the rest of the note.

        Derived, so that a note cannot disagree with its own kind. Stored, so
        that nothing reading the column has to import this module to learn which
        findings made the filing suspect: ``WHERE parse_notes @>
        '[{"severity": "error"}]'`` is every filing a guard failed on.
        """
        return self.kind.severity


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
        share price — around 1 for a note near par — which is why the guard
        only reads ``SH`` rows: see :func:`_implied_price_notes`. For an option
        it is the underlying's price, because the value is notional and the
        quantity is the underlying shares, so option lines stay in the check.
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

    Non-empty does not imply ``SUSPECT``; an :attr:`Severity.ERROR` does. A row
    dropped for a reason no guard names is a warning, recorded for whoever has
    to find it: the entry count is the verdict on the filing it came from.
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
    placed = tuple(zip(_document_rows(table), holdings, strict=True))

    # The parser's dropped rows come last. Each is filed under the guard it
    # failed — a negative quantity, an unreadable CUSIP — or, failing both, as
    # a warning; either way this is what turns an entry-count finding from
    # "two rows short" into "these two rows, this CUSIP, this field".
    notes = (
        *_implied_price_notes(placed),
        *_checksum_notes(cover=cover, holdings=holdings, multiplier=multiplier),
        *_negative_quantity_notes(table),
        *_cusip_notes(table, placed),
        *_dropped_row_notes(table),
    )
    suspect = any(note.severity is Severity.ERROR for note in notes)

    return NormalisedFiling(
        value_multiplier=multiplier,
        holdings=holdings,
        parse_status=ParseStatus.SUSPECT if suspect else ParseStatus.OK,
        parse_notes=notes,
    )


#: A parsed row, with the 1-based position of the ``<infoTable>`` it came from.
_Placed = tuple[int, NormalisedHolding]


def _document_rows(table: InformationTable) -> Iterable[int]:
    """The document position of each parsed row, in order.

    Not ``enumerate(table.rows)``. That counts the rows the parser *kept*, so
    one dropped row shifts every row after it, and a note would send someone
    to the ``<infoTable>`` before the one it is about. The parser reads every
    element in order and either keeps it or reports it dropped, so the kept
    rows occupy exactly the positions the dropped ones do not.
    """
    dropped = {warning.row for warning in table.warnings if warning.dropped}
    kept = (position for position in count(1) if position not in dropped)
    return islice(kept, len(table.rows))


def _implied_price_notes(placed: tuple[_Placed, ...]) -> tuple[ParseNote, ...]:
    """One note per ``SH`` row whose ``value_usd / shares`` is not a plausible price.

    ``SH`` only, because a share-price range can only judge a share. A ``PRN``
    row's quantity is a face value in dollars, so its ratio is a price per
    dollar of principal: around 1 for a bond near par. A bond written up 1000x
    by mistake lands at $1,000 and passes, and a defaulted one quoted at half a
    cent fails. Neither result tells us anything.
    """
    notes = [
        ParseNote(
            kind=NoteKind.IMPLIED_PRICE,
            detail=(
                f"{holding.row.cusip}: ${holding.value_usd} over {holding.row.shares} "
                f"shares implies ${price} a share, outside "
                f"${MIN_IMPLIED_PRICE}-${MAX_IMPLIED_PRICE}"
            ),
            row=position,
            cusip=holding.row.cusip,
            observed=price,
        )
        for position, holding in placed
        if holding.row.sh_prn_type == "SH"
        and (price := holding.implied_price) is not None
        and not (MIN_IMPLIED_PRICE <= price <= MAX_IMPLIED_PRICE)
    ]
    return _capped(notes, kind=NoteKind.IMPLIED_PRICE, of=len(placed))


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


def _negative_quantity_notes(table: InformationTable) -> tuple[ParseNote, ...]:
    """Rows the parser dropped for a value, share count or voting figure below zero.

    Read off the parser's warnings because that is the only place a negative can
    be seen. The parser refuses one, and ``holding``'s check constraints would
    refuse the insert if it did not, so no row that reaches this module can
    carry a minus sign. The document can. 13F is long-only, so a minus sign is a
    sign error in someone's export, and the position it was on is missing.
    """
    notes = [
        _dropped_note(warning, NoteKind.NEGATIVE_QUANTITY)
        for warning in _dropped(table, NoteKind.NEGATIVE_QUANTITY)
    ]
    return _capped(notes, kind=NoteKind.NEGATIVE_QUANTITY, of=_row_count(table))


def _cusip_notes(table: InformationTable, placed: tuple[_Placed, ...]) -> tuple[ParseNote, ...]:
    """Every CUSIP that is not nine letters and digits, kept or dropped, in row order.

    The parser keeps a row whose CUSIP uses ``*``, ``@`` or ``#``: those are
    real CUSIP characters, reserved for private placements, so the row loads
    as filed. Private placements are not Section 13(f) securities, though, so
    the filing is flagged. The parser drops a row whose CUSIP it cannot read at
    all, because it is missing, longer than nine characters, or in characters
    no CUSIP uses.

    A CUSIP the parser left-padded is not a finding. ``37833100`` is
    ``037833100`` with its leading zero eaten by a spreadsheet, and the padding
    cannot be wrong. Flagging it would make every filing from that agent
    suspect over a repair nobody needs to review.
    """
    kept = [
        ParseNote(
            kind=NoteKind.CUSIP_FORMAT,
            detail=f"{holding.row.cusip}: not nine letters and digits; loaded as filed",
            row=position,
            cusip=holding.row.cusip,
        )
        for position, holding in placed
        if not _is_cusip(holding.row.cusip)
    ]
    dropped = [
        _dropped_note(warning, NoteKind.CUSIP_FORMAT)
        for warning in _dropped(table, NoteKind.CUSIP_FORMAT)
    ]
    notes = sorted([*kept, *dropped], key=lambda note: note.row or 0)
    return _capped(notes, kind=NoteKind.CUSIP_FORMAT, of=_row_count(table))


def _dropped_row_notes(table: InformationTable) -> tuple[ParseNote, ...]:
    """Rows the parser dropped for a reason neither guard above names. Warnings.

    Only the dropped ones. A tolerated warning — today a malformed ``<figi>``,
    which is nulled while the position it belongs to is kept — costs no value
    and no share count, and putting it here would fill the column that exists
    for missing money with findings about enrichment.
    """
    notes = [
        _dropped_note(warning, NoteKind.DROPPED_ROW)
        for warning in _dropped(table, NoteKind.DROPPED_ROW)
    ]
    return _capped(notes, kind=NoteKind.DROPPED_ROW, of=_row_count(table))


def _dropped(table: InformationTable, kind: NoteKind) -> list[InfoTableWarning]:
    """The parser's dropped rows that belong under ``kind``. Each belongs under one."""
    return [
        warning
        for warning in table.warnings
        if warning.dropped and _dropped_row_kind(warning) is kind
    ]


def _dropped_row_kind(warning: InfoTableWarning) -> NoteKind:
    """Which guard a dropped row failed, from the field and value the parser reported.

    Decided on the value rather than by matching the parser's sentence, so that
    rewording a message cannot quietly move a finding from an error to a
    warning. A row whose CUSIP is missing fails the CUSIP guard like any other
    unreadable one: an empty string is not nine characters either.
    """
    if warning.field == "cusip":
        return NoteKind.CUSIP_FORMAT
    if warning.field in _QUANTITY_FIELDS and _is_negative(warning.value):
        return NoteKind.NEGATIVE_QUANTITY
    return NoteKind.DROPPED_ROW


def _dropped_note(warning: InfoTableWarning, kind: NoteKind) -> ParseNote:
    """A dropped row as a note: the parser's own sentence, and the row's absence."""
    got = f" (got {warning.value!r})" if warning.value is not None else ""
    return ParseNote(
        kind=kind,
        detail=f"{warning.field}: {warning.reason}{got}; row not loaded",
        row=warning.row,
        cusip=warning.cusip,
    )


def _is_negative(text: str | None) -> bool:
    """Whether a quantity the parser refused was a number below zero.

    Tolerates thousands separators, as the parser's own read does. A value that
    is not a finite number at all was refused for that instead.
    """
    if text is None:
        return False
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return False
    return number.is_finite() and number < 0


def _is_cusip(value: str) -> bool:
    """Nine ASCII letters and digits. ``str.isalnum`` alone would pass ``É``."""
    return len(value) == 9 and value.isascii() and value.isalnum()


def _row_count(table: InformationTable) -> int:
    """``<infoTable>`` elements in the document: the rows kept and the ones dropped."""
    return len(table.rows) + sum(warning.dropped for warning in table.warnings)


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
