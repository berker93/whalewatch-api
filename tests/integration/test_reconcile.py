"""Reconciliation: every derived number against one worked out by hand, then the invariants.

Derived data is where wrongness hides. A snapshot can look plausible with a
restated filing counted twice, and a change table can look plausible with an
exit missing. So this suite has two halves.

**The fixture, worked out on paper.** Three filers over the four quarters of
2024, at one set of period-end prices, with each case the derived tables have
to get right: a new position, an add, a trim, a hold (exact, with the price
moving, and one share inside the hold band), an exit, a re-entry after an exit,
a restatement, a filer that skips a quarter, a filer whose history starts late,
a suspect filing a restatement replaced, a stake completed by a new-holdings
amendment, and an option and a bond that are not positions. :data:`SNAPSHOT`, :data:`CHANGES`,
:data:`CONSENSUS`, :data:`FLOWS` and :data:`SUMMARY` are every row of the
derived tables and the views, computed by hand from the books below, not by
running anything. Each is asserted whole, built three ways: everything at once,
filing by filing as filed, and filing by filing newest first.

**The invariants, made to fail.** :mod:`app.derived.reconcile` runs over the
fixture clean, and then over the fixture broken one way at a time, and each
breakage has to be caught by the invariant meant for it, on the row it was made
on. A check that has never failed proves nothing. ``make reconcile`` runs the
same invariants over the real dataset.

The books
---------
Prices, the same for every filer::

            2024Q1  2024Q2  2024Q3  2024Q4
    ACME        10      12      15      15
    BOLT        20      20      25      25
    CORE        50      40      40      50
    DYNA         5       5      10      10
    ECHO       100     100     100     100

``alpha-capital`` files every quarter::

    Q1  ACME 1,000 = 10,000   BOLT 2,000 = 40,000   CORE 1,000 = 50,000          = 100,000
    Q2  ACME 1,500 = 18,000   BOLT 2,000 = 40,000   DYNA 10,000 = 50,000         = 108,000
        add +500              hold                  new; CORE exits
    Q3  ACME 3,666 = 54,990   BOLT 1,000 = 25,000   CORE 500 = 20,000
        DYNA 10,001 = 100,010                                                    = 200,000
        add +2,166            trim -1,000           CORE back: new, against Q2
        DYNA +1 share, 0.01%: a hold, the band being inclusive
    Q4  ACME 3,666 = 54,990   CORE 900 = 45,000     DYNA 10,001 = 100,010        = 200,000
        hold                  add +400              hold; BOLT exits

The 900 CORE arrive in two filings: 500 in the original, and 400 in a
new-holdings amendment filed last, as a stake released from confidential
treatment is. They are one position, read from the amendment, which last
changed it.

Q2's weights are sixths and twenty-sevenths, rounded to six places: 16.666667,
37.037037 and 46.296296, which sum to 100.000000.

``bravo-partners`` starts in Q2, so its Q2 is a first period and not a flow.
Its Q3 original is suspect: values in thousands, and a DYNA line in error. A
restatement filed after its Q4 replaces it, so Q3 is published from the
restatement alone, and Q4's changes are against it::

    Q2    ACME 2,500 = 30,000   ECHO 700 = 70,000                                = 100,000
    Q3    ACME 2,500 = $38      DYNA 4,000 = $40      ECHO 700 = $70             suspect
    Q3/A  ACME 2,500 = 37,500   BOLT 500 = 12,500     ECHO 500 = 50,000          = 100,000
          hold, +7,500 on price new                   trim -200
    Q4    ACME 2,500 = 37,500   BOLT 2,500 = 62,500                              = 100,000
          hold                  add +2,000            ECHO exits

``charlie-fund`` files nothing for Q3, so Q4 is against Q2, and BOLT's exit is
dated Q4. Its Q1 is three thirds, 33.333333 each, which sum to 99.999999. Its Q2
also lists a call on 100 CORE and $50,000 of a bond, neither of which is in
any table below::

    Q1  ACME 1,000 = 10,000   BOLT 500 = 10,000     CORE 200 = 10,000            =  30,000
    Q2  ACME 1,000 = 12,000   BOLT 1,000 = 20,000   CORE 200 = 8,000             =  40,000
        hold, +2,000 on price add +500              hold, -2,000 on price
    Q4  ACME 800 = 12,000     CORE 200 = 10,000     ECHO 180 = 18,000            =  40,000
        trim -200             hold                  new; BOLT exits, at Q2's $20

Every period's value deltas sum to its change in value: alpha's Q2 is 8,000 on
100,000, and its Q4 nets to nothing, a 25,000 exit against a 25,000 add.

The views, from those
---------------------
Flows count traded dollars, the shares bought or sold at the period-end price,
an exit at its last price. A hold trades nothing, and a first period is not a
flow. Turnover is half of what was traded over the previous book: alpha's Q3
traded 32,490 + 25,000 + 20,000 = 77,490 against 108,000, which is 35.875%.
The median of two weights is their midpoint, rounded half up: 10 and
33.333333 give 21.666667.
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from typer.testing import CliRunner

from app.cli import app
from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.models import (
    Filer,
    FilerCik,
    Filing,
    Holding,
    PositionChange,
    PositionSnapshot,
    Security,
)
from app.db.models.enums import AmendmentKind
from app.derived.recompute import recompute
from app.derived.reconcile import INVARIANTS, Reconciliation, reconcile
from app.derived.scope import EVERYTHING, Scope, filing_pairs
from app.derived.views import (
    CONSENSUS_HOLDINGS,
    FILER_SUMMARY,
    MATERIALISED_VIEWS,
    QUARTER_FLOWS,
    refresh_views,
)
from tests.conftest import make_settings

D = Decimal

Q1 = date(2024, 3, 31)
Q2 = date(2024, 6, 30)
Q3 = date(2024, 9, 30)
Q4 = date(2024, 12, 31)

ACME = "11111A101"
BOLT = "22222B202"
CORE = "33333C303"
DYNA = "44444D404"
ECHO = "55555E505"
BOND = "66666F606"

ALPHA = "alpha-capital"
BRAVO = "bravo-partners"
CHARLIE = "charlie-fund"
CIKS: Final = {ALPHA: "0000000101", BRAVO: "0000000102", CHARLIE: "0000000103"}

#: Period-end prices, the same for every filer: a position's value is its shares at these.
PRICES: Final = {
    Q1: {ACME: 10, BOLT: 20, CORE: 50, DYNA: 5, ECHO: 100},
    Q2: {ACME: 12, BOLT: 20, CORE: 40, DYNA: 5, ECHO: 100},
    Q3: {ACME: 15, BOLT: 25, CORE: 40, DYNA: 10, ECHO: 100},
    Q4: {ACME: 15, BOLT: 25, CORE: 50, DYNA: 10, ECHO: 100},
}


@dataclass(frozen=True, slots=True)
class _Filing:
    """One 13F of the fixture, inserted as loaded, with its holdings."""

    label: str
    filer: str
    period: date
    filed: datetime
    held: dict[str, int]
    """Shares of common stock by CUSIP, worth :data:`PRICES` unless :attr:`values` says so."""
    values: dict[str, int] | None = None
    amends: AmendmentKind | None = None
    suspect: bool = False
    lines: tuple[tuple[str, str | None, str, int, int], ...] = ()
    """Lines that are not common stock: ``(cusip, put_call, sshprnamt_type, amount, value)``."""

    @property
    def accession(self) -> str:
        return f"{CIKS[self.filer]}-{self.filed:%y}-{FILINGS.index(self) + 1:06d}"


def _filed(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 16, tzinfo=UTC)


# fmt: off
#: Every filing, in the order filed.
FILINGS: Final = (
    _Filing("alpha 2024Q1", ALPHA, Q1, _filed(2024, 5, 10),
            {ACME: 1_000, BOLT: 2_000, CORE: 1_000}),
    _Filing("charlie 2024Q1", CHARLIE, Q1, _filed(2024, 5, 14),
            {ACME: 1_000, BOLT: 500, CORE: 200}),
    _Filing("alpha 2024Q2", ALPHA, Q2, _filed(2024, 8, 9),
            {ACME: 1_500, BOLT: 2_000, DYNA: 10_000}),
    _Filing("bravo 2024Q2", BRAVO, Q2, _filed(2024, 8, 12),
            {ACME: 2_500, ECHO: 700}),
    _Filing("charlie 2024Q2", CHARLIE, Q2, _filed(2024, 8, 14),
            {ACME: 1_000, BOLT: 1_000, CORE: 200},
            lines=((CORE, "Call", "SH", 100, 4_000), (BOND, None, "PRN", 50_000, 49_000))),
    _Filing("alpha 2024Q3", ALPHA, Q3, _filed(2024, 11, 8),
            {ACME: 3_666, BOLT: 1_000, CORE: 500, DYNA: 10_001}),
    _Filing("bravo 2024Q3", BRAVO, Q3, _filed(2024, 11, 13),
            {ACME: 2_500, DYNA: 4_000, ECHO: 700},
            values={ACME: 38, DYNA: 40, ECHO: 70}, suspect=True),
    _Filing("alpha 2024Q4", ALPHA, Q4, _filed(2025, 2, 7),
            {ACME: 3_666, CORE: 500, DYNA: 10_001}),
    _Filing("bravo 2024Q4", BRAVO, Q4, _filed(2025, 2, 11),
            {ACME: 2_500, BOLT: 2_500}),
    _Filing("charlie 2024Q4", CHARLIE, Q4, _filed(2025, 2, 14),
            {ACME: 800, CORE: 200, ECHO: 180}),
    _Filing("bravo 2024Q3/A", BRAVO, Q3, _filed(2025, 3, 3),
            {ACME: 2_500, BOLT: 500, ECHO: 500}, amends=AmendmentKind.RESTATEMENT),
    _Filing("alpha 2024Q4/A", ALPHA, Q4, _filed(2025, 3, 10),
            {CORE: 400}, amends=AmendmentKind.NEW_HOLDINGS),
)
# fmt: on


# --- worked out by hand -------------------------------------------------------
#
# Tables, so that a row can be checked against the books in the docstring by
# reading across it. Whole numbers are ints; Decimal("...") is a weight or a
# percentage to six places, as stored.

# fmt: off
#: (filer, period, security): (shares, value_usd, weight_pct, the filing it was read from)
SNAPSHOT: Final = {
    (ALPHA, Q1, ACME):   (1_000,  10_000,  10,             "alpha 2024Q1"),
    (ALPHA, Q1, BOLT):   (2_000,  40_000,  40,             "alpha 2024Q1"),
    (ALPHA, Q1, CORE):   (1_000,  50_000,  50,             "alpha 2024Q1"),
    (ALPHA, Q2, ACME):   (1_500,  18_000,  D("16.666667"), "alpha 2024Q2"),
    (ALPHA, Q2, BOLT):   (2_000,  40_000,  D("37.037037"), "alpha 2024Q2"),
    (ALPHA, Q2, DYNA):   (10_000, 50_000,  D("46.296296"), "alpha 2024Q2"),
    (ALPHA, Q3, ACME):   (3_666,  54_990,  D("27.495"),    "alpha 2024Q3"),
    (ALPHA, Q3, BOLT):   (1_000,  25_000,  D("12.5"),      "alpha 2024Q3"),
    (ALPHA, Q3, CORE):   (500,    20_000,  10,             "alpha 2024Q3"),
    (ALPHA, Q3, DYNA):   (10_001, 100_010, D("50.005"),    "alpha 2024Q3"),
    (ALPHA, Q4, ACME):   (3_666,  54_990,  D("27.495"),    "alpha 2024Q4"),
    (ALPHA, Q4, CORE):   (900,    45_000,  D("22.5"),      "alpha 2024Q4/A"),
    (ALPHA, Q4, DYNA):   (10_001, 100_010, D("50.005"),    "alpha 2024Q4"),
    (BRAVO, Q2, ACME):   (2_500,  30_000,  30,             "bravo 2024Q2"),
    (BRAVO, Q2, ECHO):   (700,    70_000,  70,             "bravo 2024Q2"),
    (BRAVO, Q3, ACME):   (2_500,  37_500,  D("37.5"),      "bravo 2024Q3/A"),
    (BRAVO, Q3, BOLT):   (500,    12_500,  D("12.5"),      "bravo 2024Q3/A"),
    (BRAVO, Q3, ECHO):   (500,    50_000,  50,             "bravo 2024Q3/A"),
    (BRAVO, Q4, ACME):   (2_500,  37_500,  D("37.5"),      "bravo 2024Q4"),
    (BRAVO, Q4, BOLT):   (2_500,  62_500,  D("62.5"),      "bravo 2024Q4"),
    (CHARLIE, Q1, ACME): (1_000,  10_000,  D("33.333333"), "charlie 2024Q1"),
    (CHARLIE, Q1, BOLT): (500,    10_000,  D("33.333333"), "charlie 2024Q1"),
    (CHARLIE, Q1, CORE): (200,    10_000,  D("33.333333"), "charlie 2024Q1"),
    (CHARLIE, Q2, ACME): (1_000,  12_000,  30,             "charlie 2024Q2"),
    (CHARLIE, Q2, BOLT): (1_000,  20_000,  50,             "charlie 2024Q2"),
    (CHARLIE, Q2, CORE): (200,    8_000,   20,             "charlie 2024Q2"),
    (CHARLIE, Q4, ACME): (800,    12_000,  30,             "charlie 2024Q4"),
    (CHARLIE, Q4, CORE): (200,    10_000,  25,             "charlie 2024Q4"),
    (CHARLIE, Q4, ECHO): (180,    18_000,  45,             "charlie 2024Q4"),
}

#: (filer, period, security): three lines of four,
#:     action,                 shares,      value_usd,      weight_pct,
#:     prev_period_of_report,  prev_shares, prev_value_usd, prev_weight_pct,
#:     shares_delta,           shares_delta_pct, value_delta, weight_delta
CHANGES: Final = {
    (ALPHA, Q1, ACME): (
        "new",   1_000,   10_000,   10,
        None,    None,    None,     None,
        1_000,   None,    10_000,   10,
    ),
    (ALPHA, Q1, BOLT): (
        "new",   2_000,   40_000,   40,
        None,    None,    None,     None,
        2_000,   None,    40_000,   40,
    ),
    (ALPHA, Q1, CORE): (
        "new",   1_000,   50_000,   50,
        None,    None,    None,     None,
        1_000,   None,    50_000,   50,
    ),
    (ALPHA, Q2, ACME): (
        "add",   1_500,   18_000,   D("16.666667"),
        Q1,      1_000,   10_000,   10,
        500,     50,      8_000,    D("6.666667"),
    ),
    (ALPHA, Q2, BOLT): (
        "hold",  2_000,   40_000,   D("37.037037"),
        Q1,      2_000,   40_000,   40,
        0,       0,       0,        D("-2.962963"),
    ),
    (ALPHA, Q2, CORE): (
        "exit",  0,       0,        0,
        Q1,      1_000,   50_000,   50,
        -1_000,  -100,    -50_000,  -50,
    ),
    (ALPHA, Q2, DYNA): (
        "new",   10_000,  50_000,   D("46.296296"),
        Q1,      None,    None,     None,
        10_000,  None,    50_000,   D("46.296296"),
    ),
    (ALPHA, Q3, ACME): (
        "add",   3_666,   54_990,   D("27.495"),
        Q2,      1_500,   18_000,   D("16.666667"),
        2_166,   D("144.4"), 36_990,   D("10.828333"),
    ),
    (ALPHA, Q3, BOLT): (
        "trim",  1_000,   25_000,   D("12.5"),
        Q2,      2_000,   40_000,   D("37.037037"),
        -1_000,  -50,     -15_000,  D("-24.537037"),
    ),
    (ALPHA, Q3, CORE): (
        "new",   500,     20_000,   10,
        Q2,      None,    None,     None,
        500,     None,    20_000,   10,
    ),
    (ALPHA, Q3, DYNA): (
        "hold",  10_001,  100_010,  D("50.005"),
        Q2,      10_000,  50_000,   D("46.296296"),
        1,       D("0.01"), 50_010,   D("3.708704"),
    ),
    (ALPHA, Q4, ACME): (
        "hold",  3_666,   54_990,   D("27.495"),
        Q3,      3_666,   54_990,   D("27.495"),
        0,       0,       0,        0,
    ),
    (ALPHA, Q4, BOLT): (
        "exit",  0,       0,        0,
        Q3,      1_000,   25_000,   D("12.5"),
        -1_000,  -100,    -25_000,  D("-12.5"),
    ),
    (ALPHA, Q4, CORE): (
        "add",   900,     45_000,   D("22.5"),
        Q3,      500,     20_000,   10,
        400,     80,      25_000,   D("12.5"),
    ),
    (ALPHA, Q4, DYNA): (
        "hold",  10_001,  100_010,  D("50.005"),
        Q3,      10_001,  100_010,  D("50.005"),
        0,       0,       0,        0,
    ),
    (BRAVO, Q2, ACME): (
        "new",   2_500,   30_000,   30,
        None,    None,    None,     None,
        2_500,   None,    30_000,   30,
    ),
    (BRAVO, Q2, ECHO): (
        "new",   700,     70_000,   70,
        None,    None,    None,     None,
        700,     None,    70_000,   70,
    ),
    (BRAVO, Q3, ACME): (
        "hold",  2_500,   37_500,   D("37.5"),
        Q2,      2_500,   30_000,   30,
        0,       0,       7_500,    D("7.5"),
    ),
    (BRAVO, Q3, BOLT): (
        "new",   500,     12_500,   D("12.5"),
        Q2,      None,    None,     None,
        500,     None,    12_500,   D("12.5"),
    ),
    (BRAVO, Q3, ECHO): (
        "trim",  500,     50_000,   50,
        Q2,      700,     70_000,   70,
        -200,    D("-28.571429"), -20_000,  -20,
    ),
    (BRAVO, Q4, ACME): (
        "hold",  2_500,   37_500,   D("37.5"),
        Q3,      2_500,   37_500,   D("37.5"),
        0,       0,       0,        0,
    ),
    (BRAVO, Q4, BOLT): (
        "add",   2_500,   62_500,   D("62.5"),
        Q3,      500,     12_500,   D("12.5"),
        2_000,   400,     50_000,   50,
    ),
    (BRAVO, Q4, ECHO): (
        "exit",  0,       0,        0,
        Q3,      500,     50_000,   50,
        -500,    -100,    -50_000,  -50,
    ),
    (CHARLIE, Q1, ACME): (
        "new",   1_000,   10_000,   D("33.333333"),
        None,    None,    None,     None,
        1_000,   None,    10_000,   D("33.333333"),
    ),
    (CHARLIE, Q1, BOLT): (
        "new",   500,     10_000,   D("33.333333"),
        None,    None,    None,     None,
        500,     None,    10_000,   D("33.333333"),
    ),
    (CHARLIE, Q1, CORE): (
        "new",   200,     10_000,   D("33.333333"),
        None,    None,    None,     None,
        200,     None,    10_000,   D("33.333333"),
    ),
    (CHARLIE, Q2, ACME): (
        "hold",  1_000,   12_000,   30,
        Q1,      1_000,   10_000,   D("33.333333"),
        0,       0,       2_000,    D("-3.333333"),
    ),
    (CHARLIE, Q2, BOLT): (
        "add",   1_000,   20_000,   50,
        Q1,      500,     10_000,   D("33.333333"),
        500,     100,     10_000,   D("16.666667"),
    ),
    (CHARLIE, Q2, CORE): (
        "hold",  200,     8_000,    20,
        Q1,      200,     10_000,   D("33.333333"),
        0,       0,       -2_000,   D("-13.333333"),
    ),
    (CHARLIE, Q4, ACME): (
        "trim",  800,     12_000,   30,
        Q2,      1_000,   12_000,   30,
        -200,    -20,     0,        0,
    ),
    (CHARLIE, Q4, BOLT): (
        "exit",  0,       0,        0,
        Q2,      1_000,   20_000,   50,
        -1_000,  -100,    -20_000,  -50,
    ),
    (CHARLIE, Q4, CORE): (
        "hold",  200,     10_000,   25,
        Q2,      200,     8_000,    20,
        0,       0,       2_000,    5,
    ),
    (CHARLIE, Q4, ECHO): (
        "new",   180,     18_000,   45,
        Q2,      None,    None,     None,
        180,     None,    18_000,   45,
    ),
}

#: mv_consensus_holdings, (period, security):
#:     (holder_count, total_value_usd, total_shares, avg_weight_pct, median_weight_pct, value_rank)
CONSENSUS: Final = {
    (Q1, ACME): (2, 20_000,  2_000,  D("21.666667"), D("21.666667"), 3),
    (Q1, BOLT): (2, 50_000,  2_500,  D("36.666667"), D("36.666667"), 2),
    (Q1, CORE): (2, 60_000,  1_200,  D("41.666667"), D("41.666667"), 1),
    (Q2, ACME): (3, 60_000,  5_000,  D("25.555556"), 30,             2),
    (Q2, BOLT): (2, 60_000,  3_000,  D("43.518519"), D("43.518519"), 2),
    (Q2, CORE): (1, 8_000,   200,    20,             20,             5),
    (Q2, DYNA): (1, 50_000,  10_000, D("46.296296"), D("46.296296"), 4),
    (Q2, ECHO): (1, 70_000,  700,    70,             70,             1),
    (Q3, ACME): (2, 92_490,  6_166,  D("32.4975"),   D("32.4975"),   2),
    (Q3, BOLT): (2, 37_500,  1_500,  D("12.5"),      D("12.5"),      4),
    (Q3, CORE): (1, 20_000,  500,    10,             10,             5),
    (Q3, DYNA): (1, 100_010, 10_001, D("50.005"),    D("50.005"),    1),
    (Q3, ECHO): (1, 50_000,  500,    50,             50,             3),
    (Q4, ACME): (3, 104_490, 6_966,  D("31.665"),    30,             1),
    (Q4, BOLT): (1, 62_500,  2_500,  D("62.5"),      D("62.5"),      3),
    (Q4, CORE): (2, 55_000,  1_100,  D("23.75"),     D("23.75"),     4),
    (Q4, DYNA): (1, 100_010, 10_001, D("50.005"),    D("50.005"),    2),
    (Q4, ECHO): (1, 18_000,  180,    45,             45,             5),
}

#: mv_quarter_flows, (period, security):
#:     (bought_value_usd, sold_value_usd, net_value_usd, net_shares,
#:      new_positions, exits, buyer_count, seller_count)
#: No Q1, and no ECHO in Q2: those are first periods.
FLOWS: Final = {
    (Q2, ACME): (6_000,  0,      6_000,   500,    0, 0, 1, 0),
    (Q2, BOLT): (10_000, 0,      10_000,  500,    0, 0, 1, 0),
    (Q2, CORE): (0,      50_000, -50_000, -1_000, 0, 1, 0, 1),
    (Q2, DYNA): (50_000, 0,      50_000,  10_000, 1, 0, 1, 0),
    (Q3, ACME): (32_490, 0,      32_490,  2_166,  0, 0, 1, 0),
    (Q3, BOLT): (12_500, 25_000, -12_500, -500,   1, 0, 1, 1),
    (Q3, CORE): (20_000, 0,      20_000,  500,    1, 0, 1, 0),
    (Q3, DYNA): (0,      0,      0,       1,      0, 0, 0, 0),
    (Q3, ECHO): (0,      20_000, -20_000, -200,   0, 0, 0, 1),
    (Q4, ACME): (0,      3_000,  -3_000,  -200,   0, 0, 0, 1),
    (Q4, BOLT): (50_000, 45_000, 5_000,   0,      0, 2, 1, 2),
    (Q4, CORE): (20_000, 0,      20_000,  400,    0, 0, 1, 0),
    (Q4, DYNA): (0,      0,      0,       0,      0, 0, 0, 0),
    (Q4, ECHO): (18_000, 50_000, -32_000, -320,   1, 1, 1, 1),
}

#: mv_filer_summary, (filer, period):
#:     (portfolio_value_usd, position_count, top10_weight_pct, turnover_pct)
SUMMARY: Final = {
    (ALPHA, Q1):   (100_000, 3, 100, None),
    (ALPHA, Q2):   (108_000, 3, 100, 53),
    (ALPHA, Q3):   (200_000, 4, 100, D("35.875")),
    (ALPHA, Q4):   (200_000, 3, 100, D("11.25")),
    (BRAVO, Q2):   (100_000, 2, 100, None),
    (BRAVO, Q3):   (100_000, 3, 100, D("16.25")),
    (BRAVO, Q4):   (100_000, 2, 100, 50),
    (CHARLIE, Q1): (30_000,  3, 100, None),
    (CHARLIE, Q2): (40_000,  3, 100, D("16.666667")),
    (CHARLIE, Q4): (40_000,  3, 100, D("51.25")),
}
# fmt: on


# --- building it ---------------------------------------------------------------


async def _filers(session: AsyncSession) -> dict[str, int]:
    """The three filers, each with the one CIK its filings are under."""
    ids = {}
    for slug, cik in CIKS.items():
        filer_id = await session.scalar(
            insert(Filer).values(name=slug, slug=slug).returning(Filer.id)
        )
        assert filer_id is not None
        await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
        ids[slug] = filer_id
    return ids


async def _load(session: AsyncSession, filers: dict[str, int], filing: _Filing) -> None:
    """``filing`` and its holdings, as the loader would leave them: loaded, guards run."""
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=filing.accession,
            cik=CIKS[filing.filer],
            filer_id=filers[filing.filer],
            form_type="13F-HR" if filing.amends is None else "13F-HR/A",
            amendment_kind=filing.amends,
            period_of_report=filing.period,
            filed_at=filing.filed,
            value_multiplier=1,
            parse_status="suspect" if filing.suspect else "ok",
            parse_notes=(
                [{"kind": "implied_price", "severity": "error", "detail": "values in thousands"}]
                if filing.suspect
                else None
            ),
        )
        .returning(Filing.id)
    )
    assert filing_id is not None
    stock = [
        (cusip, None, "SH", shares, (filing.values or {}).get(cusip, shares * price))
        for cusip, shares in filing.held.items()
        for price in [PRICES[filing.period][cusip]]
    ]
    for cusip, put_call, kind, amount, value in [*stock, *filing.lines]:
        await session.execute(
            pg_insert(Security)
            .values(cusip=cusip)
            .on_conflict_do_nothing(index_elements=[Security.cusip])
        )
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id).where(Security.cusip == cusip).scalar_subquery(),
                filer_id=filers[filing.filer],
                period_of_report=filing.period,
                cusip=cusip,
                value_usd=value,
                shares=amount,
                sshprnamt_type=kind,
                put_call=put_call,
            )
        )


#: The ways the fixture is published. Each must come out as the tables above.
BUILDS: Final = ("at once", "as filed", "newest first")


async def _publish(session: AsyncSession, build: str = "at once") -> None:
    """The fixture loaded and published ``build``'s way, then the views refreshed.

    ``at once`` loads everything and runs ``recompute --all``. The other two
    load one filing at a time and rebuild its pair, as ``ingest-filing`` does.
    As filed, the restatement arrives after bravo's Q4 has been published
    against Q2, and the amendment after alpha's Q4 has CORE as a hold. Newest
    first, the amendment is alpha's whole Q4 until its original arrives.
    """
    filers = await _filers(session)
    if build == "at once":
        for filing in FILINGS:
            await _load(session, filers, filing)
        await recompute(session, EVERYTHING)
    else:
        for filing in FILINGS if build == "as filed" else reversed(FILINGS):
            await _load(session, filers, filing)
            await recompute(session, Scope.of(await filing_pairs(session, filing.accession)))
    await refresh_views(session)


@pytest.fixture(params=BUILDS)
async def published(db_session: AsyncSession, request: pytest.FixtureRequest) -> AsyncSession:
    await _publish(db_session, request.param)
    return db_session


# --- reading it back -----------------------------------------------------------

_LABELS: Final = {filing.accession: filing.label for filing in FILINGS}


async def _snapshot(session: AsyncSession) -> dict[tuple[str, date, str], tuple[Any, ...]]:
    snapshot = PositionSnapshot
    rows = await session.execute(
        select(
            Filer.slug,
            snapshot.period_of_report,
            Security.cusip,
            snapshot.shares,
            snapshot.value_usd,
            snapshot.weight_pct,
            Filing.accession_no,
            snapshot.suspect,
        )
        .join(Filer, Filer.id == snapshot.filer_id)
        .join(Security, Security.id == snapshot.security_id)
        .join(Filing, Filing.id == snapshot.source_filing_id)
    )
    return {
        (slug, period, cusip): (shares, value, weight, _LABELS[accession], suspect)
        for slug, period, cusip, shares, value, weight, accession, suspect in rows.tuples()
    }


async def _changes(session: AsyncSession) -> dict[tuple[str, date, str], tuple[Any, ...]]:
    change = PositionChange
    rows = await session.execute(
        select(
            Filer.slug,
            change.period_of_report,
            Security.cusip,
            change.action,
            change.shares,
            change.value_usd,
            change.weight_pct,
            change.prev_period_of_report,
            change.prev_shares,
            change.prev_value_usd,
            change.prev_weight_pct,
            change.shares_delta,
            change.shares_delta_pct,
            change.value_delta,
            change.weight_delta,
            change.suspect,
        )
        .join(Filer, Filer.id == change.filer_id)
        .join(Security, Security.id == change.security_id)
    )
    return {(row[0], row[1], row[2]): tuple(row[3:]) for row in rows.tuples()}


async def _per_security(
    session: AsyncSession, view: Any, *columns: str
) -> dict[tuple[date, str], tuple[Any, ...]]:
    rows = await session.execute(
        select(
            view.c.period_of_report,
            Security.cusip,
            *(view.c[column] for column in columns),
            view.c.suspect,
        ).join(Security, Security.id == view.c.security_id)
    )
    return {(row[0], row[1]): tuple(row[2:]) for row in rows.tuples()}


async def _per_filer(session: AsyncSession) -> dict[tuple[str, date], tuple[Any, ...]]:
    view = FILER_SUMMARY
    rows = await session.execute(
        select(
            Filer.slug,
            view.c.period_of_report,
            view.c.portfolio_value_usd,
            view.c.position_count,
            view.c.top10_weight_pct,
            view.c.turnover_pct,
            view.c.suspect,
        ).join(Filer, Filer.id == view.c.filer_id)
    )
    return {(row[0], row[1]): tuple(row[2:]) for row in rows.tuples()}


def _unsuspected(expected: dict[Any, tuple[Any, ...]]) -> dict[Any, tuple[Any, ...]]:
    """``expected`` with ``suspect`` false on every row: nothing suspect is published here."""
    return {key: (*row, False) for key, row in expected.items()}


# --- every number, by hand ----------------------------------------------------


async def test_the_snapshot_is_the_one_worked_out_by_hand(published: AsyncSession) -> None:
    """Bravo's Q3 is the restatement alone: not the suspect original, not the
    two added up, and not withheld for the original being suspect."""
    assert await _snapshot(published) == _unsuspected(SNAPSHOT)


async def test_the_changes_are_the_ones_worked_out_by_hand(published: AsyncSession) -> None:
    """Including what the incremental builds have to undo: as filed, bravo's
    Q4 is first published against Q2, with ECHO's exit dated Q4 from 700
    shares, and the restatement moves all of it onto Q3."""
    assert await _changes(published) == _unsuspected(CHANGES)


async def test_consensus_holdings_are_the_ones_worked_out_by_hand(published: AsyncSession) -> None:
    """Q2's ACME and BOLT tie at $60,000, and share second place."""
    assert await _per_security(
        published,
        CONSENSUS_HOLDINGS,
        "holder_count",
        "total_value_usd",
        "total_shares",
        "avg_weight_pct",
        "median_weight_pct",
        "value_rank",
    ) == _unsuspected(CONSENSUS)


