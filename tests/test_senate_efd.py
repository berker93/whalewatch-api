"""A Senate eFD session through the US fetcher, against a fake eFD.

The fake is a small Django in spirit: a ``csrftoken`` cookie from the
agreement page, the token checked against the form field on every POST, a
``Referer`` required, the session cookie set on the 302 that accepts the terms,
and everything else redirecting to the agreement without one. It can expire
every session on demand, and can refuse the agreement outright, which is how
eFD looks from outside the US.

It runs over a real socket through :class:`LocalFetcher`, so what is tested is
the handler's cookie jar and redirects doing what eFD needs, not a mock of them.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pytest

from app.ingestion.congress.senate_efd import (
    SenateEfd,
    SenateEfdError,
    SenateEfdSessionError,
)
from app.ingestion.remote.fetcher import FetcherUnavailable, FetchRequest, LocalFetcher
from app.storage.raw import LocalRawStore, RawStore
from tests.fake_http import FakeHttpServer, Reply, Seen

REPORT_ID: Final = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
NOW: Final = datetime(2026, 11, 5, 14, 3, 12, tzinfo=UTC)
USER_AGENT: Final = "WhaleWatch ops@whalewatch.io"

_AGREEMENT: Final = """<form id="agreement_form" action="/search/home/" method="POST">
<input type="hidden" name="csrfmiddlewaretoken" value="{token}">
<input class="form-check-input" type="checkbox" name="prohibition_agreement" value="1">
</form>"""


@dataclass
class FakeEfd:
    sessions: set[str] = field(default_factory=set)
    refuse_agreement: bool = False
    refuse_everything: bool = False
    acceptances: int = 0

    def __call__(self, seen: Seen) -> Reply:
        if self.refuse_everything:
            return Reply(403, b"Access Denied", [("Content-Type", "text/html")])
        accepted = seen.cookies.get("sessionid") in self.sessions
        csrf = seen.cookies.get("csrftoken")

        if seen.path == "/search/home/" and seen.method == "GET":
            token = csrf or secrets.token_hex(8)
            return Reply(
                200,
                _AGREEMENT.format(token=token).encode(),
                [("Content-Type", "text/html"), ("Set-Cookie", f"csrftoken={token}; Path=/")],
            )
        if seen.path == "/search/home/" and seen.method == "POST":
            if self.refuse_agreement or not self._csrf_ok(seen, csrf, header=False):
                return Reply(403, b"CSRF verification failed", [("Content-Type", "text/html")])
            if seen.form.get("prohibition_agreement") != "1":
                return _to_home()
            session = secrets.token_hex(8)
            self.sessions.add(session)
            self.acceptances += 1
            return Reply(
                302,
                b"",
                [
                    ("Location", "/search/"),
                    ("Set-Cookie", f"sessionid={session}; HttpOnly; Path=/"),
                ],
            )
        if not accepted:
            return _to_home()
        if seen.path == "/search/":
            return Reply(200, b"<h1>Search</h1>", [("Content-Type", "text/html")])
        if seen.path == "/search/report/data/" and seen.method == "POST":
            if not self._csrf_ok(seen, csrf, header=True):
                return Reply(403, b"CSRF verification failed", [("Content-Type", "text/html")])
            body = {
                "draw": 1,
                "recordsTotal": 1,
                "recordsFiltered": 1,
                "data": [["Jane", "Doe", "Doe, Jane (Senator)", "<a href=...>", "11/01/2026"]],
                "result": "ok",
                "echo": seen.form,
            }
            return Reply(200, json.dumps(body).encode(), [("Content-Type", "application/json")])
        if seen.path == f"/search/view/ptr/{REPORT_ID}/":
            return Reply(
                200, b"<table>PTR \xe9</table>", [("Content-Type", "text/html; charset=utf-8")]
            )
        return Reply(404, b"Not Found", [("Content-Type", "text/html")])

    def expire_sessions(self) -> None:
        self.sessions.clear()

    @staticmethod
    def _csrf_ok(seen: Seen, cookie: str | None, *, header: bool) -> bool:
        if cookie is None or seen.form.get("csrfmiddlewaretoken") != cookie:
            return False
        if header and seen.headers.get("X-CSRFToken") != cookie:
            return False
        return bool(seen.headers.get("Referer"))


def _to_home() -> Reply:
    return Reply(302, b"", [("Location", "/search/home/")])


@pytest.fixture
def efd_site() -> Iterator[tuple[FakeEfd, FakeHttpServer]]:
    fake = FakeEfd()
    with FakeHttpServer(fake) as server:
        yield fake, server


@pytest.fixture
def store(tmp_path: Path) -> LocalRawStore:
    return LocalRawStore(tmp_path)


def _efd(server: FakeHttpServer, store: RawStore) -> SenateEfd:
    return SenateEfd(
        LocalFetcher(), store, user_agent=USER_AGENT, base_url=server.base, clock=lambda: NOW
    )


# --- the terms -------------------------------------------------------------------


async def test_accepting_the_terms_starts_a_session(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site

    await _efd(server, store).accept_terms()

    assert fake.acceptances == 1
    post = next(s for s in server.seen if s.method == "POST")
    assert post.form["prohibition_agreement"] == "1"
    assert post.headers["Referer"] == server.url("/search/home/")
    assert all(s.headers["User-Agent"] == USER_AGENT for s in server.seen)


async def test_the_terms_are_accepted_on_first_use(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site

    await _efd(server, store).report(f"/search/view/ptr/{REPORT_ID}/")

    assert fake.acceptances == 1


async def test_a_refused_agreement_is_a_session_error(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site
    fake.refuse_agreement = True

    with pytest.raises(SenateEfdSessionError, match="did not accept the terms: 403"):
        await _efd(server, store).accept_terms()


async def test_a_403_for_the_agreement_page_suggests_the_address(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site
    fake.refuse_everything = True

    with pytest.raises(SenateEfdSessionError, match="outside the US"):
        await _efd(server, store).accept_terms()


# --- search and reports, archived ------------------------------------------------------


async def test_a_search_page_is_archived_as_served_before_it_is_returned(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    _, server = efd_site

    page = await _efd(server, store).search_reports(
        submitted_from=date(2026, 10, 1), submitted_to=date(2026, 10, 31), start=100
    )

    assert page.key == "raw/senate-efd/search/20261105T140312Z-000100.json"
    assert await store.get(page.key) == page.body
    echo = json.loads(page.body)["echo"]
    assert echo["report_types"] == "[11]"
    assert echo["start"] == "100"
    assert echo["submitted_start_date"] == "10/01/2026 00:00:00"
    assert echo["submitted_end_date"] == "10/31/2026 23:59:59"


async def test_a_report_page_is_archived_under_its_id(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    _, server = efd_site

    report = await _efd(server, store).report(server.url(f"/search/view/ptr/{REPORT_ID}/"))

    assert report.key == f"raw/senate-efd/report/ptr/{REPORT_ID}.html"
    assert await store.get(report.key) == b"<table>PTR \xe9</table>"


async def test_a_missing_report_raises_and_archives_nothing(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    _, server = efd_site
    missing = "ffffffff-0000-0000-0000-000000000000"

    with pytest.raises(SenateEfdError) as failed:
        await _efd(server, store).report(f"/search/view/ptr/{missing}/")

    assert failed.value.status == 404
    assert await store.list("raw/senate-efd/") == ()


async def test_a_link_that_is_not_a_report_is_refused(store: RawStore) -> None:
    efd = SenateEfd(LocalFetcher(), store, user_agent=USER_AGENT)

    with pytest.raises(ValueError, match="not an eFD report link"):
        await efd.report("/search/view/ptr/../../etc/")


# --- expiry ------------------------------------------------------------------------


async def test_an_expired_session_is_accepted_again_and_the_request_repeated(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site
    efd = _efd(server, store)
    await efd.search_reports(submitted_from=date(2026, 10, 1))

    fake.expire_sessions()
    page = await efd.search_reports(submitted_from=date(2026, 10, 1), start=100)

    assert fake.acceptances == 2
    assert json.loads(page.body)["result"] == "ok"


async def test_a_session_refused_right_after_acceptance_is_an_error_not_a_loop(
    efd_site: tuple[FakeEfd, FakeHttpServer], store: RawStore
) -> None:
    fake, server = efd_site
    # Every session survives the terms and dies at the first report request.
    original = fake.__call__

    def forgetful(seen: Seen) -> Reply:
        if seen.path.startswith("/search/view/"):
            fake.expire_sessions()
        return original(seen)

    server.app = forgetful

    with pytest.raises(SenateEfdSessionError, match="refused a session it had just accepted"):
        await _efd(server, store).report(f"/search/view/ptr/{REPORT_ID}/")

    assert fake.acceptances == 2


# --- no fallback -----------------------------------------------------------------------


class _Down:
    backend = "lambda"

    async def fetch(self, request: FetchRequest, cookies: object = ()) -> object:
        raise FetcherUnavailable("the US fetcher is unreachable: invoking failed")


async def test_an_unreachable_fetcher_propagates_and_the_archive_is_not_read(
    store: RawStore,
) -> None:
    key = f"raw/senate-efd/report/ptr/{REPORT_ID}.html"
    await store.put(key, b"<table>yesterday's copy</table>", "text/html")
    efd = SenateEfd(_Down(), store, user_agent=USER_AGENT)  # type: ignore[arg-type]

    with pytest.raises(FetcherUnavailable):
        await efd.report(f"/search/view/ptr/{REPORT_ID}/")
