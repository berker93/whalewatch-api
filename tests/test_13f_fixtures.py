"""The golden fixture suite: nine real 13Fs, parsed and compared to snapshots.

These are documents nobody designed. The hand-written fixtures next door —
``tests/fixtures/thirteen_f`` — are each broken in one specific way and asserted
field by field, which is the right shape for a test of a known hazard. This file
is for the hazards nobody has thought of yet: a filing agent's spacing, a
``<figi>`` that arrives one quarter, a manager who reports the same CUSIP on
four rows. An exact comparison against a stored result catches all of it, and
the diff of a snapshot is the diagnosis.

Nothing here touches the network. The filings were downloaded once and
committed; a unit test that fetched its own input would be testing EDGAR's
uptime and would fail on a plane. See ``scripts/fetch_13f_fixture.py`` for how a
tenth fixture gets added, and ``tests/fixtures_13f.py`` for the snapshot
format and why regeneration is a separate, deliberate command.

**A failing snapshot test is not fixed by running ``make fixtures``.** It is
fixed by reading the diff and deciding whether the parser got better or worse.
Regenerating is how an improvement gets recorded, after someone has looked.
"""

from decimal import Decimal

import pytest

from app.db.models.enums import AmendmentKind
from app.ingestion.normalisation import normalise_filing
from tests.fixtures_13f import (
    ADDED_TO_PERIOD,
    CUTOVER_PAIR,
    README,
    RESTATED_PERIOD,
    Fixture,
    by_slug,
    load_fixtures,
    snapshot,
)

FIXTURES = load_fixtures()
SLUGS = [target.slug for target in FIXTURES]

REGENERATE = (
    "the parsers no longer produce the committed snapshot. Read the diff first: "
    "if the new output is right, record it with `make fixtures`."
)


@pytest.fixture(params=FIXTURES, ids=SLUGS)
def filing(request: pytest.FixtureRequest) -> Fixture:
    """Each committed filing in turn, so every test below runs over all nine."""
    target: Fixture = request.param
    return target


# --- the snapshots -----------------------------------------------------------


def test_the_parsed_filing_matches_its_snapshot(filing: Fixture) -> None:
    """The whole suite, in one assertion — staged so the failure is readable.

    A bare ``assert actual == expected`` over the 2,263-row fixture prints a
    diff of two 700KB structures, which is a way of not reporting the failure.
    Comparing the cover page, then the totals, then the rows one at a time means
    the first thing pytest prints is the row that changed.
    """
    actual = snapshot(filing)
    expected = filing.stored_snapshot()

    assert actual["cover"] == expected["cover"], f"cover page: {REGENERATE}"

    parsed, stored = actual["information_table"], expected["information_table"]
    summary = ("row_count", "value_total", "warning_count")
    assert [parsed[key] for key in summary] == [stored[key] for key in summary], (
        f"the table's shape changed — rows or total: {REGENERATE}"
    )

    for position, (got, want) in enumerate(zip(parsed["rows"], stored["rows"], strict=True), 1):
        assert got == want, f"row {position} of {filing.slug}: {REGENERATE}"
    assert parsed["warnings"] == stored["warnings"], f"parser warnings: {REGENERATE}"

    # The staged assertions above cover every field this snapshot has today.
    # This one covers the fields it grows tomorrow.
    assert actual == expected, REGENERATE


# --- the filer's own checksum ------------------------------------------------


def test_every_row_the_filer_declared_is_a_row_we_parsed(filing: Fixture) -> None:
    """``tableEntryTotal`` against the rows, which is the check the loader runs.

    A parser that silently skips a malformed ``<infoTable>`` returns a portfolio
    that is merely smaller than the real one, and a fund that is smaller than it
    was looks exactly like a fund that sold. All nine of these filings are clean,
    so the count is exact: dropped rows would show up here as a shortfall.
    """
    cover, table = filing.parse()
    assert cover.table_entry_total is not None, "every fixture here is a holdings report"
    dropped = [warning for warning in table.warnings if warning.dropped]
    assert len(table.rows) + len(dropped) == cover.table_entry_total
    assert dropped == [], f"{filing.slug} parses whole today; a dropped row is the regression"


def test_the_summed_rows_match_the_cover_pages_own_total(filing: Fixture) -> None:
    """Both sides unscaled, which is what makes this a check on the units too.

    :attr:`PrimaryDoc.table_value_total` and the row values are in whatever the
    filing filed in, so they are comparable to each other before any multiplier
    is applied — and comparing them there catches a parser that scaled one side.
    """
    cover, table = filing.parse()
    assert cover.table_value_total is not None
    assert table.value_total == Decimal(cover.table_value_total)


# --- the cutover regression guard --------------------------------------------


def test_the_cutover_pair_reports_comparable_totals() -> None:
    """Two consecutive quarters of one portfolio must not differ by 1000x.

    This is the single most expensive bug this codebase can have. The units of
    ``value`` changed on 2023-01-03 — thousands of dollars before, whole dollars
    after — and the two fixtures here are the same manager's filings from either
    side of it, six weeks and one quarter apart. Berkshire did not triple in
    size or lose 99.9% of it over that quarter, so once both are normalised the
    totals have to land within a hair of each other.

    The assertion on the *raw* totals is what makes the rest of it mean
    something: unscaled, these two documents disagree by three orders of
    magnitude. Any change that stops applying the multiplier, applies it to the
    wrong side of the cutover, or keys it on ``period_of_report`` instead of
    ``filed_at`` fails here.
    """
    before, after = (by_slug(slug) for slug in CUTOVER_PAIR)
    normalised = []
    for target in (before, after):
        cover, table = target.parse()
        result = normalise_filing(filed_at=target.filed_at, cover=cover, table=table)
        normalised.append((table.value_total, result))

    (raw_before, thousands), (raw_after, dollars) = normalised
    assert (thousands.value_multiplier, dollars.value_multiplier) == (1000, 1)

    # Unnormalised, the pair is off by ~1000x — the error being guarded against.
    assert raw_after / raw_before > 900

    total_before = sum((holding.value_usd for holding in thousands.holdings), Decimal(0))
    total_after = sum((holding.value_usd for holding in dollars.holdings), Decimal(0))
    assert Decimal("0.5") < total_after / total_before < 2, (
        f"one quarter apart, {before.slug} totals {total_before} and {after.slug} "
        f"totals {total_after} — a portfolio does not move that far in a quarter, "
        "so the multiplier is wrong on one of them"
    )