async def test_quarter_flows_are_the_ones_worked_out_by_hand(published: AsyncSession) -> None:
    """Charlie's BOLT exit, across its missing Q3, sells at the $20 it was last
    held at, not Q4's $25. DYNA's one share of drift is a hold: in net shares,
    and nobody's buying."""
    assert await _per_security(
        published,
        QUARTER_FLOWS,
        "bought_value_usd",
        "sold_value_usd",
        "net_value_usd",
        "net_shares",
        "new_positions",
        "exits",
        "buyer_count",
        "seller_count",
    ) == _unsuspected(FLOWS)


async def test_filer_summary_is_the_one_worked_out_by_hand(published: AsyncSession) -> None:
    assert await _per_filer(published) == _unsuspected(SUMMARY)


async def test_every_invariant_holds_on_the_fixture(published: AsyncSession) -> None:
    report = await reconcile(published, sample=None)

    assert {checked.invariant.name: checked.sample for checked in report.checked} == {
        invariant.name: () for invariant in INVARIANTS
    }
    assert (report.positions, report.changes, report.periods) == (
        len(SNAPSHOT),
        len(CHANGES),
        len({(filer, period) for filer, period, _ in SNAPSHOT}),
    )


# fmt: off
#: Alpha's Q3 changes with its Q2 withheld, against Q1 instead, laid out as :data:`CHANGES`.
#: CORE was in Q1, so its re-entry is a trim of 500, and DYNA, first seen in
#: Q2, is new. Their value deltas still add up: 200,000 - 100,000.
STEPPED_OVER: Final = {
    (ALPHA, Q3, ACME): (
        "add",   3_666,   54_990,   D("27.495"),
        Q1,      1_000,   10_000,   10,
        2_666,   D("266.6"), 44_990, D("17.495"),
    ),
    (ALPHA, Q3, BOLT): (
        "trim",  1_000,   25_000,   D("12.5"),
        Q1,      2_000,   40_000,   40,
        -1_000,  -50,     -15_000,  D("-27.5"),
    ),
    (ALPHA, Q3, CORE): (
        "trim",  500,     20_000,   10,
        Q1,      1_000,   50_000,   50,
        -500,    -50,     -30_000,  -40,
    ),
    (ALPHA, Q3, DYNA): (
        "new",   10_001,  100_010,  D("50.005"),
        Q1,      None,    None,     None,
        10_001,  None,    100_010,  D("50.005"),
    ),
}
# fmt: on


