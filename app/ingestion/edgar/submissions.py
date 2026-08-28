"""data.sec.gov/submissions crawling.

What EDGAR knows about a submission that the submission's own documents do not
say. A ``primary_doc.xml`` names its filer, its period and its form type; it
does not name its accession number and it does not say when EDGAR accepted it.
Both of those are facts about the *filing event*, and this is where they come
from.

``filed_at`` is the one that has to be right
--------------------------------------------
:attr:`Submission.filed_at` is not metadata. It is the input to
:func:`~app.ingestion.normalisation.resolve_value_multiplier`, which decides
whether a 13F's ``value`` column is thousands of dollars or whole dollars — a
1000x error that does not announce itself, because every filer in a mis-parsed
quarter is wrong by the same factor and the rankings still look normal.

So this module reads ``acceptanceDateTime``, and not ``filingDate``.
``filingDate`` is a date in New York with no time on it; the units cutover falls
at midnight Eastern on 2023-01-03, and EDGAR accepts submissions until 22:00 ET,
so a filing accepted at 20:00 on 2 January is three hours from being placed on
the wrong side of the line by a date-only field. ``acceptanceDateTime`` is a
real instant with a real zone, and a submission that somehow lacks one is a
failure here rather than a guess (see :func:`_accepted_at`).

Paging
------
``filings.recent`` holds the most recent thousand submissions and no more. For a
filer of Berkshire's age that is about eight years, and every 13F older than
that lives in one of the JSON files listed under ``filings.files`` — same column
arrays, no ``filings`` wrapper. A lookup that only searched ``recent`` would
report "no such filing" for a document that is sitting in the archive, which is
the worst of the three possible answers.

The pages are searched in order and there are usually none of them. Nothing
skips a page by its declared ``filingFrom``/``filingTo`` range: those are filing
*dates*, and all we hold is an accession number, whose leading digits identify
the transmitting agent rather than any point in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from app.core.logging import get_logger
from app.ingestion.edgar.client import SEC_DATA_BASE, EdgarClient

logger = get_logger(__name__)

#: The column arrays this module reads out of a submissions document. EDGAR
#: publishes a parallel array per field, all the same length, rather than an
#: array of objects — so a "row" is an index, and reading one means indexing
#: several arrays with it.
_ACCESSION_COLUMN: Final = "accessionNumber"


class SubmissionNotFoundError(Exception):
    """No submission with that accession number was filed under that CIK.

    Its own type because the two things it can mean want different fixes and
    both are common. Either the accession number is wrong — a transposed digit,
    a filing that was withdrawn — or the CIK is: EDGAR indexes a submission
    under the CIK it was *filed for*, and a co-filer or a filing agent's CIK
    finds nothing even though the accession number is real.
    """

    def __init__(self, *, cik: str, accession_no: str) -> None:
        super().__init__(f"no submission {accession_no} is filed under CIK {cik}")
        self.cik = cik
        self.accession_no = accession_no


class SubmissionMalformedError(Exception):
    """The submissions document was found but could not be read.

    Separate from :class:`SubmissionNotFoundError` because it is not a fact about the
    filing: it means EDGAR changed the shape of this feed, or served a truncated
    body, and the answer is to look at the document rather than at the arguments.
    """


@dataclass(frozen=True, slots=True)
class Submission:
    """One row of EDGAR's submissions index: what EDGAR says about a filing.

    Deliberately not a :class:`~app.db.models.filing.Filing`, and deliberately
    not merged with :class:`~app.ingestion.parsers.thirteen_f.PrimaryDoc`. This
    is EDGAR's account of the submission; the primary document is the filer's
    account of its contents, and the loader is what joins them. Keeping them
    apart is what makes it possible to notice that they disagree.
    """

    accession_no: str
    """The dashed accession number, as asked for and as EDGAR spells it."""

    cik: str
    """The CIK the submission is indexed under, zero-padded to ten characters.

    Taken from the document's own ``cik`` field rather than echoed back from the
    argument, so an unpadded ``--cik 1067983`` comes back in the one spelling
    the database stores.
    """

    entity_name: str | None
    """EDGAR's name for the entity, for the operator reading a CLI summary.

    Never written to ``filer.name`` — that comes from the cover page, which is
    the filer's own statement of who they are for this period. This one exists
    so that a human can tell at a glance whether they fetched the filing they
    meant to.
    """

    form_type: str
    """``13F-HR``, ``13F-NT``, ``4``, as EDGAR spells it.

    The cover page carries this too, and the caller should compare them: EDGAR's
    is what the submission was *accepted as*, and a disagreement means one of the
    two documents is not what it claims to be.
    """

    filed_at: datetime
    """When EDGAR accepted the submission. Timezone-aware, always.

    From ``acceptanceDateTime``. See the module docstring for why this and not
    ``filingDate``, and :func:`_accepted_at` for what happens when it is absent.
    """

    period_of_report: date | None
    """The period the filing describes, from ``reportDate``. Null for forms
    that describe a day rather than a quarter, and for the ones EDGAR leaves
    blank."""

    primary_document: str | None
    """EDGAR's name for the filing's principal document, e.g. ``primary_doc.xml``.

    Advisory only, and not what
    :func:`~app.ingestion.edgar.documents.fetch_13f_documents` keys on: for a
    13F this field routinely carries a *rendering* path
    (``xslForm13F_X02/primary_doc.xml``) that is a stylesheet applied to the
    document rather than the document itself.
    """


async def find_submission(edgar: EdgarClient, *, cik: str, accession_no: str) -> Submission:
    """Look one accession number up in a filer's submissions index.

    :param edgar: An open client. One to three requests are made through it.
    :param cik: The CIK the filing is indexed under, padded or not.
    :param accession_no: The dashed accession number,
        ``0001067983-24-000011``.
    :returns: What EDGAR says about that submission.
    :raises SubmissionNotFoundError: The CIK's index does not contain it.
    :raises SubmissionMalformedError: The index could not be read as a submissions
        document, or the row it contains has no readable acceptance timestamp.
    """
    payload = await edgar.get_json(EdgarClient.submissions_url(cik))
    if not isinstance(payload, dict):
        raise SubmissionMalformedError(f"submissions for CIK {cik} is not a JSON object")

    entity_name = payload.get("name")
    indexed_cik = _padded_cik(payload.get("cik"), fallback=cik)
    filings = payload.get("filings")
    if not isinstance(filings, dict):
        raise SubmissionMalformedError(f"submissions for CIK {cik} has no 'filings' object")

    row = _row_for(filings.get("recent"), accession_no)

    # Only when the recent thousand did not have it. Each page is a separate
    # request against a rate-limited host, so they are walked lazily and in
    # order rather than fetched up front.
    for page in _older_pages(filings):
        if row is not None:
            break
        logger.info("submissions.page", cik=indexed_cik, page=page, accession_no=accession_no)
        row = _row_for(await edgar.get_json(f"{SEC_DATA_BASE}/submissions/{page}"), accession_no)

    if row is None:
        raise SubmissionNotFoundError(cik=indexed_cik, accession_no=accession_no)

    return Submission(
        accession_no=accession_no,
        cik=indexed_cik,
        entity_name=entity_name if isinstance(entity_name, str) and entity_name else None,
        form_type=_text(row, "form") or "",
        filed_at=_accepted_at(row, accession_no=accession_no),
        period_of_report=_report_date(row, accession_no=accession_no),
        primary_document=_text(row, "primaryDocument"),
    )


def _older_pages(filings: dict[str, Any]) -> list[str]:
    """Names of the overflow JSON files, oldest submissions last.

    Tolerant of a missing or oddly-shaped ``files``, because its absence is the
    normal case: a filer with fewer than a thousand submissions has every one of
    them in ``recent`` and this key is an empty list.
    """
    files = filings.get("files")
    if not isinstance(files, list):
        return []
    return [
        entry["name"]
        for entry in files
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]


def _row_for(columns: Any, accession_no: str) -> dict[str, Any] | None:
    """Find one accession number in a set of parallel column arrays.

    EDGAR publishes a submissions index as one array per field rather than an
    array of objects, so a row is an index into all of them at once. This finds
    the index in ``accessionNumber`` and gathers the same position out of every
    other array, which is what lets the rest of this module read a row as a
    mapping.

    Arrays shorter than ``accessionNumber`` are skipped at that index rather
    than padded, so a feed that gains a sparse column does not turn every lookup
    into an ``IndexError``.
    """
    if not isinstance(columns, dict):
        return None
    accessions = columns.get(_ACCESSION_COLUMN)
    if not isinstance(accessions, list):
        return None

    try:
        index = accessions.index(accession_no)
    except ValueError:
        return None

    return {
        name: values[index]
        for name, values in columns.items()
        if isinstance(values, list) and index < len(values)
    }


def _text(row: dict[str, Any], name: str) -> str | None:
    """A column's value as non-empty text, or ``None``.

    EDGAR writes ``""`` rather than ``null`` for a field that does not apply —
    ``reportDate`` on a Form 8-K, ``primaryDocument`` on an old paper filing —
    so an empty string means absent and is folded into ``None`` here rather than
    at four call sites.
    """
    value = row.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _accepted_at(row: dict[str, Any], *, accession_no: str) -> datetime:
    """``acceptanceDateTime`` as a timezone-aware instant.

    A failure rather than a fallback when it is missing or naive, and that is
    the whole point of the function. The alternatives available here —
    ``filingDate`` at midnight, or the same timestamp read as UTC — differ from
    the truth by exactly the hours in which the units cutover changes its
    answer, so a guess would be a silent 1000x error on the values of one
    filing. EDGAR has published this field on every submission back to 1998; an
    absence means something has changed and should be looked at, not papered
    over.

    :raises SubmissionMalformedError: If the field is absent, unparseable, or carries
        no timezone.
    """
    raw = _text(row, "acceptanceDateTime")
    if raw is None:
        raise SubmissionMalformedError(
            f"{accession_no}: submissions index has no acceptanceDateTime, so the "
            "filing cannot be placed against the 2023 units cutover"
        )
    try:
        # 3.11+ accepts the trailing 'Z' EDGAR writes; earlier versions did not,
        # which is why this is spelled as a plain fromisoformat and not as a
        # strptime with a hardcoded format that would reject the day EDGAR
        # starts writing an offset instead.
        accepted = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SubmissionMalformedError(
            f"{accession_no}: acceptanceDateTime is not an ISO-8601 instant ({raw!r})"
        ) from exc

    if accepted.tzinfo is None or accepted.tzinfo.utcoffset(accepted) is None:
        raise SubmissionMalformedError(
            f"{accession_no}: acceptanceDateTime {raw!r} carries no timezone, and the "
            "two available guesses straddle the 2023 units cutover"
        )
    return accepted


def _report_date(row: dict[str, Any], *, accession_no: str) -> date | None:
    """``reportDate`` as a date, absent meaning absent.

    Unlike :func:`_accepted_at` this one tolerates absence — a Form 4 describes
    a day and has no period at all — but not a malformed value, which is the
    same rule the parsers follow: a field that is present has to be readable, or
    the parser and the archived document disagree with nothing recording that
    they do.
    """
    raw = _text(row, "reportDate")
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SubmissionMalformedError(
            f"{accession_no}: reportDate is not an ISO-8601 date ({raw!r})"
        ) from exc


def _padded_cik(value: Any, *, fallback: str) -> str:
    """The document's own CIK, zero-padded, falling back to what we asked for.

    A fallback rather than a failure because this is a display and bookkeeping
    field: the request has already succeeded by the time it is read, and the CIK
    we asked for is by construction the one the answer belongs to.
    """
    for candidate in (value, fallback):
        try:
            return f"{int(str(candidate).strip()):010d}"
        except (TypeError, ValueError):
            continue
    return str(fallback)
