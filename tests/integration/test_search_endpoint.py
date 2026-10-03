"""``/v1/search``, against a real Postgres with ``pg_trgm``.

The ranking is the contract: an exact ticker before anything, prefixes before
anything merely alike, and the most widely held first among equals. Each test
makes the wrong order the tempting one, usually by giving the row that should
lose more dollars than the row that should win.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filer, FilerCik, Filing, Holding, Security
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views

Q1 = date(2024, 3, 31)

_ciks = count(1)
_accessions = count(1)
_cusips = count(1)


async def _fund(
    session: AsyncSession,
    slug: str,
    *,
    display_name: str | None = None,
    manager_name: str | None = None,
) -> int:
    filer_id = await session.scalar(
        insert(Filer)
        .values(name=slug.upper(), slug=slug, display_name=display_name, manager_name=manager_name)
        .returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=0)
    )
    return filer_id


async def _stock(session: AsyncSession, name: str, *, ticker: str | None = None) -> str:
    """A security named ``name``, and its new CUSIP."""
    cusip = f"{next(_cusips):05d}X101"
    await session.execute(insert(Security).values(cusip=cusip, name=name, ticker=ticker))
    return cusip


async def _holds(session: AsyncSession, filer_id: int, **dollars: int) -> None:
    """A 13F-HR for Q1 holding each CUSIP for that many dollars, at $10 a share."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-24-{next(_accessions):06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR",
            period_of_report=Q1,
            filed_at=datetime(2024, 3, 31, 16, tzinfo=UTC) + timedelta(days=45),
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    for cusip, value in dollars.items():
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=select(Security.id).where(Security.cusip == cusip).scalar_subquery(),
                filer_id=filer_id,
                period_of_report=Q1,
                cusip=cusip,
                value_usd=Decimal(value),
                shares=Decimal(value) / 10,
                sshprnamt_type="SH",
            )
        )