def test_the_cutover_pair_is_one_manager_in_consecutive_quarters() -> None:
    """What makes the guard above a guard, asserted so it cannot quietly lapse.

    Swap either fixture for a different manager or a distant quarter and the
    magnitude comparison still passes while testing nothing.
    """
    before, after = (by_slug(slug) for slug in CUTOVER_PAIR)
    assert before.cik == after.cik
    assert before.period_of_report == "2022-09-30"
    assert after.period_of_report == "2022-12-31"
    assert before.filed_at.year == 2022 and after.filed_at.year == 2023


@pytest.mark.parametrize(
    ("slugs", "kinds"),
    [
        (RESTATED_PERIOD, [None, AmendmentKind.RESTATEMENT, AmendmentKind.NEW_HOLDINGS]),
        (ADDED_TO_PERIOD, [None, AmendmentKind.NEW_HOLDINGS]),
    ],
    ids=["restated", "added-to"],
)
def test_each_amended_period_is_one_managers_whole_period_in_filing_order(
    slugs: tuple[str, ...], kinds: list[AmendmentKind | None]
) -> None:
    """What the amendment integration tests resolve, asserted so it cannot lapse.

    Resolution is by acceptance order within one CIK and one period. A set that
    mixed managers or periods, or listed its filings out of order, would still
    load and resolve — to an answer about nothing the manager reported.
    """
    period = [by_slug(slug) for slug in slugs]
    covers = [target.parse()[0] for target in period]

    assert len({target.cik for target in period}) == 1
    assert len({target.period_of_report for target in period}) == 1
    assert [target.filed_at for target in period] == sorted(target.filed_at for target in period)
    assert [cover.amendment_kind for cover in covers] == kinds
    assert [cover.amendment_no for cover in covers] == [None, *range(1, len(slugs))]


# --- the fixtures stay interesting -------------------------------------------


def test_the_amendment_fixtures_cover_both_kinds() -> None:
    """A restatement and a new-holdings amendment, which are not interchangeable.

    Reading one as the other is the most expensive mistake in the pipeline: a
    NEW HOLDINGS amendment carries the positions that were withheld under
    confidential treatment, and loading its single row as a restatement replaces
    the 138 rows the quarter's original reported.
    """
    restatement, _ = by_slug("berkshire-2023q3-restatement").parse()
    assert restatement.amendment_kind is AmendmentKind.RESTATEMENT
    assert restatement.confidential_omitted is True, "positions withheld, to be revealed later"

    new_holdings, table = by_slug("berkshire-2023q4-new-holdings").parse()
    assert new_holdings.amendment_kind is AmendmentKind.NEW_HOLDINGS
    assert len(table.rows) == 1, "one position released when the confidentiality lapsed"


def test_the_options_fixture_still_carries_puts_and_calls() -> None:
    """An option's value is its underlying's notional, not the premium paid.

    Which means these rows are the ones that break a naive price sanity check,
    and a fixture set without them tests a market that does not exist.
    """
    _, table = by_slug("soros-2026q2-options").parse()
    put_call = {row.put_call for row in table.rows}
    assert {"Put", "Call"} <= put_call
    assert "PRN" in {row.sh_prn_type for row in table.rows}, "principal amounts, not share counts"


def test_the_large_fixture_is_large() -> None:
    """Two thousand rows is where streaming stops being a style preference.

    :func:`~app.ingestion.parsers.thirteen_f._info_tables` clears each element
    as it goes so that peak memory is one row rather than one document, and a
    change that reverts to ``fromstring`` passes every other test in the suite.
    """
    _, table = by_slug("point72-2025q3-large").parse()
    assert len(table.rows) >= 2000


def test_the_same_cusip_can_appear_on_several_rows() -> None:
    """Berkshire splits positions across the managers that hold them.

    Not a defect and not something to collapse here: rows differing only by
    ``otherManager`` have to be summed by the loader, and a parser that
    deduplicated them would hide from the loader that there was anything to sum.
    """
    _, table = by_slug("berkshire-2022q3-thousands").parse()
    cusips = [row.cusip for row in table.rows]
    assert len(cusips) > len(set(cusips))


# --- the fixtures stay documented --------------------------------------------


def test_every_fixture_is_named_in_the_readme() -> None:
    """The README is the only place that says *why* a filing is worth keeping.

    Without that, a fixture is 900KB of XML that nobody dares delete and nobody
    can justify, and the first person to trim the suite deletes the wrong one.
    """
    text = README.read_text()
    for target in FIXTURES:
        assert target.slug in text, f"{target.slug} has no README line"
        assert target.accession_no in text, f"{target.slug}'s accession number is not in the README"


def test_the_manifest_and_the_directories_agree() -> None:
    """No orphan directory, no manifest entry pointing at documents that are gone."""
    on_disk = {path.name for path in README.parent.iterdir() if path.is_dir()}
    assert on_disk == set(SLUGS)
    for target in FIXTURES:
        assert target.snapshot_path.exists(), f"{target.slug} has never been snapshotted"
