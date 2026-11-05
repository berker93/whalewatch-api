"""The US-region fetcher: fetch one URL, return its bytes, honour its cookies.

This file is deployed on its own, as the whole of an AWS Lambda function in
us-east-1 (``infra/us-fetcher/``), and imported in-process by
:class:`~app.ingestion.remote.fetcher.LocalFetcher`. That is why it imports
nothing but the standard library — a zip of this one file is the deployment —
and why the worker side imports its wire format from here rather than
restating it: the Lambda and the worker cannot disagree about a shape that has
one definition.

Why it exists at all: Senate eFD and the House Clerk's disclosure site answer
requests from outside the US badly or not at all, and the workers do not run in
the US. The fetcher is the one piece of the stack that does.

Stateless, cookies included
---------------------------
A Lambda keeps nothing between invocations that anyone may rely on, so the
cookie jar travels with each request and comes back with each response. The
worker holds it (:class:`~app.ingestion.remote.fetcher.CookieSession`); this
side loads it into a real :class:`http.cookiejar.CookieJar` for the duration
of one fetch, so domain, path, ``Secure`` and expiry rules are the standard
library's rather than ours, and cookies set by a redirect's intermediate
response — which is where Django sets a session on a login POST — are caught.

The body goes to S3, not back in the reply
------------------------------------------
A synchronous invocation's reply is capped at 6 MB, and JSON would need the
body base64-encoded inside it. A House yearly archive is bigger than that. So
the body is written to a spool bucket and the reply carries its key, size and
SHA-256; the worker reads it, checks the digest, archives it to the raw store
and deletes the spool copy. A lifecycle rule on the bucket expires whatever a
crashed worker left behind.

Not an open proxy
-----------------
``ALLOWED_HOSTS`` lists the hosts this function will fetch from. Anyone who
can invoke it can make it request any URL it allows, from an AWS address,
with whatever headers they like; the list is what bounds what a leaked worker
credential is worth. Redirects are checked against it too, hop by hop.

The wire format, version 1
--------------------------
Request (the invocation's payload)::

    {"v": 1, "url": "https://...", "method": "GET",
     "headers": [["Referer", "..."]], "body_b64": null,
     "cookies": [CookieRecord, ...], "follow_redirects": true, "timeout": 30}

Reply, success — any HTTP status the target answered with, 4xx and 5xx
included, is a success here; what it means is the caller's business::

    {"v": 1, "ok": true, "status": 200, "url": "<final url>",
     "headers": [["Content-Type", "text/html"]], "cookies": [...],
     "body": {"bucket": "...", "key": "...", "size": 1234, "sha256": "..."}}

Reply, failure — the fetcher ran and the target could not be had::

    {"v": 1, "ok": false, "error": {"kind": "upstream_unreachable", "message": "..."}}

``kind`` is one of :data:`ERROR_KINDS`. An exception escaping this module is
not part of the format: Lambda reports it as a ``FunctionError``, and the worker
reads that as the fetcher itself being broken.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.cookiejar
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, TypedDict
from urllib.parse import urlsplit

#: Bumped when either shape above changes incompatibly. The worker refuses a
#: reply of another version rather than misreading it.
WIRE_VERSION: Final = 1

#: What ``error.kind`` can say.
#:
#: - ``bad_request``: the event was not a version-1 request. A worker bug.
#: - ``host_not_allowed``: the URL, or a redirect, left ``ALLOWED_HOSTS``.
#: - ``upstream_unreachable``: DNS, connect, TLS or read failed, or timed out.
#: - ``too_large``: the body passed :data:`MAX_BODY_BYTES`.
ERROR_KINDS: Final = frozenset(
    {"bad_request", "host_not_allowed", "upstream_unreachable", "too_large"}
)

#: Read into memory before it is written anywhere, so bounded by the function's
#: memory (512 MB in the template). A House yearly archive is a few MB.
MAX_BODY_BYTES: Final = 64 * 1024 * 1024

#: Per request, connect and each read. The function's own timeout (60s in the
#: template) bounds the whole invocation, redirects included.
DEFAULT_TIMEOUT_SECONDS: Final = 30.0
MAX_TIMEOUT_SECONDS: Final = 50.0

_METHODS: Final = frozenset({"GET", "POST", "HEAD"})

#: Where the spool key goes in the bucket. A prefix, so the lifecycle rule and
#: the IAM policies can name it.
SPOOL_PREFIX: Final = "spool/"


class CookieRecord(TypedDict):
    """One cookie as it crosses the wire: what a jar needs to apply it again."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool
    expires: int | None
    #: The domain came from a ``Domain=`` attribute, so subdomains get it too.
    domain_specified: bool
    path_specified: bool
    #: A session cookie: no ``Expires``/``Max-Age``.
    discard: bool
    http_only: bool