async def _publish(session: AsyncSession) -> None:
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _search(client: AsyncClient, q: str, **params: Any) -> Any:
    response = await client.get("/v1/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


async def _stocks(client: AsyncClient, q: str, **params: Any) -> list[str]:
    """The issuer names found, in order."""
    return [s["issuer_name"] for s in (await _search(client, q, **params))["securities"]]


async def _investors(client: AsyncClient, q: str, **params: Any) -> list[str]:
    return [i["slug"] for i in (await _search(client, q, **params))["investors"]]


# --- the order ------------------------------------------------------------------


async def test_an_exact_ticker_is_first_then_ticker_prefixes_then_name_prefixes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Each below the one before it however much more of it is held."""
    exact = await _stock(db_session, "PLATFORMS INC", ticker="META")
    ticker_prefix = await _stock(db_session, "METALLURGY INC", ticker="METAV")
    name_prefix = await _stock(db_session, "META MATERIALS INC", ticker="MMAT")
    fund = await _fund(db_session, "fund")
    await _holds(db_session, fund, **{exact: 1_000, ticker_prefix: 50_000, name_prefix: 900_000})
    await _publish(db_session)

    assert await _stocks(client, "meta") == [
        "PLATFORMS INC",
        "METALLURGY INC",
        "META MATERIALS INC",
    ]


async def test_the_exact_ticker_is_first_in_any_case(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _stock(db_session, "ALPHABET INC", ticker="GOOGL")
    await _stock(db_session, "GOOG ETF", ticker="GOOGX")

    for q in ("googl", "GOOGL", "Googl"):
        assert (await _stocks(client, q))[0] == "ALPHABET INC", q


async def test_a_name_prefix_is_above_a_closer_and_more_held_fuzzy_match(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``APPLE`` is a whole word of APPLE INC, the most alike a name can be,
    and still below a name that merely starts with ``apple``."""
    prefix = await _stock(db_session, "APPLESEED HOLDINGS")
    fuzzy = await _stock(db_session, "THE APPLE INC")
    fund = await _fund(db_session, "fund")
    await _holds(db_session, fund, **{prefix: 1, fuzzy: 1_000_000})
    await _publish(db_session)

    assert await _stocks(client, "apple") == ["APPLESEED HOLDINGS", "THE APPLE INC"]


async def test_among_equals_the_most_dollars_held_is_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Three names starting with ``apple``, and one nobody holds."""
    small, large, _nobody, medium = [
        await _stock(db_session, name)
        for name in ("APPLE HOSPITALITY", "APPLE INC", "APPLE INC OPTIONS", "APPLE RUSH")
    ]
    fund = await _fund(db_session, "fund")
    await _holds(db_session, fund, **{small: 10, large: 1_000, medium: 100})
    await _publish(db_session)

    assert await _stocks(client, "apple") == [
        "APPLE INC",
        "APPLE RUSH",
        "APPLE HOSPITALITY",
        "APPLE INC OPTIONS",
    ]


async def test_investors_are_ranked_by_name_prefix_then_by_portfolio(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    small = await _fund(db_session, "capital-small", display_name="Capital Small")
    large = await _fund(db_session, "capital-large", display_name="Capital Large")
    await _fund(db_session, "greenlight", display_name="Greenlight Capital")
    stock = await _stock(db_session, "ANYTHING INC")
    await _holds(db_session, small, **{stock: 10})
    await _holds(db_session, large, **{stock: 1_000})
    await _publish(db_session)

    assert await _investors(client, "capital") == ["capital-large", "capital-small", "greenlight"]


# --- what is alike --------------------------------------------------------------


async def test_a_misspelling_finds_the_name_it_misspells_first(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``word_similarity`` alone scores these three the same."""
    for name in ("ALLEGRO MICROSYSTEMS INC", "MICROSTRATEGY INC", "MICROSOFT CORP"):
        await _stock(db_session, name)

    assert (await _stocks(client, "microsft"))[0] == "MICROSOFT CORP"


async def test_a_half_typed_word_finds_the_word_it_starts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``strict_word_similarity`` alone prefers the shorter word, HAT."""
    await _stock(db_session, "BLUE HAT INTERACTIVE")
    await _stock(db_session, "BERKSHIRE HATHAWAY INC")
    await _fund(db_session, "berkshire-hathaway", display_name="Berkshire Hathaway")

    assert (await _stocks(client, "hath"))[0] == "BERKSHIRE HATHAWAY INC"
    assert await _investors(client, "hath") == ["berkshire-hathaway"]


async def test_a_word_inside_a_long_name_finds_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Whole-name ``similarity`` is 0.20 here, under its threshold."""
    await _fund(
        db_session,
        "pershing-square",
        display_name="Pershing Square Capital Management",
        manager_name="Bill Ackman",
    )

    assert await _investors(client, "square") == ["pershing-square"]


async def test_an_investor_is_found_by_its_managers_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _fund(db_session, "berkshire", display_name="Berkshire", manager_name="Warren Buffett")
    await _fund(db_session, "pershing", display_name="Pershing", manager_name="Bill Ackman")

    assert await _investors(client, "buffett") == ["berkshire"]
    assert await _investors(client, "bill") == ["pershing"]
    assert await _investors(client, "ackmann") == ["pershing"]


async def test_whole_words_are_ranked_like_any_fuzzy_match(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A whole word scores 70 by a regular expression rather than by trigrams.
    It has to be the same 70, so the dollars decide, and whatever the spacing."""
    less, more = await _stock(db_session, "ALPHA  CORP"), await _stock(db_session, "BETA CORP")
    await _stock(db_session, "CORPS OF GAMMA")
    fund = await _fund(db_session, "fund")
    await _holds(db_session, fund, **{less: 10, more: 1_000})
    await _publish(db_session)

    assert await _stocks(client, "corp") == ["CORPS OF GAMMA", "BETA CORP", "ALPHA  CORP"]
    assert await _stocks(client, "alpha corp") == ["ALPHA  CORP"]


async def test_nothing_alike_is_nothing_found(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _stock(db_session, "APPLE INC", ticker="AAPL")
    await _fund(db_session, "berkshire", display_name="Berkshire Hathaway")

    assert await _search(client, "zzzzqqq") == {
        "query": "zzzzqqq",
        "investors": [],
        "securities": [],
    }


# --- the query ------------------------------------------------------------------


async def test_two_characters_match_prefixes_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``ab`` has three trigrams, two of them the padding every ``a`` word
    shares: alike nearly everything, so it is not compared."""
    await _stock(db_session, "ABBVIE INC", ticker="ABBV")
    await _stock(db_session, "CRAB HOLDINGS")
    await _stock(db_session, "GRAB INC", ticker="GRAB")

    assert await _stocks(client, "ab") == ["ABBVIE INC"]


async def test_wildcards_are_matched_literally(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _stock(db_session, "ANYTHING INC")
    await _stock(db_session, "100% GOLD TRUST")

    assert await _stocks(client, "%%") == []
    assert await _stocks(client, "__") == []
    assert await _stocks(client, "100%") == ["100% GOLD TRUST"]


async def test_regular_expression_characters_are_harmless(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _stock(db_session, "C++ SOFTWARE INC")

    # Three characters or more, or no regular expression is built.
    for q in ("c++", "(inc", "ab\\", "[ab", "x{2,}", "x.*", "\\m\\M"):
        await _search(client, q)


async def test_q_is_trimmed_and_must_then_be_two_characters(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _stock(db_session, "ABBVIE INC")

    assert (await _search(client, "  abbvie  "))["query"] == "abbvie"
    assert await _stocks(client, "  abbvie  ") == ["ABBVIE INC"]
    for q in ("", "a", "  a  "):
        response = await client.get("/v1/search", params={"q": q})
        assert response.status_code == 422, q
    assert (await client.get("/v1/search")).status_code == 422


@pytest.mark.parametrize(("limit", "status"), [(0, 422), (1, 200), (20, 200), (21, 422)])
async def test_limit_is_refused_out_of_range(client: AsyncClient, limit: int, status: int) -> None:
    response = await client.get("/v1/search", params={"q": "abc", "limit": limit})
    assert response.status_code == status


async def test_limit_applies_to_each_group_and_fills_from_the_fuzzy_matches(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Three prefix matches and two fuzzy ones, for a limit of four: the
    three, then the better of the two, and none of them twice."""
    for name in ("ORACLE CORP", "ORACLES INC", "THE ORACLE FUND", "ORACLE", "BIG ORACLE GROUP"):
        await _stock(db_session, name)
    for n in range(6):
        await _fund(db_session, f"oracle-{n}", display_name=f"Oracle Partners {n}")

    found = await _search(client, "oracle", limit=4)
    assert len(found["investors"]) == 4
    names = [s["issuer_name"] for s in found["securities"]]
    assert len(names) == len(set(names)) == 4
    assert set(names[:3]) == {"ORACLE CORP", "ORACLES INC", "ORACLE"}


async def test_the_default_limit_is_five(client: AsyncClient, db_session: AsyncSession) -> None:
    for n in range(7):
        await _stock(db_session, f"ZETA {n} INC")

    assert len(await _stocks(client, "zeta")) == 5


async def test_a_stock_without_a_ticker_is_returned_by_its_cusip(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    cusip = await _stock(db_session, "UNRESOLVED INC")

    assert (await _search(client, "unresolved"))["securities"] == [
        {"cusip": cusip, "ticker": None, "issuer_name": "UNRESOLVED INC"}
    ]


async def test_an_investor_row_has_what_a_palette_shows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    filer = await _fund(
        db_session,
        "berkshire-hathaway",
        display_name="Berkshire Hathaway",
        manager_name="Warren Buffett",
    )
    await db_session.execute(update(Filer).where(Filer.id == filer).values(category="value"))

    assert (await _search(client, "berkshire"))["investors"] == [
        {
            "slug": "berkshire-hathaway",
            "display_name": "Berkshire Hathaway",
            "manager_name": "Warren Buffett",
            "category": "value",
        }
    ]


# --- the database ---------------------------------------------------------------


async def test_the_trigram_functions_are_costed_so_the_index_is_used(
    db_session: AsyncSession,
) -> None:
    """0020's ``COST 100``. At the extension's ``COST 1``, the planner reads
    all of ``security`` rather than ask the index, 40 times slower on the dev
    database. ``pg_dump`` does not keep it, so a restored database fails here."""
    rows = await db_session.execute(
        text(
            "SELECT proname, procost FROM pg_proc WHERE proname IN "
            "('word_similarity_commutator_op', 'strict_word_similarity_commutator_op', "
            "'word_similarity', 'strict_word_similarity')"
        )
    )
    costs = {name: cost for name, cost in rows}

    assert costs == {
        "word_similarity_commutator_op": 100,
        "strict_word_similarity_commutator_op": 100,
        "word_similarity": 100,
        "strict_word_similarity": 100,
    }
