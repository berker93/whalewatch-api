"""The envelope and the cursor, without a database.

What is pinned here is the contract a client sees: a cursor survives the round
trip exactly, anything else sent as ``?cursor=`` is a 400 rather than a 500,
``?limit=`` is bounded, and no number in any response is a JSON number. That
the cursors page a real table without gaps or repeats is
``tests/integration/test_pagination.py``.
"""

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import BigInteger, ClauseElement, Date, Numeric, Text, column, table
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect

from app.api.deps import PageParamsDep
from app.api.pagination import (
    CURSOR_VERSION,
    Cursor,
    InvalidCursorError,
    Keyset,
    SortKey,
    decode_cursor,
    encode_cursor,
    position,
)
from app.api.schemas.envelope import Envelope, Meta, Page
from app.api.schemas.types import Money

ROWS = table(
    "rows",
    column("id", BigInteger),
    column("value", Numeric),
    column("name", Text),
    column("day", Date),
)

BY_VALUE = Keyset(
    "rows.value",
    (
        SortKey("value", ROWS.c.value, Decimal, descending=True),
        SortKey("id", ROWS.c.id, int, descending=True),
    ),
)

#: One of every key type, and the directions mixed.
EVERY_TYPE = Keyset(
    "rows.every-type",
    (
        SortKey("value", ROWS.c.value, Decimal, descending=True),
        SortKey("name", ROWS.c.name, str),
        SortKey("day", ROWS.c.day, date),
        SortKey("at", ROWS.c.day, datetime),
        SortKey("id", ROWS.c.id, int),
    ),
)
POSITION: list[object] = [
    Decimal("2040000000.0001"),
    "APPLE INC",
    date(2026, 6, 30),
    datetime(2026, 8, 14, 13, 34, 5, tzinfo=UTC),
    9_007_199_254_740_993,  # more than a double carries, as a JSON number would be
]


def _token(payload: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


# --- the cursor ----------------------------------------------------------------------------


def test_a_cursor_round_trips_every_key_type_exactly() -> None:
    token = encode_cursor(EVERY_TYPE, POSITION)

    assert position(decode_cursor(token), EVERY_TYPE) == POSITION


def test_a_cursor_is_url_safe_and_unpadded() -> None:
    token = encode_cursor(EVERY_TYPE, POSITION)

    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_a_decimal_travels_as_a_string() -> None:
    """A JSON number would come back as a float, and not as the value it was."""
    token = encode_cursor(BY_VALUE, [Decimal("0.1"), 1])
    payload = json.loads(base64.urlsafe_b64decode(token + "=="))

    assert payload == {"v": CURSOR_VERSION, "k": "rows.value", "a": ["0.1", 1]}


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not a cursor!", id="not-base64"),
        pytest.param(base64.urlsafe_b64encode(b"\xff\xfe").decode(), id="not-utf8"),
        pytest.param(base64.urlsafe_b64encode(b"hello").decode(), id="not-json"),
        pytest.param(_token(["rows.value", ["1", 1]]), id="not-an-object"),
        pytest.param(_token({"v": CURSOR_VERSION, "k": "rows.value"}), id="missing-values"),
        pytest.param(
            _token({"v": CURSOR_VERSION, "k": "rows.value", "a": [], "x": 1}), id="extra-field"
        ),
        pytest.param(_token({"v": CURSOR_VERSION, "k": 1, "a": []}), id="keyset-not-a-name"),
        pytest.param(_token({"v": CURSOR_VERSION, "k": "rows.value", "a": {}}), id="not-a-list"),
        pytest.param("A" * 2000, id="too-long"),
    ],
)
def test_anything_we_did_not_issue_is_refused(token: str) -> None:
    with pytest.raises(InvalidCursorError) as caught:
        decode_cursor(token)

    assert caught.value.status_code == 400


def test_a_cursor_from_another_version_is_refused_by_name() -> None:
    token = _token({"v": CURSOR_VERSION + 1, "k": "rows.value", "a": ["1", 1]})

    with pytest.raises(InvalidCursorError, match="different version"):
        decode_cursor(token)