class FetchError(Exception):
    """A request this side refuses or cannot complete. Carries a wire ``kind``."""

    def __init__(self, kind: str, message: str) -> None:
        assert kind in ERROR_KINDS, kind
        super().__init__(message)
        self.kind = kind


class Fetched(TypedDict):
    """A completed fetch, before the body is put anywhere."""

    status: int
    url: str
    headers: list[list[str]]
    cookies: list[CookieRecord]
    body: bytes


# --- the fetch ---------------------------------------------------------------


def perform(event: Mapping[str, Any], allowed_hosts: Iterable[str]) -> Fetched:
    """Do the request ``event`` describes. Pure apart from the network.

    :raises FetchError: For every failure the wire format has a kind for.
    """
    allowed = frozenset(host.strip().lower() for host in allowed_hosts if host.strip())
    url, method, headers, body, cookies, follow, timeout = _parse_request(event)
    _check_host(url, allowed)

    jar = http.cookiejar.CookieJar()
    for record in cookies:
        jar.set_cookie(_to_cookie(record))

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        _Redirects(allowed, follow=follow),
    )
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers:
        request.add_header(name, value)

    try:
        with opener.open(request, timeout=timeout) as response:
            return _fetched(response, jar)
    except urllib.error.HTTPError as answered:
        # A 4xx, a 5xx, or a redirect not followed: an answer, not a failure.
        # HTTPError is the response object too.
        with answered:
            return _fetched(answered, jar)
    except FetchError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError) as failure:
        reason = getattr(failure, "reason", failure)
        raise FetchError("upstream_unreachable", f"{method} {url}: {reason}") from failure


def _fetched(response: Any, jar: http.cookiejar.CookieJar) -> Fetched:
    body = _read_capped(response)
    return {
        "status": int(response.status),
        "url": str(response.geturl()),
        "headers": [[name, value] for name, value in response.headers.items()],
        "cookies": [_to_record(cookie) for cookie in jar],
        "body": body,
    }


def _read_capped(response: Any) -> bytes:
    try:
        body: bytes = response.read(MAX_BODY_BYTES + 1)
    except (http.client.HTTPException, OSError) as failure:
        raise FetchError("upstream_unreachable", f"reading {response.geturl()}: {failure}") from (
            failure
        )
    if len(body) > MAX_BODY_BYTES:
        raise FetchError(
            "too_large", f"{response.geturl()} is over {MAX_BODY_BYTES // (1024 * 1024)} MB"
        )
    return body


