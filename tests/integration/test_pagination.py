"""Keyset pagination, walked through the app against a real Postgres.

No endpoint pages anything yet, so the walk goes through one written here: a
route over a temporary table, mounted on an app built by ``create_app``, with
the shared ``?limit=``/``?cursor=`` dependency and ``paginate`` doing exactly
what a real collection endpoint will have them do. The table lives in the
test's transaction and goes with its rollback.

The values are drawn from seven, so most rows tie on the first key with dozens
of others and nearly every page boundary falls inside a tie. That is where a
keyset without a unique last key loses rows, and where a row comparison
written the wrong way round repeats them.
"""

import base64
import json
import random
from decimal import Decimal
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Numeric, Text, column, delete, insert, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParamsDep, SessionDep
from app.api.meta import unscoped_meta
from app.api.pagination import CURSOR_VERSION, Keyset, SortKey, encode_cursor, paginate
from app.api.schemas.envelope import Envelope
from app.api.schemas.types import Money
from app.core.config import Settings
from app.main import create_app

ROW_COUNT = 500

PROBE = table(
    "pagination_probe",
    column("id", BigInteger),
    column("value", Numeric),
    column("label", Text),
)

#: Value descending with the id as the tie-break: the shape of every
#: "largest first" listing in the API, all one direction.
BY_VALUE = Keyset(
    "probe.value",
    (
        SortKey("value", PROBE.c.value, Decimal, descending=True),
        SortKey("id", PROBE.c.id, int, descending=True),
    ),
)

#: Directions mixed, so the comparison is the expanded OR rather than one row
#: comparison.
MIXED = Keyset(
    "probe.mixed",
    (
        SortKey("value", PROBE.c.value, Decimal, descending=True),
        SortKey("label", PROBE.c.label, str),
        SortKey("id", PROBE.c.id, int),
    ),
)

#: More digits than a double carries, so a value that came back as a JSON
#: number would come back changed.
VALUES = [Decimal(v) for v in ("9007199254740993.0001", "1500", "1500.0001", "42", "7", "1", "0")]


class ProbeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    value: Money
    label: str


router = APIRouter()


@router.get("/probe", response_model=Envelope[ProbeRead])
async def list_probe(
    session: SessionDep, page: PageParamsDep, mixed: bool = False
) -> Envelope[ProbeRead]:
    rows, page_info = await paginate(session, select(PROBE), MIXED if mixed else BY_VALUE, page)
    return Envelope(
        data=[ProbeRead.model_validate(row) for row in rows], meta=unscoped_meta(), page=page_info
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Overrides the conftest's: the real app, plus the probe route."""
    application = create_app(settings)
    application.include_router(router)
    return application


@pytest.fixture
async def rows(db_session: AsyncSession) -> list[dict[str, Any]]:
    """500 rows in the probe table, inserted in no particular order."""
    await db_session.execute(
        text(
            "CREATE TEMP TABLE pagination_probe "
            "(id bigint PRIMARY KEY, value numeric(24, 4) NOT NULL, label text NOT NULL)"
        )
    )
    rng = random.Random(500)
    ids = list(range(1, ROW_COUNT + 1))
    rng.shuffle(ids)
    seeded = [{"id": i, "value": rng.choice(VALUES), "label": rng.choice("abcde")} for i in ids]
    await db_session.execute(insert(PROBE), seeded)
    return seeded


def _by_value(row: dict[str, Any]) -> tuple[Decimal, int]:
    return (-row["value"], -row["id"])


def _mixed(row: dict[str, Any]) -> tuple[Decimal, str, int]:
    return (-row["value"], row["label"], row["id"])


async def _page(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/probe", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _walk(
    client: AsyncClient, *, cursor: str | None = None, **params: Any
) -> list[list[Any]]:
    """Every page from ``cursor`` (or the start) to the end, as lists of rows."""
    pages: list[list[Any]] = []
    while True:
        body = await _page(client, **params, **({"cursor": cursor} if cursor else {}))
        pages.append(body["data"])
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) <= ROW_COUNT, "the walk is not making progress"


def _ids(pages: list[list[Any]]) -> list[int]:
    return [row["id"] for page in pages for row in page]


