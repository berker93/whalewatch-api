"""A real HTTP server on 127.0.0.1 that answers with whatever a test says.

For the US fetcher's tests, which have to go over a socket: the handler is
urllib, which respx cannot intercept, and the behaviour under test — cookies
set on a redirect, a 4xx returned rather than raised — is urllib's and the
standard cookie jar's, not ours to mock.

One callable answers every request, so a test can be a small state machine
(the fake eFD in tests/test_senate_efd.py is one)::

    def app(seen: Seen) -> Reply:
        return Reply(200, b"hello", [("Set-Cookie", "a=1; Path=/")])

    with FakeHttpServer(app) as server:
        server.url("/hello")
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class Seen:
    """One request as the server received it."""

    method: str
    path: str
    query: dict[str, str]
    headers: Message
    body: bytes
    cookies: dict[str, str]

    @property
    def form(self) -> dict[str, str]:
        return dict(parse_qsl(self.body.decode()))


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)


App = Callable[[Seen], Reply]


class FakeHttpServer:
    def __init__(self, app: App) -> None:
        self.app = app
        self.seen: list[Seen] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                jar = SimpleCookie(self.headers.get("Cookie") or "")
                seen = Seen(
                    method=self.command,
                    path=split.path,
                    query=dict(parse_qsl(split.query)),
                    headers=self.headers,
                    body=self.rfile.read(length) if length else b"",
                    cookies={name: morsel.value for name, morsel in jar.items()},
                )
                server.seen.append(seen)
                reply = server.app(seen)
                self.send_response(reply.status)
                for name, value in reply.headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(reply.body)

            do_GET = do_POST = do_HEAD = _answer  # noqa: N815

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll, so shutdown() at the end of each test takes milliseconds
        # rather than the default half-second.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str) -> str:
        return self.base + path

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
