"""The worker's side of the US fetcher, against the real handler.

:class:`LambdaFetcher` is tested with a stand-in for the Lambda service that
runs :func:`~app.ingestion.remote.handler.lambda_handler` in-process — the
code that is deployed — with its spool on a moto S3 server. So the wire format
is exercised from both ends at once, which is the part that can drift; only
AWS's invocation plumbing is not real.

The failure tests are the acceptance criterion "clear failure when the fetcher
is unreachable, never a silent fallback": every way the fetcher itself can be
unusable is :class:`FetcherUnavailable`, which nothing catches and retries
somewhere else.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any, Final

import botocore.session
import pytest
from aiobotocore.session import get_session
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError
from typer.testing import CliRunner

from app import cli
from app.ingestion.remote import handler
from app.ingestion.remote.fetcher import (
    CookieSession,
    FetcherUnavailable,
    FetchRefused,
    FetchRequest,
    LambdaFetcher,
    LocalFetcher,
    UpstreamUnreachable,
    open_fetcher,
)
from tests.conftest import make_settings
from tests.fake_http import FakeHttpServer, Reply, Seen

_REGION: Final = "us-east-1"
FUNCTION: Final = "whalewatch-us-fetcher"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    server_module = pytest.importorskip("moto.server")
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _admin(endpoint: str) -> Any:
    return botocore.session.get_session().create_client(
        "s3",
        endpoint_url=endpoint,
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def bucket(moto_endpoint: str) -> str:
    name = f"spool-{uuid.uuid4().hex[:12]}"
    _admin(moto_endpoint).create_bucket(Bucket=name)
    return name


@pytest.fixture
async def s3_client(moto_endpoint: str) -> AsyncIterator[Any]:
    async with get_session().create_client(
        "s3",
        endpoint_url=moto_endpoint,
        region_name=_REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as client:
        yield client


@pytest.fixture
def site() -> Iterator[FakeHttpServer]:
    def app(seen: Seen) -> Reply:
        if seen.path == "/set":
            return Reply(200, b"set", [("Set-Cookie", "csrftoken=t0k; Path=/")])
        if seen.path == "/echo":
            return Reply(200, repr(sorted(seen.cookies.items())).encode())
        if seen.path == "/unwell":
            return Reply(503, b"maintenance")
        return Reply(200, b"\x00binary\xff", [("Content-Type", "application/zip")])

    with FakeHttpServer(app) as running:
        yield running


class _Payload:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self) -> bytes:
        return self._raw


class FakeLambda:
    """The Lambda service, as far as :class:`LambdaFetcher` can tell.

    ``invoke`` runs the real handler with the spool on moto. ``reply`` and
    ``failure`` replace that with a canned answer or exception.
    """

    def __init__(self, moto_endpoint: str, bucket: str) -> None:
        self._s3 = _admin(moto_endpoint)
        self._bucket = bucket
        self.invocations: list[dict[str, Any]] = []
        self.reply: dict[str, Any] | None = None
        self.function_error: dict[str, Any] | None = None
        self.failure: Exception | None = None
        self.after_put: Callable[[str, str], None] | None = None

    async def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> Any:  # noqa: N803
        assert FunctionName == FUNCTION
        assert InvocationType == "RequestResponse"
        event = json.loads(Payload)
        self.invocations.append(event)
        if self.failure is not None:
            raise self.failure
        if self.function_error is not None:
            return {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": _Payload(json.dumps(self.function_error).encode()),
            }
        if self.reply is not None:
            return {"StatusCode": 200, "Payload": _Payload(json.dumps(self.reply).encode())}
        reply = await asyncio.to_thread(self._run, event)
        return {"StatusCode": 200, "Payload": _Payload(json.dumps(reply).encode())}

    def _run(self, event: dict[str, Any]) -> dict[str, Any]:
        def put(bucket: str, key: str, body: bytes) -> None:
            self._s3.put_object(Bucket=bucket, Key=key, Body=body)
            if self.after_put is not None:
                self.after_put(bucket, key)

        return handler.lambda_handler(
            event, SimpleNamespace(aws_request_id=uuid.uuid4().hex), put=put
        )

    def spooled(self) -> list[str]:
        listed = self._s3.list_objects_v2(Bucket=self._bucket)
        return [item["Key"] for item in listed.get("Contents", [])]


@pytest.fixture
def aws_lambda(moto_endpoint: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> FakeLambda:
    monkeypatch.setenv("SPOOL_BUCKET", bucket)
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1")
    return FakeLambda(moto_endpoint, bucket)


@pytest.fixture
def fetcher(aws_lambda: FakeLambda, s3_client: Any) -> LambdaFetcher:
    return LambdaFetcher(aws_lambda, s3_client, FUNCTION)  # type: ignore[arg-type]


# --- through the Lambda ------------------------------------------------------


async def test_a_fetch_through_the_lambda_returns_the_body_and_clears_the_spool(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, site: FakeHttpServer
) -> None:
    response = await fetcher.fetch(FetchRequest(url=site.url("/archive.zip")))

    assert response.status == 200
    assert response.body == b"\x00binary\xff"
    assert response.content_type == "application/zip"
    assert aws_lambda.spooled() == []


async def test_an_error_status_through_the_lambda_is_a_response(
    fetcher: LambdaFetcher, site: FakeHttpServer
) -> None:
    response = await fetcher.fetch(FetchRequest(url=site.url("/unwell")))

    assert (response.status, response.body) == (503, b"maintenance")


async def test_a_cookie_session_carries_the_jar_across_invocations(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, site: FakeHttpServer
) -> None:
    session = CookieSession(fetcher)

    await session.fetch(FetchRequest(url=site.url("/set")))
    echoed = await session.fetch(FetchRequest(url=site.url("/echo")))

    assert session.cookie("csrftoken") == "t0k"
    assert echoed.body == b"[('csrftoken', 't0k')]"
    # Two invocations, the second carrying what the first set: nothing is
    # kept in the function between them.
    assert [len(event["cookies"]) for event in aws_lambda.invocations] == [0, 1]


async def test_a_form_is_sent_urlencoded(fetcher: LambdaFetcher, aws_lambda: FakeLambda) -> None:
    aws_lambda.reply = {"v": 1, "ok": False, "error": {"kind": "bad_request", "message": "-"}}

    with pytest.raises(FetchRefused):
        await fetcher.fetch(
            FetchRequest(url="https://x/", method="POST", form=[("a", "1"), ("b", "[11]")])
        )

    (event,) = aws_lambda.invocations
    assert event["body_b64"] == "YT0xJmI9JTVCMTElNUQ="  # a=1&b=%5B11%5D
    assert ["Content-Type", "application/x-www-form-urlencoded"] in event["headers"]


async def test_a_target_that_does_not_answer_is_upstream_unreachable(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda
) -> None:
    aws_lambda.reply = {
        "v": 1,
        "ok": False,
        "error": {"kind": "upstream_unreachable", "message": "GET https://x/: timed out"},
    }

    with pytest.raises(UpstreamUnreachable, match="timed out"):
        await fetcher.fetch(FetchRequest(url="https://x/"))


async def test_a_host_off_the_allow_list_is_refused(
    fetcher: LambdaFetcher, monkeypatch: pytest.MonkeyPatch, site: FakeHttpServer
) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", "efdsearch.senate.gov")

    with pytest.raises(FetchRefused) as refused:
        await fetcher.fetch(FetchRequest(url=site.url("/")))

    assert refused.value.kind == "host_not_allowed"


# --- the fetcher itself unusable: FetcherUnavailable, every time ---------------


async def test_a_function_that_raised_is_unavailable_with_its_message(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda
) -> None:
    """What the template's placeholder code does until deploy.sh uploads the handler."""
    aws_lambda.function_error = {
        "errorMessage": "placeholder code: run infra/us-fetcher/deploy.sh to upload handler.py",
        "errorType": "RuntimeError",
    }

    with pytest.raises(FetcherUnavailable, match=r"Unhandled.*placeholder code"):
        await fetcher.fetch(FetchRequest(url="https://x/"))


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no function"}},
                "Invoke",
            ),
            id="not-deployed",
        ),
        pytest.param(
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Invoke"),
            id="no-permission",
        ),
        pytest.param(
            EndpointConnectionError(endpoint_url="https://lambda.us-east-1.amazonaws.com"),
            id="unreachable",
        ),
    ],
)
async def test_an_invocation_that_fails_is_unavailable(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, failure: Exception
) -> None:
    aws_lambda.failure = failure

    with pytest.raises(FetcherUnavailable, match="the US fetcher is unreachable"):
        await fetcher.fetch(FetchRequest(url="https://x/"))


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"v": 2, "ok": True}, id="newer-version"),
        pytest.param(None, id="null"),
        pytest.param(["not", "a", "reply"], id="list"),
    ],
)
async def test_a_reply_in_another_shape_is_unavailable(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, reply: Any
) -> None:
    aws_lambda.reply = reply if reply is not None else {}

    with pytest.raises(FetcherUnavailable, match="is the deployed code current"):
        await fetcher.fetch(FetchRequest(url="https://x/"))