def _parse_request(
    event: Mapping[str, Any],
) -> tuple[str, str, list[tuple[str, str]], bytes | None, list[CookieRecord], bool, float]:
    if not isinstance(event, Mapping) or event.get("v") != WIRE_VERSION:
        raise FetchError("bad_request", f"expected a version {WIRE_VERSION} request")
    url = event.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme not in ("https", "http"):
        raise FetchError("bad_request", f"not an http(s) URL: {url!r}")
    method = str(event.get("method", "GET")).upper()
    if method not in _METHODS:
        raise FetchError("bad_request", f"method {method!r} is not one of {sorted(_METHODS)}")
    try:
        headers = [(str(name), str(value)) for name, value in event.get("headers") or ()]
        encoded = event.get("body_b64")
        body = None if encoded is None else base64.b64decode(encoded, validate=True)
        cookies: list[CookieRecord] = list(event.get("cookies") or ())
        follow = bool(event.get("follow_redirects", True))
        timeout = min(float(event.get("timeout", DEFAULT_TIMEOUT_SECONDS)), MAX_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as failure:
        raise FetchError("bad_request", f"malformed request: {failure}") from failure
    if body is not None and method != "POST":
        raise FetchError("bad_request", f"a {method} carries no body")
    return url, method, headers, body, cookies, follow, timeout


def _check_host(url: str, allowed: frozenset[str]) -> None:
    host = (urlsplit(url).hostname or "").lower()
    if host not in allowed:
        raise FetchError("host_not_allowed", f"{host or url!r} is not in ALLOWED_HOSTS")


class _Redirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handling, with each hop held to the allow-list.

    With ``follow=False`` a 3xx is returned as itself, ``Location`` and all.
    Followed, a POST answered 301/302/303 becomes a GET with no body, as a
    browser does; urllib does that already.
    """

    def __init__(self, allowed: frozenset[str], *, follow: bool) -> None:
        self._allowed = allowed
        self._follow = follow

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not self._follow:
            return None
        _check_host(newurl, self._allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --- cookies on the wire -----------------------------------------------------


def _to_record(cookie: http.cookiejar.Cookie) -> CookieRecord:
    return {
        "name": cookie.name,
        "value": cookie.value or "",
        "domain": cookie.domain,
        "path": cookie.path,
        "secure": bool(cookie.secure),
        "expires": cookie.expires,
        "domain_specified": bool(cookie.domain_specified),
        "path_specified": bool(cookie.path_specified),
        "discard": bool(cookie.discard),
        "http_only": cookie.has_nonstandard_attr("HttpOnly"),
    }


def _to_cookie(record: CookieRecord) -> http.cookiejar.Cookie:
    domain = record["domain"]
    return http.cookiejar.Cookie(
        version=0,
        name=record["name"],
        value=record["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=record["domain_specified"],
        domain_initial_dot=domain.startswith("."),
        path=record["path"],
        path_specified=record["path_specified"],
        secure=record["secure"],
        expires=record["expires"],
        discard=record["discard"],
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""} if record["http_only"] else {},
    )


# --- the Lambda entry point --------------------------------------------------

#: ``put(bucket, key, body)``. boto3 in Lambda; a test hands in its own.
Put = Callable[[str, str, bytes], None]


def lambda_handler(
    event: Mapping[str, Any], context: Any, put: Put | None = None
) -> dict[str, Any]:
    """Fetch, spool the body to ``SPOOL_BUCKET``, reply with where it is.

    Configured by environment: ``SPOOL_BUCKET`` (required) and
    ``ALLOWED_HOSTS``, comma-separated. A missing ``SPOOL_BUCKET`` raises —
    that is the function being misdeployed, which the worker should hear about
    as a broken fetcher, not as a failed fetch.
    """
    bucket = os.environ["SPOOL_BUCKET"]
    allowed = os.environ.get("ALLOWED_HOSTS", "").split(",")
    started = time.monotonic()
    try:
        fetched = perform(event, allowed)
    except FetchError as failure:
        _log(event, error=failure.kind, elapsed=time.monotonic() - started)
        error = {"kind": failure.kind, "message": str(failure)}
        return {"v": WIRE_VERSION, "ok": False, "error": error}

    body = fetched["body"]
    request_id = (
        getattr(context, "aws_request_id", None)
        or hashlib.sha256(f"{time.time_ns()}{socket.gethostname()}".encode()).hexdigest()[:32]
    )
    key = f"{SPOOL_PREFIX}{request_id}"
    (put or _boto3_put)(bucket, key, body)
    _log(event, status=fetched["status"], size=len(body), elapsed=time.monotonic() - started)
    return {
        "v": WIRE_VERSION,
        "ok": True,
        "status": fetched["status"],
        "url": fetched["url"],
        "headers": fetched["headers"],
        "cookies": fetched["cookies"],
        "body": {
            "bucket": bucket,
            "key": key,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
    }


def _boto3_put(bucket: str, key: str, body: bytes) -> None:
    # Imported here: boto3 is in the Lambda runtime and not in this project,
    # and nothing but the deployed function takes this path.
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body)


def _log(event: Mapping[str, Any], **fields: object) -> None:
    """One JSON line per invocation for CloudWatch. The host and method only:
    query strings, headers and cookies can carry a session."""
    url = event.get("url") if isinstance(event, Mapping) else None
    host = urlsplit(url).hostname if isinstance(url, str) else None
    method = event.get("method", "GET") if isinstance(event, Mapping) else None
    print(json.dumps({"fetch": {"host": host, "method": method, **fields}}, default=str))