def test_a_cursor_from_another_ordering_is_refused() -> None:
    """Same arity, same types: only the name tells them apart, and it must."""
    other = Keyset("rows.other", BY_VALUE.keys)
    cursor = decode_cursor(encode_cursor(other, [Decimal(1), 1]))

    with pytest.raises(InvalidCursorError, match="different listing"):
        position(cursor, BY_VALUE)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["1"], id="too-few"),
        pytest.param(["1", 1, 1], id="too-many"),
        pytest.param([1, 1], id="decimal-as-number"),
        pytest.param([1.5, 1], id="decimal-as-float"),
        pytest.param(["NaN", 1], id="decimal-nan"),
        pytest.param(["Infinity", 1], id="decimal-infinite"),
        pytest.param(["lots", 1], id="decimal-garbage"),
        pytest.param([None, 1], id="null"),
        pytest.param(["1", "1"], id="int-as-string"),
        pytest.param(["1", True], id="int-as-bool"),
        pytest.param(["1", 1.0], id="int-as-float"),
    ],
)
def test_a_value_of_the_wrong_type_never_reaches_sql(values: list[object]) -> None:
    cursor = Cursor(keyset=BY_VALUE.name, values=tuple(values))

    with pytest.raises(InvalidCursorError):
        position(cursor, BY_VALUE)


@pytest.mark.parametrize(
    ("index", "raw"),
    [(2, "30/06/2026"), (2, 20260630), (3, "yesterday")],
    ids=["date-misspelled", "date-as-number", "datetime-garbage"],
)
def test_dates_must_be_iso_8601(index: int, raw: object) -> None:
    values = list(decode_cursor(encode_cursor(EVERY_TYPE, POSITION)).values)
    values[index] = raw

    with pytest.raises(InvalidCursorError):
        position(Cursor(EVERY_TYPE.name, tuple(values)), EVERY_TYPE)


# --- keys, read off a row ------------------------------------------------------------------


class _Row:
    """Enough of a SQLAlchemy ``Row`` for :meth:`Keyset.position_of`."""

    def __init__(self, **values: object) -> None:
        self._mapping = values


def test_a_null_key_is_our_bug_not_the_clients() -> None:
    with pytest.raises(ValueError, match="NOT NULL"):
        BY_VALUE.position_of(_Row(value=None, id=1))  # type: ignore[arg-type]


def test_a_key_the_statement_does_not_select_is_named() -> None:
    with pytest.raises(KeyError, match="'id'"):
        BY_VALUE.position_of(_Row(value=Decimal(1)))  # type: ignore[arg-type]


def test_a_key_of_an_undeclared_type_is_refused_on_the_way_out() -> None:
    """A float where a Decimal was declared would round-trip as something else."""
    with pytest.raises(TypeError, match="declared Decimal"):
        BY_VALUE.position_of(_Row(value=1.5, id=1))  # type: ignore[arg-type]


# --- the comparison ------------------------------------------------------------------------


#: SQLAlchemy's dialect constructors are not annotated.
_POSTGRES: Dialect = postgresql.dialect()  # type: ignore[no-untyped-call]


def _sql(clause: ClauseElement) -> str:
    return str(clause.compile(dialect=_POSTGRES, compile_kwargs={"literal_binds": True}))


def test_one_direction_is_one_row_comparison() -> None:
    """The form an index on ``(value, id)`` answers with a single seek."""
    assert _sql(BY_VALUE.after([Decimal("1.5"), 7])) == "(rows.value, rows.id) < (1.5, 7)"


def test_mixed_directions_expand() -> None:
    mixed = Keyset(
        "rows.mixed",
        (
            SortKey("value", ROWS.c.value, Decimal, descending=True),
            SortKey("name", ROWS.c.name, str),
            SortKey("id", ROWS.c.id, int),
        ),
    )

    assert _sql(mixed.after([Decimal(2), "b", 3])) == (
        "rows.value < 2 "
        "OR rows.value = 2 AND rows.name > 'b' "
        "OR rows.value = 2 AND rows.name = 'b' AND rows.id > 3"
    )


