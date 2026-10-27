"""The alerts schema, asserted against a real Postgres (0042).

The bias of test_core_schema.py again: towards the second write. A recompute
re-emits every event it ever emitted, and ``(user_id, dedupe_key)`` is what
makes that harmless, so the first tests below emit the same event twice.
"""

from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PAYLOAD_KEYS, AlertRule, Filer, Notification, Security
from tests.integration.accounts import make_user

DEDUPE_KEY = "filer_filed:berkshire-hathaway:2026Q3"


def _payload(**values: Any) -> dict[str, Any]:
    return {**dict.fromkeys(PAYLOAD_KEYS), **values}


def _filed(user_id: int, **overrides: Any) -> dict[str, Any]:
    """Berkshire's 13F for 2026Q3 arrived: no ticker, no delta."""
    return {
        "user_id": user_id,
        "kind": "filer_filed",
        "title": "Berkshire Hathaway filed its 13F for 2026Q3",
        "body": "41 positions, $266.4B.",
        "payload": _payload(
            filer_slug="berkshire-hathaway", period="2026Q3", as_of="2026-11-14", n_positions=41
        ),
        "dedupe_key": DEDUPE_KEY,
        **overrides,
    }


async def _emit(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """The writer's insert: ``ON CONFLICT DO NOTHING`` on the dedupe key."""
    await session.execute(
        pg_insert(Notification)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_notification_user_id_dedupe_key")
    )


async def _filer(session: AsyncSession, slug: str = "berkshire-hathaway") -> int:
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    return filer_id


async def _security(session: AsyncSession) -> int:
    security_id = await session.scalar(
        insert(Security)
        .values(cusip="037833100", ticker="AAPL", name="APPLE INC", resolution_source="manual")
        .returning(Security.id)
    )
    assert security_id is not None
    return security_id


async def _refused(session: AsyncSession, row: Any, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            session.add(row)
            await session.flush()


# --- notification: the second emission -----------------------------------------


async def test_an_event_emitted_twice_is_one_notification(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)

    for _ in range(2):
        await _emit(db_session, [_filed(user_id)])

    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_a_read_notification_stays_read_when_its_event_is_emitted_again(
    db_session: AsyncSession,
) -> None:
    """The recompute writes nothing, so the user is not told twice."""
    user_id = await make_user(db_session)
    await _emit(db_session, [_filed(user_id)])
    await db_session.execute(update(Notification).values(read_at=func.now()))

    await _emit(db_session, [_filed(user_id, title="Re-rendered")])

    [row] = (await db_session.execute(select(Notification.title, Notification.read_at))).all()
    assert row.read_at is not None
    assert row.title == "Berkshire Hathaway filed its 13F for 2026Q3"


async def test_one_event_is_one_notification_for_each_user(db_session: AsyncSession) -> None:
    ada = await make_user(db_session, "ada@example.com")
    bob = await make_user(db_session, "bob@example.com")

    await _emit(db_session, [_filed(ada), _filed(bob)])

    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 2


# --- notification: what a row must hold -------------------------------------------


async def test_a_payload_missing_a_key_is_refused(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    payload = _payload()
    del payload["confidence"]

    await _refused(
        db_session,
        Notification(**_filed(user_id, payload=payload)),
        "ck_notification_payload_renders",
    )


async def test_a_payload_that_is_not_an_object_is_refused(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)

    await _refused(
        db_session,
        Notification(**_filed(user_id, payload=list(PAYLOAD_KEYS))),
        "ck_notification_payload_renders",
    )


@pytest.mark.parametrize(
    "dedupe_key",
    [
        "berkshire-hathaway:2026Q3",  # no kind
        "filer_position_change:berkshire-hathaway:2026Q3",  # another kind's
        "filer_filed:",  # the kind and nothing else
    ],
)
async def test_a_dedupe_key_must_start_with_its_kind(
    db_session: AsyncSession, dedupe_key: str
) -> None:
    user_id = await make_user(db_session)

    await _refused(
        db_session,
        Notification(**_filed(user_id, dedupe_key=dedupe_key)),
        "ck_notification_dedupe_key_names_its_kind",
    )


async def test_an_unknown_kind_is_refused(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)

    await _refused(
        db_session,
        Notification(**_filed(user_id, kind="price_move", dedupe_key="price_move:aapl")),
        "ck_notification_kind_is_known",
    )


async def test_the_unread_count_reads_the_partial_index(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    await _emit(db_session, [_filed(user_id)])
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = "\n".join(
        (
            await db_session.execute(
                text("""
                    EXPLAIN SELECT count(*) FROM notification
                    WHERE user_id = :u AND read_at IS NULL
                """),
                {"u": user_id},
            )
        )
        .scalars()
        .all()
    )

    assert "ix_notification_unread" in plan


# --- alert_rule: targets ----------------------------------------------------------


async def test_each_kind_names_its_own_target(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    filer_id = await _filer(db_session)
    security_id = await _security(db_session)

    db_session.add_all(
        [
            AlertRule(user_id=user_id, kind="filer_filed", filer_id=filer_id),
            AlertRule(user_id=user_id, kind="filer_position_change", filer_id=filer_id),
            AlertRule(user_id=user_id, kind="stock_ownership_change", security_id=security_id),
            AlertRule(user_id=user_id, kind="insider_cluster", security_id=security_id),
            AlertRule(user_id=user_id, kind="season_digest"),
        ]
    )
    await db_session.flush()

    [rule] = (
        await db_session.scalars(select(AlertRule).where(AlertRule.kind == "season_digest"))
    ).all()
    assert (rule.channels, rule.is_active, rule.min_value) == (["in_app"], True, None)


@pytest.mark.parametrize(
    ("kind", "target", "constraint"),
    [
        ("filer_filed", None, "ck_alert_rule_filer_kinds_name_a_filer"),
        ("filer_position_change", "security", "ck_alert_rule_filer_kinds_name_a_filer"),
        ("stock_ownership_change", "filer", "ck_alert_rule_filer_kinds_name_a_filer"),
        ("insider_cluster", None, "ck_alert_rule_security_kinds_name_a_security"),
        ("season_digest", "security", "ck_alert_rule_security_kinds_name_a_security"),
    ],
)
async def test_a_rule_with_the_wrong_target_for_its_kind_is_refused(
    db_session: AsyncSession, kind: str, target: str | None, constraint: str
) -> None:
    user_id = await make_user(db_session)
    targets = {
        "filer": {"filer_id": await _filer(db_session)},
        "security": {"security_id": await _security(db_session)},
        None: {},
    }

    await _refused(db_session, AlertRule(user_id=user_id, kind=kind, **targets[target]), constraint)


async def test_one_rule_per_kind_and_target(db_session: AsyncSession) -> None:
    user_id = await make_user(db_session)
    filer_id = await _filer(db_session)
    db_session.add(AlertRule(user_id=user_id, kind="filer_filed", filer_id=filer_id))
    await db_session.flush()

    await _refused(
        db_session,
        AlertRule(user_id=user_id, kind="filer_filed", filer_id=filer_id, min_value=1),
        "uq_alert_rule_user_id_kind_target",
    )


async def test_the_digest_is_asked_for_once_though_its_target_is_null(
    db_session: AsyncSession,
) -> None:
    """NULLS NOT DISTINCT: without it, two nulls never collide."""
    user_id = await make_user(db_session)
    db_session.add(AlertRule(user_id=user_id, kind="season_digest"))
    await db_session.flush()

    await _refused(
        db_session,
        AlertRule(user_id=user_id, kind="season_digest", channels=["email"]),
        "uq_alert_rule_user_id_kind_target",
    )


# --- alert_rule: settings ---------------------------------------------------------


@pytest.mark.parametrize("channels", [[], ["sms"], ["in_app", "push"]])
async def test_channels_must_be_known_and_at_least_one(
    db_session: AsyncSession, channels: list[str]
) -> None:
    user_id = await make_user(db_session)

    await _refused(
        db_session,
        AlertRule(user_id=user_id, kind="season_digest", channels=channels),
        "ck_alert_rule_channels_are_known",
    )


async def test_a_threshold_of_nothing_is_refused(db_session: AsyncSession) -> None:
    """Null is "every event": one way to say it."""
    user_id = await make_user(db_session)

    await _refused(
        db_session,
        AlertRule(user_id=user_id, kind="season_digest", min_value=0),
        "ck_alert_rule_min_value_is_positive",
    )
