"""A session with Senate eFD (efdsearch.senate.gov), through the US fetcher.

eFD is a Django site, and it behaves like one. Before anything else it wants
its terms accepted: a GET of ``/search/home/`` serves an agreement form with a
CSRF token in it and sets a ``csrftoken`` cookie; a POST of that form with
``prohibition_agreement=1`` sets the session cookie and redirects to
``/search/``. Every later POST — the search endpoint is one — needs the token
again, in the form and in ``X-CSRFToken``, and a ``Referer`` on the same host,
because Django checks the Referer on HTTPS. Miss any of it and the answer is a
403 or a redirect back to the agreement.

Sessions expire. An expired one looks like a request for a report page or a
search being redirected to ``/search/home/``, or a 403 from the CSRF check.
:class:`SenateEfd` re-accepts the terms once and repeats the request; a second
refusal straight after a fresh acceptance is not expiry, and raises
:class:`SenateEfdSessionError`. A 403 on the agreement page itself usually
means the request did not come from a US address.

Archive before parse
--------------------
What eFD answers is written to the raw store before it is returned
(:func:`~app.storage.raw.senate_efd_report_key`,
:func:`~app.storage.raw.senate_efd_search_key`), as every EDGAR document is: a
parser fix is then a re-parse of archived bytes, never a re-crawl. The terms
pages are not archived — they carry no data. Neither is an answer that is not
what was asked for (an error status, an HTML page where JSON was expected):
it raises instead, and nothing parses it.

Nothing here falls back to the archive when eFD cannot be reached. The
fetcher's failures propagate as they are.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from urllib.parse import urljoin, urlsplit

from app.core.logging import get_logger
from app.ingestion.remote.fetcher import CookieSession, Fetcher, FetchRequest, FetchResponse
from app.storage.raw import RawStore, senate_efd_report_key, senate_efd_search_key

logger = get_logger(__name__)

EFD_BASE: Final = "https://efdsearch.senate.gov"

_HOME: Final = "/search/home/"
_SEARCH: Final = "/search/"
_SEARCH_DATA: Final = "/search/report/data/"

#: eFD's ``report_types`` value for a Periodic Transaction Report — the
#: STOCK Act filing a senator makes within 45 days of a trade.
PTR_REPORT_TYPE: Final = 11

#: The largest page the search endpoint serves. It is a DataTables backend,
#: and quietly truncates a larger ``length`` to this.
MAX_SEARCH_PAGE: Final = 100

_CSRF_INPUT: Final = re.compile(
    rb"""name=["']csrfmiddlewaretoken["']\s+value=["']([^"']+)["']"""
    rb"""|value=["']([^"']+)["']\s+name=["']csrfmiddlewaretoken["']"""
)
_AGREEMENT_FIELD: Final = b'name="prohibition_agreement"'
_REPORT_PATH: Final = re.compile(r"^/search/view/([a-z-]+)/([0-9a-fA-F-]+)/?$")


class SenateEfdError(Exception):
    """eFD answered, and not with what was asked for."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SenateEfdSessionError(SenateEfdError):
    """eFD would not keep a session: the terms, the CSRF token, or the address."""


@dataclass(frozen=True, slots=True)
class Archived:
    """One answer from eFD, and where its bytes were archived."""

    key: str
    body: bytes
    fetched_at: datetime


class SenateEfd:
    """One eFD session. Not for concurrent use: it is one cookie jar.

    The terms are accepted on first use, or by :meth:`accept_terms` up front
    (``fetcher-check`` does, to prove the whole path works)::

        async with open_fetcher(settings) as fetcher, open_raw_store(settings) as store:
            efd = SenateEfd(fetcher, store, user_agent=settings.sec_user_agent)
            page = await efd.search_reports(submitted_from=date(2026, 10, 1))
    """

    def __init__(
        self,
        fetcher: Fetcher,
        store: RawStore,
        *,
        user_agent: str,
        base_url: str = EFD_BASE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = CookieSession(fetcher)
        self._store = store
        self._user_agent = user_agent
        self._base = base_url.rstrip("/")
        self._clock = clock
        self._accepted = False

    # --- the terms -------------------------------------------------------------

    async def accept_terms(self) -> None:
        """Accept eFD's terms, starting a fresh session.

        :raises SenateEfdSessionError: If eFD will not serve the agreement or
            will not take it.
        """
        self._session.clear()
        self._accepted = False
        home = await self._session.fetch(self._request(_HOME))
        if home.status != 200:
            raise SenateEfdSessionError(
                f"eFD answered {home.status} for its agreement page"
                + (" — is the fetcher outside the US?" if home.status == 403 else ""),
                status=home.status,
            )
        token = _csrf_token(home.body) or self._session.cookie("csrftoken")
        if token is None:
            raise SenateEfdSessionError("eFD's agreement page carried no CSRF token")

        accepted = await self._session.fetch(
            self._request(
                _HOME,
                method="POST",
                form=[("prohibition_agreement", "1"), ("csrfmiddlewaretoken", token)],
                referer=_HOME,
            )
        )
        if accepted.status != 200 or _is_agreement(accepted):
            raise SenateEfdSessionError(
                f"eFD did not accept the terms: {accepted.status} at {_path(accepted.url)}",
                status=accepted.status,
            )
        self._accepted = True
        logger.info("senate_efd.terms_accepted")

    # --- what eFD serves --------------------------------------------------------

    async def search_reports(
        self,
        *,
        submitted_from: date,
        submitted_to: date | None = None,
        start: int = 0,
        length: int = MAX_SEARCH_PAGE,
        report_types: Sequence[int] = (PTR_REPORT_TYPE,),
    ) -> Archived:
        """One page of reports filed from ``submitted_from``, archived, as served.

        eFD's JSON, unparsed: ``{"draw", "recordsTotal", "recordsFiltered",
        "data": [[first, last, office, link, date], ...], "result"}``.
        """
        if not 0 < length <= MAX_SEARCH_PAGE:
            raise ValueError(f"length must be 1 to {MAX_SEARCH_PAGE}, not {length}")

        def request() -> FetchRequest:
            return self._request(
                _SEARCH_DATA,
                method="POST",
                form=[
                    ("start", str(start)),
                    ("length", str(length)),
                    ("report_types", _js_list(report_types)),
                    ("filer_types", "[]"),
                    ("submitted_start_date", _efd_datetime(submitted_from)),
                    (
                        "submitted_end_date",
                        ""
                        if submitted_to is None
                        else _efd_datetime(submitted_to, end_of_day=True),
                    ),
                    ("candidate_state", ""),
                    ("senator_state", ""),
                    ("office_id", ""),
                    ("first_name", ""),
                    ("last_name", ""),
                    ("csrfmiddlewaretoken", self._csrf()),
                ],
                referer=_SEARCH,
                csrf=True,
            )

        response = await self._send(request)
        _expect(response, "application/json", what=f"search page at {start}")
        fetched_at = self._clock()
        key = await self._store.put(
            senate_efd_search_key(fetched_at, start), response.body, "application/json"
        )
        return Archived(key=key, body=response.body, fetched_at=fetched_at)

    async def report(self, link: str, *, overwrite: bool = False) -> Archived:
        """One report page — ``/search/view/ptr/{id}/`` — archived, as served.

        ``link`` is a path or a whole eFD URL, as a search row's link has it.
        Archived write-once like every raw document, unless ``overwrite``.
        """
        path = _path(urljoin(self._base + "/", link))
        match = _REPORT_PATH.match(path)
        if match is None:
            raise ValueError(f"{link!r} is not an eFD report link")
        kind, report_id = match.groups()

        response = await self._send(lambda: self._request(path))
        _expect(response, "text/html", what=f"{kind} report {report_id}")
        key = await self._store.put(
            senate_efd_report_key(kind, report_id),
            response.body,
            "text/html",
            overwrite=overwrite,
        )
        return Archived(key=key, body=response.body, fetched_at=self._clock())

    # --- plumbing ------------------------------------------------------------------

    async def _send(self, build: Callable[[], FetchRequest]) -> FetchResponse:
        """``build()`` sent in an accepted session, re-accepting once on expiry.

        A callable rather than a request, because the request carries the CSRF
        token and a re-acceptance replaces it.
        """
        if not self._accepted:
            await self.accept_terms()
        response = await self._session.fetch(build())
        if not _session_refused(response):
            return response

        logger.info("senate_efd.session_expired", status=response.status, at=_path(response.url))
        await self.accept_terms()
        response = await self._session.fetch(build())
        if _session_refused(response):
            raise SenateEfdSessionError(
                f"eFD refused a session it had just accepted: {response.status} at "
                f"{_path(response.url)}",
                status=response.status,
            )
        return response

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        form: list[tuple[str, str]] | None = None,
        referer: str | None = None,
        csrf: bool = False,
    ) -> FetchRequest:
        headers = [("User-Agent", self._user_agent), ("Accept", "*/*")]
        if referer is not None:
            headers.append(("Referer", self._base + referer))
        if csrf:
            headers.append(("X-CSRFToken", self._csrf()))
        return FetchRequest(
            url=self._base + path,
            method="POST" if method == "POST" else "GET",
            headers=headers,
            form=form,
        )

    def _csrf(self) -> str:
        token = self._session.cookie("csrftoken")
        if token is None:
            raise SenateEfdSessionError("no csrftoken cookie in the session")
        return token


def _csrf_token(page: bytes) -> str | None:
    match = _CSRF_INPUT.search(page)
    if match is None:
        return None
    return (match.group(1) or match.group(2)).decode("ascii", errors="replace")


def _is_agreement(response: FetchResponse) -> bool:
    return _path(response.url).startswith(_HOME) or _AGREEMENT_FIELD in response.body


def _session_refused(response: FetchResponse) -> bool:
    """Sent back to the agreement, or refused by the CSRF check."""
    return response.status == 403 or _path(response.url).startswith(_HOME)


def _expect(response: FetchResponse, content_type: str, *, what: str) -> None:
    if response.status != 200:
        raise SenateEfdError(f"eFD answered {response.status} for {what}", status=response.status)
    if response.content_type != content_type:
        raise SenateEfdError(
            f"eFD served {response.content_type or 'no content type'} for {what}, not "
            f"{content_type}: {response.body[:120]!r}",
            status=response.status,
        )


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _js_list(values: Sequence[int]) -> str:
    """``[11]`` or ``[7, 11]``: eFD wants a JSON list as a form value."""
    return "[" + ", ".join(str(int(value)) for value in values) + "]"


def _efd_datetime(day: date, *, end_of_day: bool = False) -> str:
    """``10/01/2026 00:00:00``, the format eFD's search form posts."""
    return day.strftime("%m/%d/%Y") + (" 23:59:59" if end_of_day else " 00:00:00")
