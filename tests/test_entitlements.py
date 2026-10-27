"""Resolving entitlements, with no database: the catalog's rules and the order a key resolves in.

What the seeded plans allow, and the tables' checks, are tested against
Postgres in tests/integration/test_entitlements.py.
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest

from app.accounts import entitlements
from app.accounts.entitlements import (
    Catalog,
    CatalogError,
    EntitlementsUnavailableError,
    FlagKey,
    Grant,
    LimitKey,
    PlanTerms,
    can,
    limit,
    plan_of,
)
from app.db.models import ENTITLEMENT_KEYS, FLAG_KEYS, LIMIT_KEYS


def _terms(slug: str, **values: Any) -> PlanTerms:
    base: dict[str, Any] = {
        "history_quarters": 4,
        "insider_months": 6,
        "watchlist_items": 10,
        "screener_saved_filters": 3,
        "api_calls_month": 0,
        "mcp_calls_month": 0,
        "export_enabled": False,
        "alerts_instant": False,
        "mcp_enabled": False,
    }
    return PlanTerms(
        slug=slug,
        name=slug.title(),
        price_cents=0,
        currency="USD",
        interval=None,
        is_public=True,
        values=base | values,
    )


CATALOG = Catalog.build(
    [
        _terms("free"),
        _terms("pro", history_quarters=20, watchlist_items=None, export_enabled=True),
        _terms("pro_launch", history_quarters=24, watchlist_items=None, export_enabled=True),
    ]
)


@dataclass(frozen=True)
class Someone:
    grant: Grant | None


def _grant(plan: str, **kwargs: Any) -> Someone:
    return Someone(
        Grant(
            plan=plan,
            grandfathered=kwargs.get("grandfathered"),
            overrides=kwargs.get("overrides", {}),
            ends_at=kwargs.get("ends_at"),
        )
    )


@pytest.fixture(autouse=True)
def catalog() -> Iterator[None]:
    entitlements.install(CATALOG)
    yield
    entitlements.install(None)


def test_the_key_types_and_the_tables_name_the_same_keys() -> None:
    assert set(get_args(LimitKey)) == set(LIMIT_KEYS)
    assert set(get_args(FlagKey)) == set(FLAG_KEYS)
    assert len(ENTITLEMENT_KEYS) == len(set(ENTITLEMENT_KEYS)) == 9


def test_signed_out_is_free() -> None:
    assert limit(None, "watchlist_items") == 10
    assert can(None, "export_enabled") is False
    assert plan_of(None).slug == "free"


def test_signed_in_without_a_subscription_is_free_by_the_same_path() -> None:
    nobody = Someone(grant=None)

    assert entitlements.entitlements(nobody) == entitlements.entitlements(None)
    assert plan_of(nobody).slug == "free"


def test_a_plan_gives_its_values_and_null_is_no_limit() -> None:
    pro = _grant("pro")

    assert limit(pro, "history_quarters") == 20
    assert limit(pro, "watchlist_items") is None
    assert can(pro, "export_enabled") is True
    assert plan_of(pro).slug == "pro"


def test_the_grandfathered_plan_comes_before_the_plan() -> None:
    launch = _grant("pro", grandfathered="pro_launch")

    assert limit(launch, "history_quarters") == 24
    # Billed for Pro: the plan the account is on is not the terms it keeps.
    assert plan_of(launch).slug == "pro"


def test_an_override_comes_before_both() -> None:
    comped = _grant(
        "pro", grandfathered="pro_launch", overrides={"history_quarters": 40, "mcp_enabled": True}
    )

    assert limit(comped, "history_quarters") == 40
    assert can(comped, "mcp_enabled") is True
    assert limit(comped, "insider_months") == 6


def test_an_override_of_zero_or_false_is_kept_not_skipped() -> None:
    narrowed = _grant("pro", overrides={"watchlist_items": 0, "export_enabled": False})

    assert limit(narrowed, "watchlist_items") == 0
    assert can(narrowed, "export_enabled") is False


def test_a_grant_past_its_end_is_free() -> None:
    ended = _grant("pro", ends_at=datetime.now(UTC) - timedelta(seconds=1))
    ending = _grant("pro", ends_at=datetime.now(UTC) + timedelta(days=1))

    assert limit(ended, "history_quarters") == 4
    assert plan_of(ended).slug == "free"
    assert limit(ending, "history_quarters") == 20


def test_a_plan_the_catalog_does_not_know_yet_is_free() -> None:
    newer = _grant("enterprise")

    assert limit(newer, "history_quarters") == 4
    assert plan_of(newer).slug == "free"


def test_a_flag_is_not_a_limit_nor_the_other_way() -> None:
    with pytest.raises(ValueError, match="not a limit"):
        limit(None, "export_enabled")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a flag"):
        can(None, "history_quarters")  # type: ignore[arg-type]


def test_before_any_catalog_nothing_is_guessed() -> None:
    entitlements.install(None)

    with pytest.raises(EntitlementsUnavailableError):
        limit(None, "watchlist_items")
    with pytest.raises(EntitlementsUnavailableError):
        can(_grant("pro"), "export_enabled")


def test_the_catalog_refuses_a_plan_missing_a_key() -> None:
    gappy = _terms("pro")
    values = dict(gappy.values)
    del values["mcp_enabled"]

    with pytest.raises(CatalogError, match="pro has no mcp_enabled"):
        Catalog.build([_terms("free"), PlanTerms(**{**_fields(gappy), "values": values})])


def test_the_catalog_refuses_no_free_plan() -> None:
    with pytest.raises(CatalogError, match="no 'free' plan"):
        Catalog.build([_terms("pro")])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("watchlist_items", -1),
        ("watchlist_items", 2.5),
        ("watchlist_items", "10"),
        # bool is an int to Python; not to a limit.
        ("watchlist_items", True),
        ("export_enabled", 1),
        ("export_enabled", None),
    ],
)
def test_the_catalog_refuses_a_value_of_the_wrong_kind(key: str, value: object) -> None:
    with pytest.raises(CatalogError, match=f"free.{key} must be"):
        Catalog.build([_terms("free", **{key: value})])


def test_a_grant_survives_the_session_json() -> None:
    grant = Grant(
        plan="pro",
        grandfathered="pro_launch",
        overrides={"watchlist_items": None, "mcp_enabled": True},
        ends_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert Grant.from_json(grant.to_json()) == grant


# --- admin overrides ----------------------------------------------------------------

LATER = datetime.now(UTC) + timedelta(days=30)
EARLIER = datetime.now(UTC) - timedelta(days=1)


def _overridden(override: str, plan: str | None = None, **kwargs: Any) -> Someone:
    return Someone(
        Grant(
            plan=plan,
            grandfathered=kwargs.get("grandfathered"),
            overrides=kwargs.get("overrides", {}),
            ends_at=kwargs.get("ends_at"),
            override=override,
            override_ends_at=kwargs.get("override_ends_at", LATER),
        )
    )


def test_an_override_needs_no_subscription() -> None:
    friend = _overridden("pro")

    assert limit(friend, "history_quarters") == 20
    assert can(friend, "export_enabled") is True
    assert plan_of(friend).slug == "pro"


def test_an_override_wins_whole_over_a_subscription_and_its_own_values() -> None:
    """Above the subscription's per-key values too: an override gives a plan, whole."""
    support = _overridden(
        "pro_launch", plan="pro", overrides={"history_quarters": 8}, grandfathered="pro"
    )

    assert limit(support, "history_quarters") == 24
    assert plan_of(support).slug == "pro_launch"


