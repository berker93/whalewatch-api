"""The OpenAPI document is the frontend's contract, so it is tested as one.

Each test is a rule a generated client depends on. Each one fails naming the
route or the field, so the fix is where the failure says.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.api.deps import get_session
from app.api.openapi import OPENAPI_PATH, build, render

Schema = dict[str, Any]

#: The bodies a 4xx may have: a Problem, or a Problem with more beside it.
PROBLEMS = {"Problem", "ValidationProblem", "StockNotFound"}

#: Not an error, whatever the status says: /ready reports which dependency is
#: down in the same body it reports health in.
NOT_ERRORS = {("/ready", "503")}


@pytest.fixture(scope="module")
def document() -> Schema:
    return build()


def _operations(document: Schema) -> Iterator[tuple[str, str, Schema]]:
    for path, item in document["paths"].items():
        for method, operation in item.items():
            yield path, method, operation


def _ref_name(schema: Schema) -> str | None:
    ref = schema.get("$ref")
    return ref.rsplit("/", 1)[1] if isinstance(ref, str) else None


def _refs(schema: Schema) -> set[str]:
    """Every component ``schema`` is, or is one of."""
    names = {_ref_name(schema)} | {_ref_name(s) for s in schema.get("anyOf", [])}
    return {name for name in names if name is not None}


# --- the file ---------------------------------------------------------------------


def test_the_committed_schema_is_current() -> None:
    """The same check CI makes with git diff, run where `make check` runs."""
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(), (
        "openapi.json is stale: run `make openapi`, read the diff, and commit it"
    )


def test_the_environment_cannot_change_the_schema(
    document: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise CI and a laptop disagree, and the staleness check fails on a
    file nobody changed. ``.env`` is not the only way in: pydantic-settings
    reads os.environ even with the file switched off."""
    monkeypatch.setenv("APP_NAME", "Leaky")
    monkeypatch.setenv("ENVIRONMENT", "production")

    assert json.loads(render()) == document
    assert document["info"]["title"] == "WhaleWatch API"


# --- operations -------------------------------------------------------------------


def test_every_route_names_its_operation(app: FastAPI) -> None:
    """FastAPI's default is ``read_portfolio_v1_investors__slug__portfolio_get``,
    which a generated client would expose as the method name."""
    unnamed = [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema and not route.operation_id
    ]
    assert unnamed == []


def test_operation_ids_are_camel_case_and_unique(document: Schema) -> None:
    ids = [operation["operationId"] for _, _, operation in _operations(document)]
    assert len(ids) == len(set(ids))
    assert [i for i in ids if not re.fullmatch(r"[a-z][a-zA-Z0-9]*", i)] == []


def test_every_operation_has_a_summary_a_description_and_a_tag(document: Schema) -> None:
    missing = [
        (path, key)
        for path, _, operation in _operations(document)
        for key in ("summary", "description", "tags")
        if not operation.get(key)
    ]
    assert missing == []


def test_every_parameter_is_described(document: Schema) -> None:
    missing = [
        (path, parameter["name"])
        for path, _, operation in _operations(document)
        for parameter in operation.get("parameters", [])
        if not parameter.get("description")
    ]
    assert missing == []


# --- errors -----------------------------------------------------------------------


def test_every_error_is_a_problem(document: Schema) -> None:
    wrong = []
    for path, _, operation in _operations(document):
        for status, response in operation["responses"].items():
            if not status.startswith(("4", "5")) or (path, status) in NOT_ERRORS:
                continue
            schema = response.get("content", {}).get("application/json", {}).get("schema", {})
            if not _refs(schema) or not _refs(schema) <= PROBLEMS:
                wrong.append((path, status, schema))
    assert wrong == []


def test_fastapis_own_validation_error_is_nowhere(document: Schema) -> None:
    """It would mean a route left its 422 to FastAPI, which documents a body
    the handlers no longer send."""
    assert "HTTPValidationError" not in document["components"]["schemas"]


def test_every_route_with_a_parameter_documents_its_422(document: Schema) -> None:
    wrong = [
        path
        for path, _, operation in _operations(document)
        if operation.get("parameters")
        and _refs(
            operation["responses"]
            .get("422", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        != {"ValidationProblem"}
    ]
    assert wrong == []


def test_every_paginated_route_documents_a_bad_cursor(document: Schema) -> None:
    wrong = [
        path
        for path, _, operation in _operations(document)
        if any(p["name"] == "cursor" for p in operation.get("parameters", []))
        != ("400" in operation["responses"])
    ]
    assert wrong == []


async def test_an_unknown_path_is_a_problem(client: AsyncClient) -> None:
    response = await client.get("/v1/nothing-here")

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "detail": "Not Found"}


async def test_a_wrong_method_is_a_problem(client: AsyncClient) -> None:
    response = await client.post("/health")

    assert response.status_code == 405
    assert response.json() == {"code": "method_not_allowed", "detail": "Method Not Allowed"}


async def test_a_malformed_parameter_is_a_validation_problem(
    app: FastAPI, client: AsyncClient
) -> None:
    # Refused before the handler runs, so the session is never used; but the
    # dependency is still resolved, and this app has no pool to resolve it from.
    app.dependency_overrides[get_session] = lambda: None
    response = await client.get("/v1/search", params={"q": "berkshire", "limit": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["detail"].startswith("limit: ")
    assert [(e["loc"], e["type"]) for e in body["errors"]] == [
        (["query", "limit"], "greater_than_equal")
    ]


# --- models -----------------------------------------------------------------------


def _object_refs(document: Schema) -> set[str]:
    return {
        name
        for name, schema in document["components"]["schemas"].items()
        if schema.get("type") == "object"
    }


def _is_object(prop: Schema, objects: set[str]) -> bool:
    """A nested model, a list of them, or either or null: described, never exemplified.

    Its own fields carry the examples, and Swagger composes them.
    """
    if _ref_name(prop) in objects:
        return True
    if prop.get("type") == "array" and _is_object(prop.get("items", {}), objects):
        return True
    branches = [b for b in prop.get("anyOf", []) if b.get("type") != "null"]
    return len(branches) == 1 and _is_object(branches[0], objects)


def test_every_field_is_described(document: Schema) -> None:
    missing = [
        f"{name}.{field}"
        for name, schema in document["components"]["schemas"].items()
        for field, prop in schema.get("properties", {}).items()
        if not prop.get("description")
    ]
    assert missing == []


def test_every_value_field_has_an_example(document: Schema) -> None:
    objects = _object_refs(document)
    missing = [
        f"{name}.{field}"
        for name, schema in document["components"]["schemas"].items()
        for field, prop in schema.get("properties", {}).items()
        if not prop.get("examples") and not _is_object(prop, objects)
    ]
    assert missing == []


def test_every_enum_says_what_it_is(document: Schema) -> None:
    missing = [
        name
        for name, schema in document["components"]["schemas"].items()
        if "enum" in schema and not schema.get("description")
    ]
    assert missing == []


def test_component_names_are_not_mangled(document: Schema) -> None:
    """``Envelope_InvestorSummary_`` is what a bare generic is keyed as; see
    :class:`app.api.schemas.envelope.Envelope`."""
    mangled = [n for n in document["components"]["schemas"] if not re.fullmatch(r"[A-Za-z]+", n)]
    assert mangled == []


def test_decimals_are_strings_the_schema_says_are_decimals(document: Schema) -> None:
    value = document["components"]["schemas"]["PortfolioPosition"]["properties"]["value_usd"]

    assert (value["type"], value["format"]) == ("string", "decimal")