def test_order_by_follows_the_keys() -> None:
    assert [_sql(clause) for clause in BY_VALUE.order_by()] == ["rows.value DESC", "rows.id DESC"]


def test_a_keyset_needs_keys() -> None:
    with pytest.raises(ValueError, match="no keys"):
        Keyset("empty", ())


# --- the dependency ------------------------------------------------------------------------


@pytest.fixture
def page_app() -> FastAPI:
    app = FastAPI()

    @app.get("/things")
    async def things(page: PageParamsDep) -> dict[str, Any]:
        return {"limit": page.limit, "cursor": page.cursor and page.cursor.keyset}

    return app


@pytest.fixture
async def page_client(page_app: FastAPI) -> Any:
    async with AsyncClient(transport=ASGITransport(app=page_app), base_url="http://test") as c:
        yield c


async def test_limit_defaults_to_fifty(page_client: AsyncClient) -> None:
    response = await page_client.get("/things")

    assert response.json() == {"limit": 50, "cursor": None}


@pytest.mark.parametrize("limit", [1, 200])
async def test_limit_is_accepted_up_to_the_cap(page_client: AsyncClient, limit: int) -> None:
    response = await page_client.get("/things", params={"limit": limit})

    assert response.json()["limit"] == limit


@pytest.mark.parametrize("limit", ["0", "-1", "201", "lots"])
async def test_limit_outside_the_cap_is_refused_not_clamped(
    page_client: AsyncClient, limit: str
) -> None:
    response = await page_client.get("/things", params={"limit": limit})

    assert response.status_code == 422


async def test_a_mangled_cursor_is_a_400_before_the_handler(page_client: AsyncClient) -> None:
    response = await page_client.get("/things", params={"cursor": "not a cursor!"})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid cursor")


async def test_a_good_cursor_reaches_the_handler_decoded(page_client: AsyncClient) -> None:
    token = encode_cursor(BY_VALUE, [Decimal(1), 1])

    response = await page_client.get("/things", params={"cursor": token})

    assert response.json() == {"limit": 50, "cursor": "rows.value"}


# --- decimals, throughout ------------------------------------------------------------------


class _Priced(BaseModel):
    money: Money
    bare: Decimal


def test_the_envelope_serialises_decimals_as_strings() -> None:
    envelope = Envelope[_Priced](
        data=[_Priced(money=Decimal("2040000000.00"), bare=Decimal("0.1000"))],
        meta=Meta(generated_at=datetime(2026, 10, 2, tzinfo=UTC)),
        page=Page(limit=50, next_cursor=None),
    )

    assert json.loads(envelope.model_dump_json())["data"] == [
        {"money": "2040000000.00", "bare": "0.1000"}
    ]


def _numbers(schema: Any, path: str = "") -> list[str]:
    """Every place in a JSON schema that admits a JSON number."""
    found: list[str] = []
    if isinstance(schema, dict):
        if schema.get("type") == "number":
            found.append(path)
        for key, value in schema.items():
            found += _numbers(value, f"{path}/{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found += _numbers(value, f"{path}/{i}")
    return found


def _response_schemas(app: FastAPI) -> dict[str, Any]:
    """The components the app's responses use: the serialisation-side schemas."""
    components = app.openapi()["components"]["schemas"]
    # FastAPI splits a model whose input and output schemas differ into
    # Name-Input and Name-Output; the -Input half is request validation.
    return {name: s for name, s in components.items() if not name.endswith("-Input")}


def test_no_response_from_the_api_carries_a_json_number(app: FastAPI) -> None:
    """Every quantity here is ``numeric``; a JSON number is a double and rounds it.

    Not a check on any one model, which would only hold the models it names:
    a check on the whole published schema, so that a ``float`` or an unwrapped
    ``Decimal`` added to any response model by any endpoint fails here.
    """
    assert _numbers(_response_schemas(app)) == []


def test_the_check_above_would_catch_a_number(app: FastAPI) -> None:
    class _Bad(BaseModel):
        ratio: float

    @app.get("/bad", response_model=Envelope[_Bad])
    async def bad() -> None: ...

    app.openapi_schema = None  # rebuilt with the route in it
    assert _numbers(_response_schemas(app)) != []
