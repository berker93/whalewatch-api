"""Finding the two documents of a 13F inside a filing directory.

A 13F is two XML files sitting in one EDGAR archive directory: the cover page
and the information table. Fetching them ought to be two URLs, and it is not,
because only one of the two has a name you can predict.

The cover page is ``primary_doc.xml`` on every 13F EDGAR has. The information
table is whatever the filing agent called it — ``form13fInfoTable.xml``,
``infotable.xml``, ``0001234-info.xml``, ``56757.xml`` (that last one is
Berkshire's, and the number is an internal job id). There is no naming rule to
follow and no field in the submissions index that points at it: EDGAR's
``primaryDocument`` names the *cover page*, and even then it often names a
stylesheet path (``xslForm13F_X02/primary_doc.xml``) rather than the document.

So the directory is listed and the file is identified by what is in it.

Why the name is not enough on its own
-------------------------------------
The obvious rule — "the one ``.xml`` that is not ``primary_doc.xml``" — is right
almost always, and its failure mode is the one this codebase spends the most
effort avoiding. A directory with a second unrelated XML file (a cover letter, a
correspondence attachment, an exhibit) hands the parser a document with no
``<infoTable>`` elements in it. That is not an error:
:func:`~app.ingestion.parsers.thirteen_f.parse_information_table` returns an
empty table, because an empty table is exactly what a legitimate ``13F-NT``
produces. The filing then loads with zero positions, and a fund holding nothing
looks like a fund that sold everything rather than like a bug.

Hence :func:`_fetch_information_table`: the candidate is confirmed against
:data:`_INFORMATION_TABLE_ROOT` before it is accepted, and a directory whose
candidates are all something else is a hard failure naming the files it
rejected. A guess about which file is the portfolio is not worth making,
because the wrong guess is indistinguishable from data.

Sniffing rather than parsing, because the check has to be cheap enough to run on
a candidate that is about to be thrown away, and because a 5,000-row information
table should not be parsed twice to find out it was the right file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from app.core.logging import get_logger
from app.ingestion.edgar.client import EdgarClient

logger = get_logger(__name__)

#: The cover page's name, which — unlike the information table's — is fixed.
PRIMARY_DOC_NAME: Final = "primary_doc.xml"

#: How much of a document to look at before deciding what it is. The root
#: element is in the first line or two of both documents; four kilobytes is
#: generous enough to survive a comment block, an XML declaration and a
#: schema-location attribute list, and small enough to be free.
_SNIFF_BYTES: Final = 4096

#: Root elements, matched with an optional namespace prefix. Both documents
#: declare a default namespace in practice, but nothing stops an agent from
#: binding a prefix instead — ``<ns1:informationTable>`` is as valid as
#: ``<informationTable>`` and means the same thing. Matching the local name is
#: the same decision the parsers make with their ``local-name()`` XPaths, and
#: for the same reason: the 13F namespace URI has changed between schema
#: versions, and a rule pinned to one of them fails silently rather than loudly.
_INFORMATION_TABLE_ROOT: Final = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?informationTable\b")
_EDGAR_SUBMISSION_ROOT: Final = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?edgarSubmission\b")


class FilingDocumentsError(Exception):
    """A filing directory did not contain the documents a 13F is made of.

    Its own type so that a caller can tell "EDGAR does not have what we expected
    at this URL" from "EDGAR would not talk to us" (:class:`~app.ingestion.edgar
    .client.EdgarRateLimited`) and from "the document is malformed"
    (:class:`~app.ingestion.parsers.errors.FilingParseError`). The three want
    three different responses, and only the middle one is worth retrying.
    """


@dataclass(frozen=True, slots=True)
class FilingDocuments:
    """The bytes of one 13F's two documents, and where each came from.

    Bytes rather than parsed objects: this module's job is finding and fetching,
    and the parsers are pure functions over exactly these bytes. Keeping the two
    apart is what makes a parser bug repairable by re-running it over documents
    already archived rather than by re-crawling EDGAR.

    The URLs travel with the bytes because
    :attr:`~app.db.models.filing.Filing.source_url` wants the one the document
    actually came from. Reconstructing it later from the accession number means
    applying today's archive convention to a document fetched under yesterday's,
    which is a link that 404s in whatever incident report it lands in.
    """

    primary_doc_url: str
    """Where the cover page was fetched from."""

    primary_doc: bytes
    """``primary_doc.xml``, exactly as EDGAR served it."""

    info_table_url: str | None
    """Where the information table came from, or ``None`` — see below."""

    info_table: bytes | None
    """The information table, or ``None`` when the filing has none.

    ``None`` is a real and correct outcome, not a failure: a ``13F-NT`` reports
    no holdings — it says "everything I hold is reported by another manager" —
    and its directory contains the cover page and nothing else. The caller
    treats this as an empty table rather than as a missing document; what it
    must not do is treat a missing document as an empty table, which is why
    every *other* way of ending up with no rows raises from this module.
    """


async def fetch_13f_documents(
    edgar: EdgarClient, *, cik: str, accession_no: str
) -> FilingDocuments:
    """List a filing directory, identify its two documents, and fetch them.

    :param edgar: An open client. Two to three requests are made through it —
        the directory listing, the cover page, and the information table when
        the filing has one.
    :param cik: The CIK whose archive the filing lives under, padded or not.
        Note this is EDGAR's *path* CIK, which is the subject filer's; the
        accession number's leading digits belong to the transmitting agent and
        do not name a directory that exists.
    :param accession_no: The dashed accession number.
    :returns: The bytes of both documents and the URLs they came from.
    :raises FilingDocumentsError: If the directory has no cover page, or has
        candidate XML files none of which is an information table.

    Ambiguity is resolved by fetching. When the directory holds more than one
    candidate the likely-looking names are tried first (see :func:`_ranked`) and
    the first document whose root element is ``<informationTable>`` wins, so the
    common case costs one fetch and the pathological one costs a fetch per stray
    XML file in a directory that has at most a handful.
    """
    index_url = EdgarClient.filing_index_url(cik, accession_no)
    listing = await edgar.get_json(index_url)

    # The listing URL minus its filename, which is the directory every document
    # in it hangs off. Derived rather than rebuilt from the parts, so there is
    # one place that knows how an archive URL is spelled.
    base_url = index_url.rsplit("/", 1)[0]
    names = _xml_names(listing)

    primary_name = next((name for name in names if name.casefold() == PRIMARY_DOC_NAME), None)
    candidates = _ranked([name for name in names if name != primary_name])

    primary_url, primary_doc, candidates = await _fetch_primary_doc(
        edgar,
        base_url=base_url,
        accession_no=accession_no,
        primary_name=primary_name,
        candidates=candidates,
    )

    if not candidates:
        # Nothing left in the directory to be an information table. Correct for
        # a 13F-NT and wrong for anything else, which is the caller's judgement
        # to make: it knows the form type, and this module only knows what files
        # exist.
        logger.info("filing.no_information_table", accession_no=accession_no, url=base_url)
        return FilingDocuments(
            primary_doc_url=primary_url,
            primary_doc=primary_doc,
            info_table_url=None,
            info_table=None,
        )

    info_url, info_table = await _fetch_information_table(
        edgar, base_url=base_url, accession_no=accession_no, candidates=candidates
    )
    return FilingDocuments(
        primary_doc_url=primary_url,
        primary_doc=primary_doc,
        info_table_url=info_url,
        info_table=info_table,
    )


async def _fetch_primary_doc(
    edgar: EdgarClient,
    *,
    base_url: str,
    accession_no: str,
    primary_name: str | None,
    candidates: list[str],
) -> tuple[str, bytes, list[str]]:
    """Fetch the cover page, falling back to sniffing when it is not named.

    Every 13F in the archive names this file ``primary_doc.xml``, so the
    fallback exists for the day one does not rather than for anything seen. It
    is worth the twenty lines because the alternative failure — no cover page —
    loses the period, the form type and the filer's own totals, which is every
    field that makes the information table interpretable.

    :returns: The URL, the bytes, and the candidate list with whatever was
        consumed as the cover page removed from it, so a directory holding two
        oddly-named files cannot offer the same one twice.
    """
    if primary_name is not None:
        url = f"{base_url}/{primary_name}"
        return url, await edgar.get_bytes(url), candidates

    for name in candidates:
        url = f"{base_url}/{name}"
        document = await edgar.get_bytes(url)
        if _EDGAR_SUBMISSION_ROOT.search(document[:_SNIFF_BYTES]) is not None:
            logger.warning(
                "filing.primary_doc_renamed",
                accession_no=accession_no,
                url=url,
                expected=PRIMARY_DOC_NAME,
            )
            return url, document, [other for other in candidates if other != name]

    raise FilingDocumentsError(
        f"{accession_no}: no cover page in {base_url} — "
        f"no {PRIMARY_DOC_NAME}, and none of {candidates or ['(no XML files)']} "
        "is an <edgarSubmission> document"
    )


async def _fetch_information_table(
    edgar: EdgarClient, *, base_url: str, accession_no: str, candidates: list[str]
) -> tuple[str, bytes]:
    """Fetch the first candidate that is actually an information table.

    Confirmed rather than assumed even when there is only one candidate, because
    an unconfirmed wrong file does not raise anywhere downstream — it parses to
    zero rows, and a filing with zero rows is a valid ``13F-NT``. The whole
    portfolio would go missing without a single error, which is the failure this
    function exists to convert into an exception.
    """
    for name in candidates:
        url = f"{base_url}/{name}"
        document = await edgar.get_bytes(url)
        if _INFORMATION_TABLE_ROOT.search(document[:_SNIFF_BYTES]) is not None:
            return url, document
        logger.warning("filing.not_an_information_table", accession_no=accession_no, url=url)

    raise FilingDocumentsError(
        f"{accession_no}: none of {candidates} in {base_url} is an <informationTable> "
        "document, so the filing's holdings could not be located"
    )


def _xml_names(listing: Any) -> list[str]:
    """The XML filenames in an EDGAR ``index.json``, in the order it listed them.

    The document is ``{"directory": {"item": [{"name": ..., "size": ...}, ...]}}``,
    and the ``type`` field on each item is an icon name rather than a MIME type,
    so it says nothing useful about what the file is. The filter is therefore on
    the suffix alone.

    Names containing a path separator are dropped. EDGAR lists the stylesheet
    subdirectory a rendered filing is served through
    (``xslForm13F_X02/primary_doc.xml``), and that entry is the same document
    with a stylesheet bolted on — fetching it gets a transformed copy rather
    than the bytes the filer submitted, which is not what belongs in an archive.
    """
    if not isinstance(listing, dict):
        raise FilingDocumentsError("filing index is not a JSON object")
    directory = listing.get("directory")
    if not isinstance(directory, dict):
        raise FilingDocumentsError("filing index has no 'directory' object")
    items = directory.get("item")
    if not isinstance(items, list):
        raise FilingDocumentsError("filing index has no 'directory.item' array")

    return [
        name
        for item in items
        if isinstance(item, dict)
        and isinstance(name := item.get("name"), str)
        and name.casefold().endswith(".xml")
        and "/" not in name
    ]


def _ranked(names: list[str]) -> list[str]:
    """Candidates in the order worth fetching them, likeliest first.

    Purely an optimisation over fetch count, never a decision: whatever this
    puts first is still confirmed by its root element before it is accepted, and
    a directory with one candidate is unaffected. The names that carry a hint
    ("infotable", "form13finfotable", "0001234-info") go first so that the
    common multi-file directory costs one fetch rather than several; document
    order breaks ties, because that is the order EDGAR listed them and an
    arbitrary-but-stable ordering is what makes a failure reproducible.
    """
    return sorted(
        names, key=lambda name: (0 if "info" in name.casefold() else 1, names.index(name))
    )