async def test_a_period_withheld_for_a_suspect_filing_is_stepped_over(
    db_session: AsyncSession,
) -> None:
    """Alpha's Q2 filing suspect, and published the default way: Q2 is gone
    from both tables, Q3 is against Q1, and Q2's exit of CORE and add of ACME
    are not anywhere. The fixture has no period withheld of its own, since
    bravo's suspect original was restated, so this is the one that is."""
    await _publish(db_session)
    await _suspect_after_publishing(db_session)
    await recompute(db_session, EVERYTHING)
    await refresh_views(db_session)

    snapshot, changes = await _snapshot(db_session), await _changes(db_session)
    report = await reconcile(db_session, sample=None)

    assert {key for key in snapshot if key[:2] == (ALPHA, Q2)} == set()
    assert {key: row for key, row in changes.items() if key[:2] == (ALPHA, Q3)} == (
        _unsuspected(STEPPED_OVER)
    )
    assert {key for key in changes if key[:2] == (ALPHA, Q2)} == set()
    assert report.clean


# --- each invariant catches what it is for -------------------------------------

#: (filer slug, period, CUSIP, problem): one violation as the report names it.
Found = tuple[str | None, date | None, str | None, str]


def _row(model: Any, filer: str, period: date, cusip: str) -> tuple[Any, ...]:
    """A ``WHERE`` for the one row of ``model`` at ``(filer, period, security)``."""
    return (
        model.filer_id == select(Filer.id).where(Filer.slug == filer).scalar_subquery(),
        model.period_of_report == period,
        model.security_id == select(Security.id).where(Security.cusip == cusip).scalar_subquery(),
    )


