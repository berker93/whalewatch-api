"""Ops alerts: always the log line, the webhook once a day per kind, and never a raise."""

from __future__ import annotations

import json
from typing import Final

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import Settings
from app.ops import alerts
from app.ops.alerts import REPEAT_AFTER_SECONDS, alert_ops
from tests.conftest import make_settings
from tests.fake_redis import FakeRedis

WEBHOOK: Final = "https://hooks.example.com/services/T000/B000/secret"


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(alerts, "create_redis", lambda _: fake)
    return fake


def _settings(webhook: str | None = WEBHOOK) -> Settings:
    return make_settings(ops_alert_webhook_url=None if webhook is None else SecretStr(webhook))


@respx.mock
async def test_an_alert_is_posted_once_a_day_per_kind(redis: FakeRedis) -> None:
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
    settings = _settings()

    assert await alert_ops(settings, "senate-efd-layout", "eFD moved") is True
    assert await alert_ops(settings, "senate-efd-layout", "eFD moved again") is False
    assert await alert_ops(settings, "senate-efd-quiet", "eFD is quiet") is True

    assert hook.call_count == 2
    assert json.loads(hook.calls[0].request.content) == {"text": "[test] eFD moved"}
    assert redis.ttls["ops-alert:senate-efd-layout"] == REPEAT_AFTER_SECONDS


async def test_without_a_webhook_the_alert_is_the_log_line(redis: FakeRedis) -> None:
    assert await alert_ops(_settings(None), "senate-efd-layout", "eFD moved") is False
    assert redis.commands == []


@respx.mock
async def test_redis_down_posts_anyway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "create_redis", lambda _: FakeRedis(down=True))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))

    assert await alert_ops(_settings(), "senate-efd-layout", "eFD moved") is True
    assert hook.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [httpx.Response(500), httpx.ConnectError("refused")],
    ids=["500", "unreachable"],
)
async def test_a_webhook_that_fails_does_not_raise(
    redis: FakeRedis, answer: httpx.Response | Exception
) -> None:
    respx.post(WEBHOOK).mock(side_effect=[answer])

    assert await alert_ops(_settings(), "senate-efd-layout", "eFD moved") is False


def test_a_blank_webhook_in_a_copied_env_is_none() -> None:
    assert make_settings(ops_alert_webhook_url="  ").ops_alert_webhook_url is None
