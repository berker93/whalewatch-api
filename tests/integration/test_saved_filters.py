"""``/v1/saved-filters``, against a real Postgres.

A saved filter is the screener's filters under a name, so what matters is
that one saves only what the screener takes, that running one is the
screener's screen for its query, and that one saved before the vocabulary
changed still reads: what no longer does is named and left out, never a 500
and never applied as something else. The plan's limit is
``test_plan_enforcement.py``'s.
"""

from typing import Any
from urllib.parse import parse_qsl

from httpx import AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SavedFilter
from tests.integration.accounts import FRONTEND, make_user, sign_in
from tests.integration.test_market_endpoint import _get
from tests.integration.test_screener_endpoint import CHARLIE, _rows, _two_quarters

PATH = "/v1/saved-filters"


async def _signed_in(client: AsyncClient, session: AsyncSession) -> int:
    user_id = await make_user(session)
    await sign_in(client)
    return user_id


async def _save(client: AsyncClient, name: str = "Mine", **params: Any) -> Any:
    response = await client.post(PATH, json={"name": name, "params": params}, headers=FRONTEND)
    assert response.status_code == 201, response.text
    return response.json()


async def _kept(session: AsyncSession, user_id: int, params: dict[str, Any]) -> int:
    """A row as an older release, or an older vocabulary, wrote it."""
    filter_id = await session.scalar(
        insert(SavedFilter)
        .values(user_id=user_id, name="Old", params=params)
        .returning(SavedFilter.id)
    )
    assert filter_id is not None
    return filter_id


# --- saving ---------------------------------------------------------------------


async def test_a_filter_is_kept_as_the_vocabulary_reads_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)

    saved = await _save(
        client,
        " Activists opening ",
        filters={"action": ["new", "add"], "filer_category": ["activist"], "min_weight": [" 5 "]},
        sort="weight",
    )

    assert saved["name"] == "Activists opening"
    assert saved["params"] == {
        "filters": {"action": ["add", "new"], "min_weight": ["5"], "filer_category": ["activist"]},
        "sort": "weight",
        "order": "desc",
    }
    assert [f["key"] for f in saved["filters"]] == ["action", "min_weight", "filer_category"]
    assert saved["filters"][0]["label"] == "What happened"
    assert saved["query"] == (
        "action=add%2Cnew&min_weight=5&filer_category=activist&sort=weight&order=desc"
    )
    assert saved["ignored"] == []
    assert saved["last_run_at"] is None
    assert (await _get(client, PATH))["data"] == [saved]


async def test_no_filters_is_every_position_by_value(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)

    saved = await _save(client)

    assert saved["params"] == {"filters": {}, "sort": "value", "order": "desc"}
    assert saved["query"] == "sort=value&order=desc"


async def test_what_the_screener_would_refuse_is_never_saved(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)
    await _two_quarters(db_session)
    refused = {
        "min_wieght": {"filters": {"min_wieght": ["5"]}},
        "period": {"filters": {"period": ["2024Q2"]}},
        "out of range": {"filters": {"min_weight": ["150"]}},
        "unavailable": {"filters": {"sector": ["Energy"]}},
        "no such investor": {"filters": {"filer_slug": ["value-a", "nobody"]}},
        "no such sort": {"sort": "loudest"},
        "a string, not a list": {"filters": {"min_weight": "5"}},
        "paging": {"limit": 10},
    }

    for case, params in refused.items():
        response = await client.post(PATH, json={"name": "x", "params": params}, headers=FRONTEND)
        assert response.status_code == 422, case
    assert (await _get(client, PATH))["data"] == []


async def test_every_filter_refused_is_named_at_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)

    response = await client.post(
        PATH,
        json={"name": "x", "params": {"filters": {"min_wieght": ["5"], "action": ["bought"]}}},
        headers=FRONTEND,
    )

    errors = response.json()["error"]["detail"]["errors"]
    assert {tuple(e["loc"]) for e in errors} == {
        ("body", "params", "filters", "min_wieght"),
        ("body", "params", "filters", "action"),
    }, errors


async def test_signed_out_reads_nothing_and_unverified_saves_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await client.get(PATH)).status_code == 401
    await make_user(db_session, verified=False)
    await sign_in(client)

    response = await client.post(PATH, json={"name": "x", "params": {}}, headers=FRONTEND)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_unverified"
    assert (await client.get(PATH)).status_code == 200


