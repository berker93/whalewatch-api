"""The US fetcher's handler: the code the Lambda runs, run here over a real socket.

What it promises is narrow and all of it is tested: the body comes back as
sent; any HTTP status is an answer rather than an error; the cookie jar goes
out with the request and comes back with every cookie the response set,
including on a redirect; and nothing outside the allow-list is fetched, not
even by a redirect. Plus the one property the deployment depends on: the
module imports only the standard library, because a zip of this one file is
the whole function.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from app.ingestion.remote import handler
from tests.fake_http import FakeHttpServer, Reply, Seen

HOSTS: Final = ("127.0.0.1",)


def _event(url: str, **fields: Any) -> dict[str, Any]:
    return {"v": 1, "url": url, **fields}


@pytest.fixture
def server() -> Iterator[FakeHttpServer]:
    def app(seen: Seen) -> Reply:
        match seen.path:
            case "/bytes":
                return Reply(200, b"\xef\xbb\xbf<a>\r\n\xe9</a>", [("Content-Type", "text/xml")])
            case "/set":
                return Reply(200, b"", [("Set-Cookie", "csrftoken=abc; Path=/")])
            case "/echo":
                return Reply(200, repr(sorted(seen.cookies.items())).encode())
            case "/login":
                # Django's login shape: the session is set on the 302 itself.
                return Reply(
                    302,
                    b"",
                    [("Location", "/home"), ("Set-Cookie", "sessionid=s1; HttpOnly; Path=/")],
                )
            case "/home":
                return Reply(200, f"{seen.method} {sorted(seen.cookies)}".encode())
            case "/form":
                return Reply(200, repr(sorted(seen.form.items())).encode())
            case "/away":
                return Reply(302, b"", [("Location", "http://example.com/")])
            case "/big":
                return Reply(200, b"x" * 2048)
            case _:
                return Reply(404, b"no such page")

    with FakeHttpServer(app) as running:
        yield running


def test_the_body_comes_back_byte_for_byte(server: FakeHttpServer) -> None:
    fetched = handler.perform(_event(server.url("/bytes")), HOSTS)

    assert fetched["status"] == 200
    assert fetched["body"] == b"\xef\xbb\xbf<a>\r\n\xe9</a>"
    assert ["Content-Type", "text/xml"] in fetched["headers"]


def test_an_error_status_is_an_answer_not_a_failure(server: FakeHttpServer) -> None:
    fetched = handler.perform(_event(server.url("/missing")), HOSTS)

    assert fetched["status"] == 404
    assert fetched["body"] == b"no such page"


def test_cookies_set_by_a_response_come_back_and_are_sent_next_time(
    server: FakeHttpServer,
) -> None:
    first = handler.perform(_event(server.url("/set")), HOSTS)
    assert [(c["name"], c["value"]) for c in first["cookies"]] == [("csrftoken", "abc")]

    second = handler.perform(_event(server.url("/echo"), cookies=first["cookies"]), HOSTS)

    assert second["body"] == b"[('csrftoken', 'abc')]"
    assert second["cookies"] == first["cookies"]


def test_a_cookie_set_on_a_redirect_is_kept_and_sent_on_the_next_hop(
    server: FakeHttpServer,
) -> None:
    fetched = handler.perform(
        _event(server.url("/login"), method="POST", body_b64=base64.b64encode(b"a=1").decode()),
        HOSTS,
    )

    assert fetched["status"] == 200
    assert fetched["url"] == server.url("/home")
    # A POST answered 302 is followed as a GET, as a browser does.
    assert fetched["body"] == b"GET ['sessionid']"
    (session,) = fetched["cookies"]
    assert session["name"] == "sessionid"
    assert session["http_only"]


def test_a_redirect_not_followed_is_returned_with_its_cookies(server: FakeHttpServer) -> None:
    fetched = handler.perform(_event(server.url("/login"), follow_redirects=False), HOSTS)

    assert fetched["status"] == 302
    assert ["Location", "/home"] in fetched["headers"]
    assert [c["name"] for c in fetched["cookies"]] == ["sessionid"]


def test_a_form_post_arrives_as_sent(server: FakeHttpServer) -> None:
    body = b"start=0&report_types=%5B11%5D"
    fetched = handler.perform(
        _event(
            server.url("/form"),
            method="POST",
            headers=[["Content-Type", "application/x-www-form-urlencoded"]],
            body_b64=base64.b64encode(body).decode(),
        ),
        HOSTS,
    )

    assert fetched["body"] == b"[('report_types', '[11]'), ('start', '0')]"


def test_a_host_off_the_allow_list_is_refused_before_any_request(
    server: FakeHttpServer,
) -> None:
    with pytest.raises(handler.FetchError) as refused:
        handler.perform(_event(server.url("/bytes")), ("efdsearch.senate.gov",))

    assert refused.value.kind == "host_not_allowed"
    assert server.seen == []


def test_a_redirect_off_the_allow_list_is_refused(server: FakeHttpServer) -> None:
    with pytest.raises(handler.FetchError) as refused:
        handler.perform(_event(server.url("/away")), HOSTS)

    assert refused.value.kind == "host_not_allowed"
    assert "example.com" in str(refused.value)


def test_a_host_that_does_not_answer_is_upstream_unreachable() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    # Nothing listens on the port once the socket is closed.
    with pytest.raises(handler.FetchError) as failed:
        handler.perform(_event(f"http://127.0.0.1:{port}/"), HOSTS)

    assert failed.value.kind == "upstream_unreachable"


def test_a_body_over_the_cap_is_too_large(
    server: FakeHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler, "MAX_BODY_BYTES", 1024)

    with pytest.raises(handler.FetchError) as refused:
        handler.perform(_event(server.url("/big")), HOSTS)

    assert refused.value.kind == "too_large"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"url": "http://127.0.0.1/"}, id="no-version"),
        pytest.param({"v": 2, "url": "http://127.0.0.1/"}, id="other-version"),
        pytest.param({"v": 1, "url": "file:///etc/passwd"}, id="not-http"),
        pytest.param({"v": 1, "url": "http://127.0.0.1/", "method": "DELETE"}, id="method"),
        pytest.param({"v": 1, "url": "http://127.0.0.1/", "body_b64": "YQ=="}, id="get-with-body"),
        pytest.param(
            {"v": 1, "url": "http://127.0.0.1/", "method": "POST", "body_b64": "!"},
            id="bad-base64",
        ),
    ],
)
def test_a_malformed_request_is_a_bad_request(event: dict[str, Any]) -> None:
    with pytest.raises(handler.FetchError) as refused:
        handler.perform(event, HOSTS)

    assert refused.value.kind == "bad_request"


# --- the Lambda entry point --------------------------------------------------


def test_the_lambda_spools_the_body_and_replies_with_where_it_is(
    server: FakeHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOOL_BUCKET", "spool-bucket")
    monkeypatch.setenv("ALLOWED_HOSTS", "efdsearch.senate.gov, 127.0.0.1")
    spooled: dict[tuple[str, str], bytes] = {}

    reply = handler.lambda_handler(
        _event(server.url("/bytes")),
        SimpleNamespace(aws_request_id="req-1"),
        put=lambda bucket, key, body: spooled.__setitem__((bucket, key), body),
    )

    body = b"\xef\xbb\xbf<a>\r\n\xe9</a>"
    assert spooled == {("spool-bucket", "spool/req-1"): body}
    assert reply["ok"] is True
    assert reply["status"] == 200
    assert reply["body"] == {
        "bucket": "spool-bucket",
        "key": "spool/req-1",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def test_the_lambda_replies_with_a_failure_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPOOL_BUCKET", "spool-bucket")
    monkeypatch.setenv("ALLOWED_HOSTS", "efdsearch.senate.gov")

    reply = handler.lambda_handler(
        _event("https://example.com/"), None, put=lambda *_: pytest.fail("nothing to spool")
    )

    assert reply == {
        "v": 1,
        "ok": False,
        "error": {"kind": "host_not_allowed", "message": "'example.com' is not in ALLOWED_HOSTS"},
    }


def test_the_lambda_without_a_spool_bucket_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misdeployed function, which the worker must hear as a broken fetcher."""
    monkeypatch.delenv("SPOOL_BUCKET", raising=False)

    with pytest.raises(KeyError):
        handler.lambda_handler(_event("https://efdsearch.senate.gov/"), None)


# --- deployability -----------------------------------------------------------


def test_the_handler_imports_only_the_standard_library() -> None:
    """A zip of handler.py alone is the function: an app import would fail
    there, at the first invocation, and only there. boto3 is the Lambda
    runtime's own, imported where only the deployed function goes."""
    tree = ast.parse(Path(handler.__file__).read_text())
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    outside = imported - set(sys.stdlib_module_names) - {"__future__"}
    assert outside == {"boto3"}