async def test_a_spooled_body_that_does_not_match_its_digest_is_unavailable(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, moto_endpoint: str, site: FakeHttpServer
) -> None:
    aws_lambda.after_put = lambda bucket, key: _admin(moto_endpoint).put_object(
        Bucket=bucket, Key=key, Body=b"tampered"
    )

    with pytest.raises(FetcherUnavailable, match="does not match"):
        await fetcher.fetch(FetchRequest(url=site.url("/")))


async def test_a_spooled_body_that_is_gone_is_unavailable(
    fetcher: LambdaFetcher, aws_lambda: FakeLambda, moto_endpoint: str, site: FakeHttpServer
) -> None:
    aws_lambda.after_put = lambda bucket, key: _admin(moto_endpoint).delete_object(
        Bucket=bucket, Key=key
    )

    with pytest.raises(FetcherUnavailable, match="NoSuchKey"):
        await fetcher.fetch(FetchRequest(url=site.url("/")))


# --- the local backend ---------------------------------------------------------


async def test_the_local_backend_runs_the_same_handler(site: FakeHttpServer) -> None:
    session = CookieSession(LocalFetcher())

    await session.fetch(FetchRequest(url=site.url("/set")))
    echoed = await session.fetch(FetchRequest(url=site.url("/echo")))

    assert echoed.body == b"[('csrftoken', 't0k')]"


