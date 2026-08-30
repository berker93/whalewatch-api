"""Download one real 13F from EDGAR into ``tests/fixtures/13f/``.

Run by hand, never by a test and never by ``make fixtures``. The fixtures are
committed documents: the suite's whole premise is that a parser change is the
only thing that can move a snapshot, and a suite that re-downloaded its inputs
would lose that. This script exists to *add* a seventh fixture, not to refresh
the six that are already here.

    make fixtures-fetch a=0000950123-22-012275 cik=1067983 slug=berkshire-2022q3-thousands

It writes ``primary_doc.xml`` and ``information_table.xml`` into the fixture's
directory, records the filing's identity in ``manifest.json``, and stops. The
snapshot is a separate, deliberate step — ``make fixtures`` — because a
downloaded document and an approved expectation are two different decisions.

Whitespace between elements is collapsed and one ``<infoTable>`` is put on each
line. Content is never touched: what is committed still parses to exactly what
EDGAR served, and a two-thousand-row table is still a file a human can diff.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Final

from app.core.config import get_settings

FIXTURES: Final = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "13f"
MANIFEST: Final = FIXTURES / "manifest.json"

#: EDGAR blocks traffic whose User-Agent does not identify a contact, so this
#: borrows the application's own — one fewer place that has to be configured,
#: and the same address that appears in the crawler's requests.
_USER_AGENT: Final = get_settings().sec_user_agent

#: SEC asks for no more than ten requests a second. This script makes about
#: five in total, so the sleep is politeness rather than throttling.
_DELAY_SECONDS: Final = 0.15

#: Same root-element sniff the fetcher in ``app.ingestion.edgar.documents``
#: does, and for the same reason: the information table's filename is whatever
#: the filing agent called it, and the only reliable test is what is inside.
_INFORMATION_TABLE_ROOT: Final = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?informationTable\b")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    time.sleep(_DELAY_SECONDS)
    with urllib.request.urlopen(request, timeout=60) as response:
        return bytes(response.read())


def _tidy(xml: bytes) -> bytes:
    """Collapse whitespace between elements, then break the rows onto lines.

    ``>\\s+<`` matches only where an element ends and the next begins, so text
    content — including text that is padded with spaces — is left exactly as
    filed. What disappears is indentation, which is a fifth of the bytes on a
    large filing and none of the meaning.
    """
    collapsed = re.sub(rb">\s+<", b"><", xml).strip()
    return (
        re.sub(
            rb"<((?:[A-Za-z_][\w.\-]*:)?(?:infoTable|/informationTable))\b", rb"\n<\1", collapsed
        )
        + b"\n"
    )


def _filing_metadata(cik: str, accession_no: str) -> dict[str, Any]:
    """The submission's own facts, from EDGAR's index rather than the document.

    ``filed_at`` is the one the parsers cannot supply and the one the units
    depend on — see :func:`app.ingestion.normalisation.resolve_value_multiplier`
    — so it is recorded here, at the only moment we are talking to the source
    that knows it.
    """
    index = json.loads(_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))
    recent = index["filings"]["recent"]
    for position, number in enumerate(recent["accessionNumber"]):
        if number == accession_no:
            return {
                "filer_name": index["name"],
                "form_type": recent["form"][position],
                "period_of_report": recent["reportDate"][position],
                # ISO 8601 with a zone, because a naive timestamp cannot be
                # placed on either side of the 2023-01-03 units cutover.
                "filed_at": recent["acceptanceDateTime"][position].replace("Z", "+00:00"),
            }
    raise SystemExit(f"{accession_no} is not among the recent filings of CIK {cik}")


def _documents(cik: str, accession_no: str) -> tuple[str, bytes, str, bytes]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no.replace('-', '')}"
    primary_doc_url = f"{base}/primary_doc.xml"
    primary_doc = _get(primary_doc_url)

    listing = json.loads(_get(f"{base}/index.json"))["directory"]["item"]
    for item in listing:
        name = str(item["name"])
        if not name.lower().endswith(".xml") or name == "primary_doc.xml":
            continue
        candidate = _get(f"{base}/{name}")
        if _INFORMATION_TABLE_ROOT.search(candidate[:4096]):
            return primary_doc_url, primary_doc, f"{base}/{name}", candidate
    raise SystemExit(f"no information table in {base}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cik", required=True, help="the subject filer's CIK, whose archive holds it"
    )
    parser.add_argument("--accession", required=True, help="dashed accession number")
    parser.add_argument(
        "--slug", required=True, help="fixture directory name under tests/fixtures/13f"
    )
    parser.add_argument(
        "--note", default="", help="the README line: why this filing is worth keeping"
    )
    args = parser.parse_args(argv)

    metadata = _filing_metadata(args.cik, args.accession)
    primary_doc_url, primary_doc, info_table_url, info_table = _documents(args.cik, args.accession)

    directory = FIXTURES / args.slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "primary_doc.xml").write_bytes(_tidy(primary_doc))
    (directory / "information_table.xml").write_bytes(_tidy(info_table))

    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"fixtures": []}
    entry = {
        "slug": args.slug,
        "cik": f"{int(args.cik):010d}",
        "accession_no": args.accession,
        **metadata,
        "note": args.note,
        "primary_doc_url": primary_doc_url,
        "information_table_url": info_table_url,
    }
    fixtures = [f for f in manifest["fixtures"] if f["slug"] != args.slug] + [entry]
    manifest["fixtures"] = sorted(fixtures, key=lambda f: str(f["slug"]))
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"{args.slug}: wrote {directory}, now run `make fixtures` to snapshot it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