def test_an_expired_override_gives_the_subscription_back() -> None:
    lapsed = _overridden("pro_launch", plan="pro", override_ends_at=EARLIER)

    assert limit(lapsed, "history_quarters") == 20
    assert plan_of(lapsed).slug == "pro"


def test_an_expired_override_without_a_subscription_is_free() -> None:
    lapsed = _overridden("pro", override_ends_at=EARLIER)

    assert entitlements.entitlements(lapsed) == entitlements.entitlements(None)
    assert plan_of(lapsed).slug == "free"


def test_an_override_outlives_a_cancelled_subscription() -> None:
    kept = _overridden("pro", plan="pro_launch", ends_at=EARLIER)

    assert limit(kept, "history_quarters") == 20
    assert plan_of(kept).slug == "pro"


def test_an_override_the_catalog_does_not_know_yet_falls_to_the_subscription() -> None:
    """A snapshot made in another process, before this one's next reload."""
    unknown = _overridden("pro_20261027", plan="pro")

    assert limit(unknown, "history_quarters") == 20


def test_a_grant_with_an_override_survives_the_session_json() -> None:
    grant = Grant(
        plan=None,
        grandfathered=None,
        overrides={},
        ends_at=None,
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        override="pro",
        override_ends_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert Grant.from_json(grant.to_json()) == grant


def test_a_grant_kept_before_overrides_reads_as_none() -> None:
    """A session's JSON from before 0041 has no override keys."""
    stored = Grant(plan="pro", grandfathered=None, overrides={}, ends_at=None).to_json()
    del stored["override"], stored["override_ends_at"]

    assert Grant.from_json(stored).override is None


async def test_a_reload_that_fails_keeps_the_catalog_it_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(session: object) -> Catalog:
        raise CatalogError("pro has no mcp_enabled")

    async def once(seconds: float) -> None:
        raise asyncio.CancelledError

    class Factory:
        def __call__(self) -> "Factory":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(entitlements, "load_catalog", refused)
    monkeypatch.setattr(asyncio, "sleep", once)

    with pytest.raises(asyncio.CancelledError):
        await entitlements.keep_fresh(Factory(), 60)  # type: ignore[arg-type]

    assert entitlements.current() is CATALOG


def _fields(terms: PlanTerms) -> dict[str, Any]:
    return {name: getattr(terms, name) for name in PlanTerms.__slots__}