# --- renaming, duplicating, deleting -------------------------------------------


async def test_a_rename_leaves_the_screen_and_a_new_screen_the_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)
    saved = await _save(client, "Old name", filters={"action": ["new"]})
    url = f"{PATH}/{saved['id']}"

    renamed = await client.patch(url, json={"name": " New name "}, headers=FRONTEND)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "New name"
    assert renamed.json()["params"] == saved["params"]

    rescreened = await client.patch(
        url, json={"params": {"filters": {"min_weight": ["10"]}}}, headers=FRONTEND
    )
    assert rescreened.json()["name"] == "New name"
    assert rescreened.json()["params"]["filters"] == {"min_weight": ["10"]}

    for body in ({}, {"name": ""}, {"params": {"filters": {"min_wieght": ["5"]}}}):
        assert (await client.patch(url, json=body, headers=FRONTEND)).status_code == 422, body
    missing = await client.patch(f"{PATH}/999999", json={"name": "x"}, headers=FRONTEND)
    assert missing.status_code == 404


async def test_a_duplicate_is_a_new_filter_of_the_same_screen(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)
    saved = await _save(client, "Activists", filters={"filer_category": ["activist"]})

    copy = await client.post(f"{PATH}/{saved['id']}/duplicate", headers=FRONTEND)
    named = await client.post(
        f"{PATH}/{saved['id']}/duplicate", json={"name": "Mine too"}, headers=FRONTEND
    )

    assert copy.status_code == 201, copy.text
    assert copy.json()["name"] == "Activists (copy)"
    assert copy.json()["id"] != saved["id"]
    assert copy.json()["params"] == saved["params"]
    assert named.json()["name"] == "Mine too"
    listed = (await _get(client, PATH))["data"]
    assert [f["name"] for f in listed] == ["Activists", "Activists (copy)", "Mine too"]