# --- to exhaustion -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param(37, id="37-ragged-last-page"),
        pytest.param(100, id="100-exactly-full-last-page"),
        pytest.param(200, id="200-the-cap"),
        pytest.param(None, id="default"),
    ],
)
async def test_a_walk_returns_every_row_once_in_order(
    client: AsyncClient, rows: list[dict[str, Any]], limit: int | None
) -> None:
    params: dict[str, Any] = {"limit": limit} if limit else {}
    pages = await _walk(client, **params)
    ids = _ids(pages)
    size = limit or 50

    assert len(ids) == len(set(ids)), "a row was returned twice"
    assert set(ids) == {row["id"] for row in rows}, "a row was never returned"
    assert ids == [row["id"] for row in sorted(rows, key=_by_value)]
    # Every page full but the last, which is never empty: a last page that is
    # exactly full says so instead of handing out a cursor to nothing.
    assert [len(page) for page in pages[:-1]] == [size] * (len(pages) - 1)
    assert 0 < len(pages[-1]) <= size
    assert len(pages) == -(-ROW_COUNT // size)


async def test_a_walk_with_mixed_directions(
    client: AsyncClient, rows: list[dict[str, Any]]
) -> None:
    ids = _ids(await _walk(client, mixed=True, limit=37))

    assert ids == [row["id"] for row in sorted(rows, key=_mixed)]


async def test_values_come_back_exactly(client: AsyncClient, rows: list[dict[str, Any]]) -> None:
    body = await _page(client, limit=1)

    assert body["data"][0]["value"] == "9007199254740993.0001"


async def test_an_empty_listing_is_one_empty_page(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_session.execute(
        text("CREATE TEMP TABLE pagination_probe (id bigint, value numeric, label text)")
    )

    body = await _page(client)

    assert body["data"] == []
    assert body["page"] == {"limit": 50, "next_cursor": None}


# --- stability -----------------------------------------------------------------------------


async def test_rows_inserted_and_deleted_mid_walk_cause_no_repeat_and_no_gap(
    client: AsyncClient, db_session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    """The failure mode of ``OFFSET``, tried against the cursor.

    Three pages in, rows are inserted on both sides of the position the cursor
    holds — including two that tie with the last row seen on value and fall
    either side of it on id — and rows already returned are deleted. Under
    ``OFFSET`` each insert behind the position would repeat a row on the next
    page and each delete would skip one.
    """
    first = [await _page(client, limit=37)]
    for _ in range(2):
        first.append(await _page(client, limit=37, cursor=first[-1]["page"]["next_cursor"]))
    seen = _ids([page["data"] for page in first])
    last = first[-1]["data"][-1]
    last_value = Decimal(last["value"])

    behind = [  # sort before the position: already passed, so never seen
        {"id": 10_001, "value": VALUES[0] + 1, "label": "a"},
        {"id": 10_002, "value": last_value, "label": "a"},  # tie, higher id
    ]
    ahead = [  # sort after it: seen, once, in their place
        {"id": -1, "value": last_value, "label": "a"},  # tie, lower id
        {"id": 10_003, "value": Decimal(-1), "label": "a"},
    ]
    await db_session.execute(insert(PROBE), behind + ahead)
    await db_session.execute(delete(PROBE).where(PROBE.c.id.in_(seen[:20])))

    rest = _ids(await _walk(client, limit=37, cursor=first[-1]["page"]["next_cursor"]))
    walked = seen + rest

    assert len(walked) == len(set(walked)), "a row was returned twice"
    assert set(walked) == {row["id"] for row in rows} | {row["id"] for row in ahead}
    assert rest == [
        row["id"] for row in sorted(rows + ahead, key=_by_value) if row["id"] not in seen
    ]


async def test_a_cursor_returns_the_same_page_every_time(
    client: AsyncClient, rows: list[dict[str, Any]]
) -> None:
    cursor = (await _page(client, limit=37))["page"]["next_cursor"]

    again = [await _page(client, limit=37, cursor=cursor) for _ in range(3)]

    assert again[0]["data"] == again[1]["data"] == again[2]["data"]
    assert again[0]["page"] == again[1]["page"] == again[2]["page"]


async def test_a_cursor_can_be_resumed_with_a_different_limit(
    client: AsyncClient, rows: list[dict[str, Any]]
) -> None:
    """The cursor is a position, not a page number, so the page size is free."""
    cursor = (await _page(client, limit=37))["page"]["next_cursor"]

    rest = _ids(await _walk(client, limit=200, cursor=cursor))

    assert rest == [row["id"] for row in sorted(rows, key=_by_value)][37:]


# --- refusals ------------------------------------------------------------------------------


async def test_a_cursor_from_another_sort_order_is_a_400(
    client: AsyncClient, rows: list[dict[str, Any]]
) -> None:
    cursor = (await _page(client, mixed=True, limit=10))["page"]["next_cursor"]

    response = await client.get("/probe", params={"cursor": cursor})

    assert response.status_code == 400
    assert "different listing or sort order" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["not a number", 1], id="value-not-a-decimal"),
        pytest.param(["1", "1; DROP TABLE filing"], id="id-not-an-int"),
        pytest.param(["1"], id="too-few-values"),
    ],
)
async def test_a_forged_cursor_is_a_400_not_a_500(
    client: AsyncClient, rows: list[dict[str, Any]], values: list[object]
) -> None:
    """Forged with the right keyset name and version, so only typing stops it."""
    payload = {"v": CURSOR_VERSION, "k": BY_VALUE.name, "a": values}
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    response = await client.get("/probe", params={"cursor": cursor})

    assert response.status_code == 400


async def test_a_well_formed_position_nobody_was_given_is_just_a_position(
    client: AsyncClient, rows: list[dict[str, Any]]
) -> None:
    """Why the cursor is not signed: a forged one only reaches rows a filter could."""
    cursor = encode_cursor(BY_VALUE, [Decimal(42), 0])

    ids = _ids(await _walk(client, limit=200, cursor=cursor))

    assert ids == [row["id"] for row in sorted(rows, key=_by_value) if row["value"] < 42]
