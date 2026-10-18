"""``/v1/watchlist``, against a real Postgres: three lists, in the order added, per user.

BE-4 in docs/be-requirements-timeline-account.md: every list always present,
writes idempotent and answering the whole list, a ref that names nothing a
404, and an insider shown as their latest Form 4 gives them.
"""

from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filer, Filing, Insider, InsiderTransaction, ReportingOwner, Security
from tests.integration.accounts import FRONTEND, make_user, sign_in

AAPL = "037833100"
NVDA = "67066G104"
OTHER = "11111A101"


async def _world(session: AsyncSession) -> None:
    """Two stocks, two investors, and an insider who filed twice: as a director
    of another issuer, then as NVIDIA's CEO, latest."""
    ids = {}
    for cusip, ticker, name in (
        (AAPL, "AAPL", "APPLE INC"),
        (NVDA, "NVDA", "NVIDIA CORP"),
        (OTHER, "OTHR", "OTHER CO"),
    ):
        ids[cusip] = await session.scalar(
            insert(Security)
            .values(cusip=cusip, ticker=ticker, name=name, resolution_source="manual")
            .returning(Security.id)
        )
    for slug, display in (("berkshire-hathaway", "Berkshire Hathaway"), ("pershing-square", None)):
        await session.execute(
            insert(Filer).values(name=slug.upper(), slug=slug, display_name=display)
        )
    insider_id = await session.scalar(
        insert(Insider).values(cik="0001197647", name="HUANG JEN HSUN").returning(Insider.id)
    )
    filings: list[tuple[datetime, str, int | None, dict[str, Any]]] = [
        (datetime(2026, 1, 5, tzinfo=UTC), "OTHER CO", ids[OTHER], {"is_director": True}),
        (
            datetime(2026, 9, 4, 21, tzinfo=UTC),
            "NVIDIA CORP",
            ids[NVDA],
            {"is_officer": True, "is_director": True, "officer_title": "President and CEO"},
        ),
    ]
    for n, (filed, issuer, security_id, roles) in enumerate(filings, start=1):
        filing_id = await session.scalar(
            insert(Filing)
            .values(
                accession_no=f"0001197647-26-00000{n}",
                cik="0001045810",
                form_type="4",
                filed_at=filed,
                value_multiplier=1,
                parse_status="ok",
            )
            .returning(Filing.id)
        )
        await session.execute(
            insert(ReportingOwner).values(filing_id=filing_id, insider_id=insider_id, **roles)
        )
        await session.execute(
            insert(InsiderTransaction).values(
                filing_id=filing_id,
                insider_id=insider_id,
                line_number=1,
                issuer_cik="0001045810",
                issuer_name=issuer,
                security_id=security_id,
                transaction_date=filed.date(),
                transaction_code="S",
                acquired_disposed="D",
                is_derivative=False,
            )
        )


async def _put(client: AsyncClient, ref: dict[str, str]) -> Response:
    return await client.put("/v1/watchlist/items", json={"ref": ref}, headers=FRONTEND)


async def _delete(client: AsyncClient, ref: dict[str, str]) -> Response:
    return await client.request(
        "DELETE", "/v1/watchlist/items", json={"ref": ref}, headers=FRONTEND
    )


async def test_nothing_followed_is_three_empty_lists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    response = await client.get("/v1/watchlist")

    assert response.status_code == 200
    assert response.json() == {"securities": [], "investors": [], "insiders": []}
    assert response.headers["cache-control"] == "no-store"


async def test_signed_out_is_a_401(client: AsyncClient) -> None:
    assert (await client.get("/v1/watchlist")).status_code == 401


async def test_each_list_keeps_the_order_items_were_added(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _world(db_session)
    await make_user(db_session)
    await sign_in(client)

    await _put(client, {"kind": "stock", "cusip": NVDA.lower()})
    await _put(client, {"kind": "investor", "slug": "pershing-square"})
    await _put(client, {"kind": "stock", "cusip": AAPL})
    response = await _put(client, {"kind": "investor", "slug": "berkshire-hathaway"})

    body = response.json()
    assert [s["ticker"] for s in body["securities"]] == ["NVDA", "AAPL"]
    assert [i["slug"] for i in body["investors"]] == ["pershing-square", "berkshire-hathaway"]
    assert body["investors"][0]["display_name"] == "PERSHING-SQUARE"
    assert body["insiders"] == []
    assert (await client.get("/v1/watchlist")).json() == body


async def test_an_insider_is_shown_as_their_latest_filing_gives_them(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _world(db_session)
    await make_user(db_session)
    await sign_in(client)

    response = await _put(client, {"kind": "insider", "insider_cik": "1197647"})

    assert response.json()["insiders"] == [
        {
            "insider_cik": "0001197647",
            "insider_name": "HUANG JEN HSUN",
            "roles": ["director", "officer"],
            "officer_title": "President and CEO",
            "issuer_name": "NVIDIA CORP",
            "current_ticker": "NVDA",
        }
    ]


async def test_writes_are_idempotent(client: AsyncClient, db_session: AsyncSession) -> None:
    await _world(db_session)
    await make_user(db_session)
    await sign_in(client)

    await _put(client, {"kind": "stock", "cusip": AAPL})
    twice = await _put(client, {"kind": "stock", "cusip": AAPL})
    removed = await _delete(client, {"kind": "stock", "cusip": AAPL})
    removed_again = await _delete(client, {"kind": "stock", "cusip": AAPL})

    assert len(twice.json()["securities"]) == 1
    assert removed.status_code == removed_again.status_code == 200
    assert removed_again.json()["securities"] == []


async def test_each_user_has_their_own(client: AsyncClient, db_session: AsyncSession) -> None:
    await _world(db_session)
    await make_user(db_session)
    await make_user(db_session, "grace@example.com", name="Grace Hopper")
    await sign_in(client)
    await _put(client, {"kind": "stock", "cusip": AAPL})

    await sign_in(client, "grace@example.com")

    assert (await client.get("/v1/watchlist")).json()["securities"] == []


async def test_a_ref_that_names_nothing_is_a_404_either_way(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _world(db_session)
    await make_user(db_session)
    await sign_in(client)

    for ref in (
        {"kind": "stock", "cusip": "999999999"},
        {"kind": "investor", "slug": "nobody"},
        {"kind": "insider", "insider_cik": "42"},
    ):
        assert (await _put(client, ref)).status_code == 404, ref
        assert (await _delete(client, ref)).status_code == 404, ref


async def test_a_malformed_ref_is_a_422_naming_the_field(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session)
    await sign_in(client)

    cusip = await _put(client, {"kind": "stock", "cusip": "AAPL"})
    kind = await _put(client, {"kind": "fund", "slug": "x"})

    assert cusip.status_code == kind.status_code == 422
    assert cusip.json()["error"]["detail"]["errors"][0]["loc"] == ["body", "ref", "cusip"]


async def test_a_ticker_is_not_a_ref(client: AsyncClient, db_session: AsyncSession) -> None:
    """Tickers change; the old browser-only lists' ticker entries stay in the browser."""
    await make_user(db_session)
    await sign_in(client)

    response = await _put(client, {"kind": "stock", "ticker": "AAPL"})

    assert response.status_code == 422