def _filing(label: str) -> Any:
    [filing] = [filing for filing in FILINGS if filing.label == label]
    return select(Filing.id).where(Filing.accession_no == filing.accession).scalar_subquery()


async def _set(
    session: AsyncSession, model: Any, filer: str, period: date, cusip: str, **values: Any
) -> None:
    await session.execute(
        update(model)
        .where(*_row(model, filer, period, cusip))
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def _drop_change_checks(session: AsyncSession) -> None:
    """The constraints that keep ``new`` and ``exit`` rows honest, dropped in the
    test's transaction: a row they forbid is the only way to see the check
    that restates them catch one."""
    await session.execute(
        text(
            "ALTER TABLE position_change "
            "DROP CONSTRAINT ck_position_change_new_when_not_held_before, "
            "DROP CONSTRAINT ck_position_change_an_exit_holds_nothing"
        )
    )


async def _misweighed(session: AsyncSession) -> None:
    """A weight 0.02 too high, copied faithfully onto the changes: consistent everywhere,
    so only the sum gives it away."""
    weight = D("0.02")
    await _set(
        session, PositionSnapshot, ALPHA, Q1, ACME, weight_pct=PositionSnapshot.weight_pct + weight
    )
    await _set(
        session,
        PositionChange,
        ALPHA,
        Q1,
        ACME,
        weight_pct=PositionChange.weight_pct + weight,
        weight_delta=PositionChange.weight_delta + weight,
    )
    await _set(
        session,
        PositionChange,
        ALPHA,
        Q2,
        ACME,
        prev_weight_pct=PositionChange.prev_weight_pct + weight,
        weight_delta=PositionChange.weight_delta - weight,
    )


async def _suspect_after_publishing(session: AsyncSession) -> None:
    """A guard tightened after alpha's Q2 was published, now failing its filing."""
    await session.execute(
        update(Filing).where(Filing.id == _filing("alpha 2024Q2")).values(parse_status="suspect")
    )


async def _read_from_the_restated_original(session: AsyncSession) -> None:
    await _set(session, PositionSnapshot, BRAVO, Q3, ACME, source_filing_id=_filing("bravo 2024Q3"))


async def _holding_deleted_after_publishing(session: AsyncSession) -> None:
    await session.execute(
        delete(Holding).where(Holding.filing_id == _filing("alpha 2024Q2"), Holding.cusip == DYNA)
    )


async def _change_lost(session: AsyncSession) -> None:
    await session.execute(delete(PositionChange).where(*_row(PositionChange, CHARLIE, Q2, BOLT)))


async def _change_from_a_stale_snapshot(session: AsyncSession) -> None:
    """Alpha's Q4 DYNA hold carrying Q2's share count: a change computed from
    a snapshot row since replaced, its deltas left as they were."""
    await _set(session, PositionChange, ALPHA, Q4, DYNA, shares=10_000)


async def _exit_lost(session: AsyncSession) -> None:
    await session.execute(delete(PositionChange).where(*_row(PositionChange, CHARLIE, Q4, BOLT)))


async def _re_entry_against_its_last_holding(session: AsyncSession) -> None:
    """The bug LAG alone would have: CORE, sold in Q2 and bought back in Q3,
    compared with its Q1 position, the last row for it, as an add."""
    await _set(
        session,
        PositionChange,
        ALPHA,
        Q3,
        CORE,
        action="add",
        prev_shares=1_000,
        prev_value_usd=50_000,
        prev_weight_pct=50,
    )


async def _against_the_calendar_quarter(session: AsyncSession) -> None:
    """Charlie's Q4 against Q3, which it never filed, instead of Q2."""
    await _set(session, PositionChange, CHARLIE, Q4, CORE, prev_period_of_report=Q3)


async def _new_with_previous_shares(session: AsyncSession) -> None:
    await _drop_change_checks(session)
    await _set(session, PositionChange, ALPHA, Q2, DYNA, prev_shares=0)


async def _trim_without_previous_shares(session: AsyncSession) -> None:
    await _drop_change_checks(session)
    await _set(session, PositionChange, BRAVO, Q3, ECHO, prev_shares=None)


async def _exit_with_shares(session: AsyncSession) -> None:
    await _drop_change_checks(session)
    await _set(session, PositionChange, ALPHA, Q2, CORE, shares=5)


async def _value_delta_a_dollar_off(session: AsyncSession) -> None:
    await _set(session, PositionChange, BRAVO, Q4, BOLT, value_delta=PositionChange.value_delta + 1)


async def _negative_shares(session: AsyncSession) -> None:
    """Negative in both tables, so that they agree and only the sign is wrong."""
    await _set(session, PositionSnapshot, CHARLIE, Q4, ECHO, shares=-180)
    await _set(session, PositionChange, CHARLIE, Q4, ECHO, shares=-180)


async def _corrected_without_a_refresh(session: AsyncSession) -> None:
    """Bravo's Q4 ACME re-ingested at 2,600 shares, worth the same, and
    published by ``recompute --no-refresh-views``. The tables are right, and
    four view rows are out of date: ACME's Q4 consensus shares, its flows for
    the quarter and for the year ending at it, and bravo's Q4 turnover."""
    await session.execute(
        update(Holding)
        .where(Holding.filing_id == _filing("bravo 2024Q4"), Holding.cusip == ACME)
        .values(shares=2_600)
    )
    await recompute(session, EVERYTHING)


async def _view_emptied(session: AsyncSession) -> None:
    await session.execute(text("REFRESH MATERIALIZED VIEW mv_quarter_flows WITH NO DATA"))


VALUE_DELTAS = "value deltas do not add up to the change in portfolio value"
DIFFERS = "has a row that differs from its live query"


@dataclass(frozen=True, slots=True)
class _Breakage:
    breaks: Callable[[AsyncSession], Awaitable[None]]
    caught: dict[str, set[Found]]
    """Every violation it causes, by invariant: the one it is for, and any other
    that the same row breaks."""
    refresh: bool = True
    """Refresh the views after, so that they agree with the broken tables and
    only the invariants about the tables see it."""


BREAKAGES: Final = {
    "a weight off, consistently": _Breakage(
        _misweighed,
        {"weights_sum_to_100": {(ALPHA, Q1, None, "weights do not sum to 100")}},
    ),
    "a source filing suspect after publishing": _Breakage(
        _suspect_after_publishing,
        {
            "snapshot_traces_to_filing": {
                (ALPHA, Q2, cusip, "its source filing is suspect") for cusip in (ACME, BOLT, DYNA)
            }
        },
    ),
    "read from the original a restatement replaced": _Breakage(
        _read_from_the_restated_original,
        {
            "snapshot_traces_to_filing": {
                (BRAVO, Q3, ACME, "its source filing does not count toward its period")
            }
        },
    ),
    "a holding deleted after publishing": _Breakage(
        _holding_deleted_after_publishing,
        {
            "snapshot_traces_to_filing": {
                (ALPHA, Q2, DYNA, "its source filing does not hold the security as stock")
            }
        },
    ),
    "a change lost": _Breakage(
        _change_lost,
        {
            "changes_match_snapshot": {(CHARLIE, Q2, BOLT, "a snapshot row with no change")},
            "value_deltas_add_up": {(CHARLIE, Q2, None, VALUE_DELTAS)},
        },
    ),
    "a change out of step with its snapshot row": _Breakage(
        _change_from_a_stale_snapshot,
        {
            "changes_match_snapshot": {
                (ALPHA, Q4, DYNA, "a change whose figures are not the snapshot's")
            }
        },
    ),
    "an exit lost": _Breakage(
        _exit_lost,
        {
            "changes_follow_previous_period": {(CHARLIE, Q4, BOLT, "sold out of with no exit")},
            "value_deltas_add_up": {(CHARLIE, Q4, None, VALUE_DELTAS)},
        },
    ),
    "a re-entry against its last holding": _Breakage(
        _re_entry_against_its_last_holding,
        {
            "changes_follow_previous_period": {
                (ALPHA, Q3, CORE, "previous figures with no snapshot row")
            }
        },
    ),
    "a change across a gap against the calendar quarter": _Breakage(
        _against_the_calendar_quarter,
        {
            "changes_follow_previous_period": {
                (
                    CHARLIE,
                    Q4,
                    CORE,
                    "a change against a period other than the filer's previous published one",
                )
            }
        },
    ),
    "a new position with previous shares": _Breakage(
        _new_with_previous_shares,
        {
            "new_and_exit_rows": {(ALPHA, Q2, DYNA, "a new position with previous shares")},
            "changes_follow_previous_period": {
                (ALPHA, Q2, DYNA, "previous figures with no snapshot row")
            },
        },
    ),
    "a trim without previous shares": _Breakage(
        _trim_without_previous_shares,
        {
            "new_and_exit_rows": {(BRAVO, Q3, ECHO, "no previous shares, but not new")},
            "changes_follow_previous_period": {
                (BRAVO, Q3, ECHO, "new, but held in the previous period")
            },
        },
    ),
    "an exit with shares": _Breakage(
        _exit_with_shares,
        {"new_and_exit_rows": {(ALPHA, Q2, CORE, "an exit with shares")}},
    ),
    "a value delta a dollar off": _Breakage(
        _value_delta_a_dollar_off,
        {"value_deltas_add_up": {(BRAVO, Q4, None, VALUE_DELTAS)}},
    ),
    "negative shares": _Breakage(
        _negative_shares,
        {"nothing_negative": {(CHARLIE, Q4, ECHO, "negative shares")}},
    ),
    "a correction published without refreshing the views": _Breakage(
        _corrected_without_a_refresh,
        {
            "views_match_live": {
                (None, Q4, ACME, f"mv_consensus_holdings {DIFFERS}"),
                (None, Q4, ACME, f"mv_quarter_flows {DIFFERS}"),
                (BRAVO, Q4, None, f"mv_filer_summary {DIFFERS}"),
                (None, Q4, ACME, f"mv_year_flows {DIFFERS}"),
            }
        },
        refresh=False,
    ),
    "a view emptied": _Breakage(
        _view_emptied,
        {
            "views_match_live": {
                (None, None, None, "mv_quarter_flows is not populated: refresh-views fills it")
            }
        },
        refresh=False,
    ),
}


def _caught(report: Reconciliation) -> dict[str, set[Found]]:
    return {
        checked.invariant.name: {
            (found.slug, found.period, found.cusip, found.problem) for found in checked.sample
        }
        for checked in report.failed
    }


@pytest.mark.parametrize("breakage", BREAKAGES.values(), ids=BREAKAGES.keys())
async def test_each_breakage_is_caught_by_the_invariant_for_it_on_its_row(
    db_session: AsyncSession, breakage: _Breakage
) -> None:
    await _publish(db_session)

    await breakage.breaks(db_session)
    if breakage.refresh:
        await refresh_views(db_session)
    report = await reconcile(db_session, sample=None)

    assert _caught(report) == breakage.caught
    assert all(checked.violations == len(checked.sample) for checked in report.checked)


def test_every_invariant_has_a_breakage_it_alone_catches() -> None:
    """Each invariant earns its place: some breakage gets past every other one."""
    alone = {
        name
        for breakage in BREAKAGES.values()
        if len(breakage.caught) == 1
        for name in breakage.caught
    }
    assert alone == {invariant.name for invariant in INVARIANTS}


async def test_a_period_published_suspect_passes_only_when_reconciled_as_one(
    db_session: AsyncSession,
) -> None:
    """Alpha's Q2 filing turns suspect, and the snapshot is rebuilt with
    ``--include-suspect``: every row of the period marked. Reconciled as a
    default publication, that is three rows from a suspect filing. Reconciled
    as what it is, it is clean, until one row's mark goes missing."""
    await _publish(db_session)
    await _suspect_after_publishing(db_session)
    await recompute(db_session, EVERYTHING, include_suspect=True)
    await refresh_views(db_session)

    as_default = await reconcile(db_session, sample=None)
    as_published = await reconcile(db_session, include_suspect=True, sample=None)
    await _set(db_session, PositionSnapshot, ALPHA, Q2, BOLT, suspect=False)
    await refresh_views(db_session)
    unmarked = await reconcile(db_session, include_suspect=True, sample=None)

    assert _caught(as_default) == {
        "snapshot_traces_to_filing": {
            (ALPHA, Q2, cusip, "its source filing is suspect") for cusip in (ACME, BOLT, DYNA)
        }
    }
    assert as_published.clean
    assert _caught(unmarked) == {
        "snapshot_traces_to_filing": {(ALPHA, Q2, BOLT, "its suspect flag is wrong")}
    }


# --- the command ---------------------------------------------------------------


@pytest.fixture
def committed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, migrated_engine: AsyncEngine
) -> Iterator[AsyncEngine]:
    """For the command, which reads through ``session_scope`` and so cannot see
    a test's rolled-back transaction: the fixture committed, and every table and
    view emptied around the test."""
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    _truncate(migrated_engine)
    asyncio.run(_commit(migrated_engine))
    yield migrated_engine
    _truncate(migrated_engine)
    # The command pointed logging at the runner's stderr, which is closed now.
    logging.getLogger().handlers.clear()
    configure_logging(make_settings(), stream=sys.__stderr__)


