"""Telling ops that a job hit something no retry will mend.

Most failures need no one: a run that fails transiently is retried, and one
that fails for good leaves a ``failed`` ``ingestion_run`` row that the next run
usually makes moot. Some do need someone, and will not get better on their own
— Senate eFD changing the layout of its pages is the one so far — and for those
a row nobody reads is not enough: :func:`alert_ops`.

An alert is, always, an ERROR line ``ops.alert`` with its ``kind`` and text,
for whatever reads the logs. With ``OPS_ALERT_WEBHOOK_URL`` set it is also a
POST of ``{"text": ...}`` to that webhook (Slack's incoming webhooks take it as
it is, as do Mattermost's and Discord's ``/slack`` endpoint).

Once a day per kind
-------------------
A broken layout breaks every run until it is fixed, and the Senate job runs
every two hours: twelve identical messages a day would be noise that teaches
people to mute the channel. So a ``kind`` is posted at most once per
:data:`REPEAT_AFTER_SECONDS`, held by a Redis key that expires. The log line is
written every time, posted or not. Redis being down is no reason to stay quiet:
then the alert is posted, and may repeat.

Best effort, like sending email: an alert that cannot be posted is logged, and
the job's own failure is what propagates, never the alert's.
"""

from __future__ import annotations

from typing import Final

import httpx
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import create_redis

logger = get_logger(__name__)

#: How long a kind stays quiet after it was posted.
REPEAT_AFTER_SECONDS: Final = 24 * 60 * 60

_KEY: Final = "ops-alert:{kind}"
_TIMEOUT_SECONDS: Final = 10.0


async def alert_ops(settings: Settings, kind: str, text: str) -> bool:
    """Tell ops ``text``, under ``kind`` (``senate-efd-layout``). Never raises.

    :returns: Whether it was posted to the webhook: ``False`` with none set,
        with ``kind`` posted in the last day, or when the post failed.
    """
    logger.error("ops.alert", kind=kind, text=text)
    webhook = settings.ops_alert_webhook_url
    if webhook is None:
        return False
    if not await _first_today(settings, kind):
        logger.info("ops.alert_repeated", kind=kind)
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                webhook.get_secret_value(), json={"text": f"[{settings.environment}] {text}"}
            )
        response.raise_for_status()
    except httpx.HTTPError as failure:
        # The URL holds the webhook's secret: the error says what, not where.
        logger.warning("ops.alert_unsent", kind=kind, error=type(failure).__name__)
        return False
    logger.info("ops.alert_sent", kind=kind)
    return True


async def _first_today(settings: Settings, kind: str) -> bool:
    """Whether ``kind`` was not posted in the last day, and claim it if so."""
    redis = create_redis(settings)
    try:
        claimed = await redis.set(_KEY.format(kind=kind), "1", nx=True, ex=REPEAT_AFTER_SECONDS)
    except (RedisError, OSError) as failure:
        logger.warning("ops.alert_unthrottled", kind=kind, error=str(failure))
        return True
    finally:
        await redis.aclose()
    return bool(claimed)