async def test_a_long_names_copy_fits(client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(client, db_session)
    saved = await _save(client, "x" * 80)

    copy = (await client.post(f"{PATH}/{saved['id']}/duplicate", headers=FRONTEND)).json()

    assert copy["name"] == "x" * 73 + " (copy)"


async def test_a_duplicate_keeps_what_its_original_ignores(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A copy is of what was saved, so the vocabulary taking a filter back
    mends the copy as it mends the original."""
    user_id = await _signed_in(client, db_session)
    filter_id = await _kept(db_session, user_id, {"filters": {"min_wieght": ["5"]}})

    copy = (await client.post(f"{PATH}/{filter_id}/duplicate", headers=FRONTEND)).json()

    assert copy["ignored"] == [{"key": "min_wieght", "reason": "not a filter"}]


async def test_another_users_filter_is_not_found_and_not_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    other = await make_user(db_session, "bob@example.com")
    theirs = await _kept(db_session, other, {"filters": {}})
    await _signed_in(client, db_session)

    for method, url in (
        ("PATCH", f"{PATH}/{theirs}"),
        ("POST", f"{PATH}/{theirs}/duplicate"),
        ("GET", f"{PATH}/{theirs}/screen"),
    ):
        response = await client.request(
            method, url, json={"name": "x"} if method == "PATCH" else None, headers=FRONTEND
        )
        assert response.status_code == 404, (method, url)

    deleted = await client.delete(f"{PATH}/{theirs}", headers=FRONTEND)
    assert deleted.status_code == 204
    await sign_in(client, "bob@example.com")
    assert [f["id"] for f in (await _get(client, PATH))["data"]] == [str(theirs)]


async def test_a_delete_is_idempotent(client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(client, db_session)
    saved = await _save(client)

    for _ in range(2):
        response = await client.delete(f"{PATH}/{saved['id']}", headers=FRONTEND)
        assert response.status_code == 204
    assert (await _get(client, PATH))["data"] == []


# --- read again, every time ------------------------------------------------------


async def test_what_no_longer_reads_is_named_and_left_out(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Saved before a filter was renamed, a range narrowed, a sort dropped:
    the rest of the filter is the filter, and nothing is a 500."""
    user_id = await _signed_in(client, db_session)
    await _kept(
        db_session,
        user_id,
        {
            "filters": {
                "min_wieght": ["5"],
                "min_weight": ["150"],
                "sector": ["Energy"],
                "period": ["2024Q2"],
                "action": "new",
                "filer_category": ["activist"],
            },
            "sort": "loudest",
            "order": "sideways",
            "colour": "blue",
        },
    )

    [read] = (await _get(client, PATH))["data"]

    assert read["params"] == {
        "filters": {"filer_category": ["activist"]},
        "sort": "value",
        "order": "desc",
    }
    assert [(i["key"], i["reason"]) for i in read["ignored"]] == [
        ("colour", "not part of a saved filter"),
        ("min_wieght", "not a filter"),
        ("period", "chosen when the screen is run, not kept with it"),
        ("action", "a list of strings, as a client would send it"),
        ("min_weight", "150 is out of range: 0 to 100"),
        (
            "sector",
            "sector is not available yet: no source of sectors is loaded, so no "
            "stock has one. Leave it out.",
        ),
        ("sort", "unknown sort 'loudest': sorted by value"),
        ("order", "unknown order 'sideways': largest first"),
    ]
    assert read["query"] == "filer_category=activist&sort=value&order=desc"


async def test_a_filter_saved_for_the_flows_screen_reads_as_none(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A row from before 0050 holds GET /v1/flows's parameters: none is the
    screener's, and its min_value means a stock's dollars, not a position's,
    so none is applied."""
    user_id = await _signed_in(client, db_session)
    await _kept(
        db_session,
        user_id,
        {"direction": "buy", "min_value": "1000000", "period": "2024Q4", "sort": "buyers"},
    )

    [read] = (await _get(client, PATH))["data"]

    assert read["params"]["filters"] == {}
    assert {i["key"] for i in read["ignored"]} == {"direction", "min_value", "period", "sort"}


async def test_an_investor_whose_slug_has_gone_is_left_out(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await _signed_in(client, db_session)
    await _two_quarters(db_session)
    some = await _kept(db_session, user_id, {"filters": {"filer_slug": ["value-a", "renamed"]}})
    none = await _kept(db_session, user_id, {"filters": {"filer_slug": ["renamed"]}})

    by_id = {f["id"]: f for f in (await _get(client, PATH))["data"]}

    assert by_id[str(some)]["params"]["filters"] == {"filer_slug": ["value-a"]}
    assert by_id[str(some)]["ignored"] == [
        {"key": "filer_slug", "reason": "no investor has the slug 'renamed' now"}
    ]
    assert by_id[str(none)]["params"]["filters"] == {}
    assert "the filter is left out" in by_id[str(none)]["ignored"][0]["reason"]
    # And it runs, as what is left.
    screened = await _get(client, f"{PATH}/{some}/screen", period="2024Q2")
    assert {slug for slug, _ in _rows(screened)} == {"value-a"}


# --- running one ----------------------------------------------------------------


async def test_a_run_is_the_screeners_screen_for_its_query(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)
    await _two_quarters(db_session)
    saved = await _save(client, filters={"action": ["new", "add"]}, sort="weight", order="asc")

    run = await _get(client, f"{PATH}/{saved['id']}/screen", period="2024Q2")
    query = dict(parse_qsl(saved["query"]))
    screened = await _get(client, "/v1/screener", period="2024Q2", **query)

    assert run["data"] == screened["data"]
    assert run["meta"]["filters"] == screened["meta"]["filters"]
    assert run["meta"]["matches"] == screened["meta"]["matches"]
    assert ("value-a", CHARLIE) in _rows(run)
    assert run["meta"]["saved_filter"]["id"] == saved["id"]
    assert run["meta"]["saved_filter"]["last_run_at"] is not None
    [listed] = (await _get(client, PATH))["data"]
    assert listed["last_run_at"] == run["meta"]["saved_filter"]["last_run_at"]


async def test_a_run_is_of_the_latest_quarter_unless_asked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(client, db_session)
    await _two_quarters(db_session)
    saved = await _save(client, filters={"action": ["exit"]})

    latest = await _get(client, f"{PATH}/{saved['id']}/screen")
    earlier = await _get(client, f"{PATH}/{saved['id']}/screen", period="2024Q1")

    assert latest["meta"]["period"] == "2024Q2"
    assert ("activist-b", "44444D404") in _rows(latest)
    # 2024Q1 is everyone's first period: nothing to have sold out of.
    assert earlier["data"] == []


async def test_a_runs_filters_are_its_own(client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(client, db_session)
    await _two_quarters(db_session)
    saved = await _save(client)

    response = await client.get(f"{PATH}/{saved['id']}/screen", params={"min_weight": "5"})

    assert response.status_code == 422
    assert "this screen's filters are its own" in response.text