def _truncate(engine: AsyncEngine) -> None:
    async def run() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE ingestion_run, matview_refresh, position_change, "
                    "position_snapshot, holding, filing, security, filer_cik, filer "
                    "RESTART IDENTITY CASCADE"
                )
            )
            for view in MATERIALISED_VIEWS:
                await connection.execute(text(f"REFRESH MATERIALIZED VIEW {view.name}"))

    asyncio.run(run())


async def _commit(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as session:
        await _publish(session)
        await session.commit()


def _invariant_lines(failing: dict[str, list[str]]) -> list[str]:
    """What the command prints for each invariant, in order, with ``failing``'s
    lines under the ones that fail."""
    width = max(len(invariant.name) for invariant in INVARIANTS)
    lines = []
    for invariant in INVARIANTS:
        status = "FAIL" if invariant.name in failing else "ok  "
        lines.append(f"  {status}  {invariant.name:<{width}}  {invariant.rule}")
        lines += failing.get(invariant.name, [])
    return lines


def test_reconcile_exits_zero_and_lists_every_invariant_when_all_hold(
    committed: AsyncEngine,
) -> None:
    result = CliRunner().invoke(app, ["reconcile"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "reconcile  29 positions and 33 changes in 10 periods: all 8 invariants hold",
        *_invariant_lines({}),
    ]


def test_reconcile_exits_one_and_shows_the_rows_that_break_an_invariant(
    committed: AsyncEngine,
) -> None:
    """The exit lost, committed. Two invariants see it, each on its own row."""
    asyncio.run(_commit_breakage(committed, _exit_lost))

    result = CliRunner().invoke(app, ["reconcile"])

    assert result.exit_code == 1, result.output
    assert result.stdout.splitlines() == [
        "reconcile  29 positions and 32 changes in 10 periods: 2 of 8 invariants fail",
        *_invariant_lines(
            {
                "changes_follow_previous_period": [
                    "        1 row break it:",
                    "          charlie-fund  2024Q4  22222B202  sold out of with no exit",
                    "            1000.0000 shares, $20000.00 in 2024-06-30",
                ],
                "value_deltas_add_up": [
                    "        1 row break it:",
                    f"          charlie-fund  2024Q4  {VALUE_DELTAS}",
                    "            deltas sum to $20000.00; the portfolio went from $40000.00 to "
                    "$40000.00, a change of $0.00",
                ],
            }
        ),
    ]


def test_reconcile_shows_a_sample_and_counts_the_rest(committed: AsyncEngine) -> None:
    asyncio.run(_commit_breakage(committed, _suspect_after_publishing))

    result = CliRunner().invoke(app, ["reconcile", "--sample", "1"])

    assert result.exit_code == 1, result.output
    failing = result.stdout.splitlines()[3:7]
    assert failing == [
        "        3 rows break it:",
        "          alpha-capital  2024Q2  11111A101  its source filing is suspect",
        f"            source {FILINGS[2].accession}, suspect, for 2024-06-30",
        "          and 2 more",
    ]


async def _commit_breakage(
    engine: AsyncEngine, breaks: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """``breaks`` applied to the committed fixture, the views refreshed after."""
    async with AsyncSession(engine) as session:
        await breaks(session)
        await refresh_views(session)
        await session.commit()
