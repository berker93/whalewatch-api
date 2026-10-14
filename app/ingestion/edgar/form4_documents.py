"""Finding a Form 4's documents inside its filing directory.

A Form 4 is one XML document, the ``ownershipDocument``, named whatever the
filing agent likes: ``form4.xml``, ``wk-form4_1791925767.xml``,
``edgardoc.xml``. So the directory is listed (``index.json``) and the document
found in it, as for a 13F's information table, and confirmed by its root
element before it is accepted.

What the document does not say is when EDGAR accepted it, and the filing row
needs that instant (``filing.filed_at``). For a 13F it comes from the filer's
submissions index, but that is one large document per company, and the
development cache would serve an issuer's from before the Form 4 existed. The
filing's own SGML header (``{accession}.hdr.sgml``) says it in one small
request that never changes::

    <ACCEPTANCE-DATETIME>20261013170929

It is in New York time, and carries no zone. The header is not in the
directory listing, but EDGAR serves it for every filing.

Three requests per filing, then: the listing, the document and the header.
All three are archived, so a re-parse needs none of them again.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.ingestion.edgar.client import EdgarClient
from app.ingestion.edgar.documents import FilingDocumentsError, xml_names

logger = get_logger(__name__)

#: The zone EDGAR's acceptance times are written in.
EDGAR_ZONE: Final = ZoneInfo("America/New_York")

#: The root element, with an optional namespace prefix. See
#: :mod:`app.ingestion.edgar.documents` on matching the local name.
_OWNERSHIP_ROOT: Final = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?ownershipDocument\b")

#: As in :mod:`app.ingestion.edgar.documents`: the root is near the top.
_SNIFF_BYTES: Final = 4096

_ACCEPTED: Final = re.compile(rb"<ACCEPTANCE-DATETIME>\s*(\d{14})")


class Form4DocumentsError(FilingDocumentsError):
    """A filing directory or header was not what a Form 4's is."""


@dataclass(frozen=True, slots=True)
class Form4Documents:
    """The bytes of one Form 4's documents, and where each came from."""

    index_url: str
    index: bytes
    """EDGAR's ``index.json`` for the directory, as served."""

    document_url: str
    document: bytes
    """The ``ownershipDocument``, as served. What the parser reads."""

    header_url: str
    header: bytes
    """``{accession}.hdr.sgml``, as served: EDGAR's own account of the filing."""

    filed_at: datetime
    """When EDGAR accepted the filing: :func:`accepted_at` of :attr:`header`."""


def header_url(cik: int | str, accession_no: str) -> str:
    """``.../data/{cik}/{accession, undashed}/{accession}.hdr.sgml``."""
    directory = EdgarClient.filing_index_url(cik, accession_no).rsplit("/", 1)[0]
    return f"{directory}/{accession_no}.hdr.sgml"


async def fetch_form4_documents(
    edgar: EdgarClient, *, cik: str, accession_no: str
) -> Form4Documents:
    """List the filing's directory, find its ownership document, and fetch it
    and the filing's header.

    :param cik: Any CIK the filing is filed under: the issuer's or a reporting
        owner's. EDGAR keeps the directory under each.
    :raises Form4DocumentsError: The directory has no XML document whose root
        is ``ownershipDocument``, or more than one, or the header has no
        acceptance time.
    """
    index_url = EdgarClient.filing_index_url(cik, accession_no)
    index = await edgar.get_bytes(index_url)
    base_url = index_url.rsplit("/", 1)[0]
    try:
        names = xml_names(json.loads(index))
    except ValueError as malformed:
        raise Form4DocumentsError(f"{index_url} is not JSON: {malformed}") from malformed

    found: list[tuple[str, bytes]] = []
    for name in names:
        url = f"{base_url}/{name}"
        document = await edgar.get_bytes(url)
        if is_ownership_document(document):
            found.append((url, document))
        else:
            logger.warning("form4.not_an_ownership_document", accession_no=accession_no, url=url)
    if len(found) != 1:
        raise Form4DocumentsError(
            f"{accession_no}: expected one <ownershipDocument> in {base_url}, found "
            f"{len(found)} among {names or ['(no XML files)']}"
        )
    [(document_url, document)] = found

    header_at = header_url(cik, accession_no)
    header = await edgar.get_bytes(header_at)
    return Form4Documents(
        index_url=index_url,
        index=index,
        document_url=document_url,
        document=document,
        header_url=header_at,
        header=header,
        filed_at=accepted_at(header),
    )


def is_ownership_document(document: bytes) -> bool:
    """Whether ``document`` is a Forms 3/4/5 ``ownershipDocument``, by its root."""
    return _OWNERSHIP_ROOT.search(document[:_SNIFF_BYTES]) is not None


def accepted_at(header: bytes) -> datetime:
    """``<ACCEPTANCE-DATETIME>`` as an aware datetime in New York.

    :raises Form4DocumentsError: There is none, or it is not a time.
    """
    match = _ACCEPTED.search(header)
    if match is None:
        raise Form4DocumentsError("the filing header has no <ACCEPTANCE-DATETIME>")
    raw = match.group(1).decode()
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=EDGAR_ZONE)
    except ValueError as malformed:
        raise Form4DocumentsError(f"the filing header's acceptance time {raw!r}") from malformed