async def test_the_local_backend_keeps_the_lambdas_allow_list() -> None:
    with pytest.raises(FetchRefused) as refused:
        await LocalFetcher().fetch(FetchRequest(url="https://example.com/"))

    assert refused.value.kind == "host_not_allowed"


def test_a_request_carries_a_form_or_a_body_not_both() -> None:
    with pytest.raises(ValueError, match="not both"):
        FetchRequest(url="https://x/", method="POST", form=[("a", "1")], body=b"a=1")


# --- selection and settings ------------------------------------------------------


async def test_no_fetcher_configured_fails_on_entry_and_says_what_to_set() -> None:
    with pytest.raises(FetcherUnavailable, match="US_FETCHER_BACKEND=lambda"):
        async with open_fetcher(make_settings()):
            pytest.fail("there is no fetcher to open")


async def test_the_local_backend_opens_as_local() -> None:
    async with open_fetcher(make_settings(us_fetcher_backend="local")) as fetcher:
        assert fetcher.backend == "local"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_local_backend_is_refused_where_deployed(environment: str) -> None:
    with pytest.raises(ValidationError, match="fetches from a non-US address"):
        make_settings(environment=environment, us_fetcher_backend="local")


def test_the_lambda_backend_needs_a_function() -> None:
    with pytest.raises(ValidationError, match="needs US_FETCHER_FUNCTION"):
        make_settings(us_fetcher_backend="lambda")


def test_half_a_key_pair_is_refused() -> None:
    with pytest.raises(ValidationError, match="together or not at all"):
        make_settings(
            us_fetcher_backend="lambda",
            us_fetcher_function=FUNCTION,
            us_fetcher_access_key_id="AKIA...",
        )


# --- fetcher-check ---------------------------------------------------------------


def test_fetcher_check_without_a_fetcher_exits_1_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_settings", make_settings)

    result = CliRunner().invoke(cli.app, ["fetcher-check"])

    assert result.exit_code == 1
    assert "no US fetcher is configured" in result.output


def test_fetcher_check_reports_the_egress_address(
    monkeypatch: pytest.MonkeyPatch, site: FakeHttpServer
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: make_settings(us_fetcher_backend="local"))
    monkeypatch.setattr(cli, "CHECKIP_URL", site.url("/ip"))
    site.app = lambda seen: Reply(200, b"203.0.113.7\n")

    result = CliRunner().invoke(cli.app, ["fetcher-check", "--no-efd"])

    assert result.exit_code == 0, result.output
    assert "203.0.113.7 via local" in result.output
