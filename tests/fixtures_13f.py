"""The golden 13F fixtures: what they are, and how their snapshots are built.

``tests/fixtures/13f`` holds nine real filings, downloaded once and committed.
Next to each pair of documents sits ``snapshot.json``, the parsers' output over
exactly those bytes. :func:`snapshot` is the one function that builds one, and
both the test suite and ``make fixtures`` call it — a snapshot the tests compare
against and a snapshot the regeneration command writes have to be the same
object, or the suite is comparing against a format instead of a result.

Why snapshots rather than field assertions
------------------------------------------
The hand-written fixtures in ``tests/fixtures/thirteen_f`` assert field by
field, which is right for them: each is a document constructed to be broken in
one specific way, and the assertion names the break. These nine are the opposite
kind of test. They are messy real documents nobody designed, and the regression
worth catching in them is the one nobody predicted — a row that stops parsing,
a value that gains a digit, a warning that appears. An exact comparison against
a stored result catches every one of those without anyone having had to think
of it first, and the diff *is* the diagnosis.

That only works if regenerating is deliberate. ``make fixtures`` rewrites the
snapshots; nothing else does, and a test never writes one. A snapshot that
updated itself on failure would turn every regression into a clean run.

Purity
------
A snapshot is a function of the two committed documents and nothing else — no
clock, no network, no ``filed_at``. That is what makes it reproducible on any
machine in any year. The one fact the parsers cannot supply, the filing's
acceptance timestamp, lives in ``manifest.json`` and is used by the tests that
need it (the units cutover) rather than baked into the expectations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.ingestion.parsers.thirteen_f import (
    InformationTable,
    PrimaryDoc,
    parse_information_table,
    parse_primary_doc,
)

FIXTURES: Final = Path(__file__).parent / "fixtures" / "13f"
MANIFEST: Final = FIXTURES / "manifest.json"
README: Final = FIXTURES / "README.md"

#: The slugs of the two filings that straddle the 2023-01-03 units cutover:
#: consecutive quarters from one manager, one filed in thousands and one in
#: whole dollars. Named here rather than in the test because the pair is a
#: property of the fixture set — replace either filing and the guard it exists
#: for stops meaning anything.
CUTOVER_PAIR: Final = ("berkshire-2022q3-thousands", "berkshire-2022q4-dollars")

#: Every 13F one manager filed for one period, in the order EDGAR accepted
#: them: an original, a restatement of it, and a new-holdings amendment after
#: the restatement. Named for the same reason as the pair above — the
#: amendment tests resolve the period from these, and drop any one of them and
#: the period they resolve is not the one the manager reported.
RESTATED_PERIOD: Final = (
    "berkshire-2023q3-original",
    "berkshire-2023q3-restatement",
    "berkshire-2023q3-new-holdings",
)

#: The next period from the same manager: an original, and one new-holdings
#: amendment adding the position its confidential treatment request withheld.
ADDED_TO_PERIOD: Final = ("berkshire-2023q4-original", "berkshire-2023q4-new-holdings")


@dataclass(frozen=True, slots=True)
class Fixture:
    """One committed filing: where its bytes are, and what EDGAR said about it.

    The fields that are not in either document — :attr:`filed_at` above all —
    come from the submissions index at download time and are recorded in the
    manifest. ``filed_at`` decides the units multiplier and appears nowhere in
    the XML, so a fixture without it could not be normalised at all.
    """

    slug: str
    cik: str
    accession_no: str
    filer_name: str
    form_type: str
    period_of_report: str
    filed_at: datetime
    note: str
    primary_doc_url: str
    information_table_url: str

    @property
    def directory(self) -> Path:
        return FIXTURES / self.slug

    @property
    def snapshot_path(self) -> Path:
        return self.directory / "snapshot.json"

    def primary_doc_bytes(self) -> bytes:
        """Bytes, not text: the document declares its own encoding, and lxml is
        right to refuse a decoded string that carries the declaration."""
        return (self.directory / "primary_doc.xml").read_bytes()

    def information_table_bytes(self) -> bytes:
        return (self.directory / "information_table.xml").read_bytes()

    def parse(self) -> tuple[PrimaryDoc, InformationTable]:
        return (
            parse_primary_doc(self.primary_doc_bytes()),
            parse_information_table(self.information_table_bytes()),
        )

    def stored_snapshot(self) -> dict[str, Any]:
        """The approved expectation, as committed."""
        return dict(json.loads(self.snapshot_path.read_text()))


def load_fixtures() -> tuple[Fixture, ...]:
    """Every fixture in the manifest, in manifest order."""
    manifest = json.loads(MANIFEST.read_text())
    return tuple(
        Fixture(**{**entry, "filed_at": datetime.fromisoformat(entry["filed_at"])})
        for entry in manifest["fixtures"]
    )


def by_slug(slug: str) -> Fixture:
    """The one fixture with this slug, for a test that needs a specific filing."""
    for candidate in load_fixtures():
        if candidate.slug == slug:
            return candidate
    raise KeyError(f"no fixture named {slug!r} in {MANIFEST}")


def snapshot(target: Fixture) -> dict[str, Any]:
    """Parse a fixture's two documents into the structure the snapshot stores.

    ``mode="json"`` throughout, which renders every ``Decimal`` as a string. A
    JSON number is an IEEE 754 double, and a file whose job is to make a
    one-digit change in a share count visible is the last place to let one
    through a binary float on the way to disk.
    """
    cover, table = target.parse()
    return {
        "fixture": target.slug,
        "accession_no": target.accession_no,
        "cover": cover.model_dump(mode="json"),
        "information_table": {
            # Derived from the rows below and stored anyway. A row count and a
            # sum at the top of the file are what turn "some line in a 2,000-row
            # diff changed" into "we lost a position" without reading the diff.
            "row_count": len(table.rows),
            "value_total": str(table.value_total),
            "warning_count": len(table.warnings),
            "rows": [row.model_dump(mode="json") for row in table.rows],
            "warnings": [warning.model_dump(mode="json") for warning in table.warnings],
        },
    }


#: Matches the placeholder strings :func:`dumps` parks in the row lists. ``\x00``
#: cannot occur in JSON output any other way — the encoder escapes it — so the
#: substitution cannot collide with real content.
_PLACEHOLDER: Final = re.compile(r'"\\u0000(\d+)"')


def dumps(payload: dict[str, Any]) -> str:
    """Render a snapshot with each holding on exactly one line.

    Indented JSON puts every field of every row on its own line, which on the
    2,263-row fixture is thirty thousand lines and a diff nobody reads. One line
    per row is a tenth of that and reads like a ledger: a changed holding is one
    changed line, and a dropped one is one deleted line.

    Done by substitution rather than by hand-rolling a serialiser, so the file
    is written by :mod:`json` and is therefore JSON.
    """
    compact: list[str] = []

    def park(rows: list[dict[str, Any]]) -> list[str]:
        start = len(compact)
        compact.extend(json.dumps(row, ensure_ascii=False) for row in rows)
        return [f"\x00{index}" for index in range(start, len(compact))]

    table = payload["information_table"]
    parked = {
        **payload,
        "information_table": {
            **table,
            "rows": park(table["rows"]),
            "warnings": park(table["warnings"]),
        },
    }
    text = json.dumps(parked, indent=2, ensure_ascii=False)
    return _PLACEHOLDER.sub(lambda match: compact[int(match.group(1))], text) + "\n"


def write_snapshots() -> int:
    """Rewrite every snapshot from the committed documents. ``make fixtures``."""
    for target in load_fixtures():
        text = dumps(snapshot(target))
        before = target.snapshot_path.read_text() if target.snapshot_path.exists() else None
        target.snapshot_path.write_text(text)
        state = "unchanged" if text == before else ("new" if before is None else "REWRITTEN")
        print(f"  {state:>9}  {target.slug}/snapshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(write_snapshots())
