"""``/v1/timeline`` and ``/v1/timeline/upcoming``, against a real Postgres.

The rules are the frontend's ``buildTimeline()`` (whalewatch-web,
``src/modules/Timeline/buildTimeline.ts``), and each test here is one of its
rules on data small enough to check by hand: every 13F position at $10 a share,
so a traded value is the shares moved times ten.

What a wrong reading would get wrong without failing: a holder promoted into
the top four because a larger move aged out of the window; a hold counted as a
move; an amended quarter counted twice or dated by its superseded original; a
grant at a followed stock; one line twice when its insider and its stock are
both followed; a Form 4 placed on the wrong day by the wrong zone.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.timeline import current_quarter
from app.core.periods import quarter_label, quarters_ending
from app.db.models import (
    AmendmentKind,
    Filer,
    FilerCik,
    Filing,
    Holding,
    Insider,
    InsiderTransaction,
    ReportingOwner,
    SecCompanyTicker,
    Security,
)
from app.derived.recompute import recompute
from app.derived.scope import EVERYTHING
from app.derived.views import refresh_views
from tests.integration.accounts import make_user, sign_in

Q1 = date(2026, 3, 31)
Q2 = date(2026, 6, 30)
X = "11111A101"  # the followed stock
Z = "22222B202"  # held, never followed

WINDOW = {"from": "2026-07-15", "to": "2026-08-31", "tz": "UTC"}

_ciks = count(1)
_accessions = count(1)


def at(day: str, hour: int = 15, minute: int = 0) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=UTC)


# --- 13F ------------------------------------------------------------------------


async def _fund(session: AsyncSession, slug: str) -> int:
    filer_id = await session.scalar(
        insert(Filer).values(name=slug.upper(), slug=slug).returning(Filer.id)
    )
    assert filer_id is not None
    await session.execute(
        insert(FilerCik).values(filer_id=filer_id, cik=f"{next(_ciks):010d}", priority=0)
    )
    return filer_id


async def _security(session: AsyncSession, cusip: str, ticker: str | None = None) -> int:
    await session.execute(
        pg_insert(Security)
        .values(
            cusip=cusip,
            name=f"ISSUER {cusip}",
            ticker=ticker,
            resolution_source=None if ticker is None else "manual",
        )
        .on_conflict_do_nothing(index_elements=[Security.cusip])
    )
    security_id = await session.scalar(select(Security.id).where(Security.cusip == cusip))
    assert security_id is not None
    return security_id


async def _filing(
    session: AsyncSession,
    filer_id: int,
    period: date,
    positions: dict[str, int],
    filed_at: datetime,
    *,
    restates: bool = False,
) -> str:
    """A 13F-HR (or a restating 13F-HR/A) for ``period``: ``positions`` are
    CUSIP to shares, at $10 a share. Its accession number."""
    cik = await session.scalar(select(FilerCik.cik).where(FilerCik.filer_id == filer_id))
    assert cik is not None
    accession_no = f"{cik}-{period:%y}-{next(_accessions):06d}"
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=accession_no,
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR/A" if restates else "13F-HR",
            amendment_kind=AmendmentKind.RESTATEMENT if restates else None,
            amendment_no=1 if restates else None,
            period_of_report=period,
            filed_at=filed_at,
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    for cusip, shares in positions.items():
        security_id = await _security(session, cusip, ticker="XXX" if cusip == X else None)
        await session.execute(
            insert(Holding).values(
                filing_id=filing_id,
                security_id=security_id,
                filer_id=filer_id,
                period_of_report=period,
                cusip=cusip,
                value_usd=Decimal(shares) * 10,
                shares=Decimal(shares),
                sshprnamt_type="SH",
            )
        )
    return accession_no


async def _publish(session: AsyncSession) -> None:
    await recompute(session, EVERYTHING)
    await refresh_views(session)


async def _a_quarter_of_holders(session: AsyncSession) -> dict[str, str]:
    """Seven holders of X from Q1 (1,000 shares each, filed in May), and what
    each did in Q2. Their Q2 accession numbers, by slug.

    Q2, by traded value: alpha exits (10,000, filed before the window), bravo
    adds 800 (8,000, its original restated), charlie trims 600 (6,000), delta
    adds 500 and echo trims 500 (5,000 each: delta wins the tie by slug),
    foxtrot adds 300 (3,000), golf holds. golf also adds 200 of Z.
    """
    q2 = {
        "alpha": ({Z: 100}, at("2026-07-10")),
        "bravo": ({X: 1_100}, at("2026-08-01")),
        "charlie": ({X: 400}, at("2026-08-05")),
        "delta": ({X: 1_500}, at("2026-08-14", 20)),
        "echo": ({X: 500}, at("2026-08-10")),
        "foxtrot": ({X: 1_300}, at("2026-08-11")),
        "golf": ({X: 1_000, Z: 700}, at("2026-08-12")),
    }
    accessions = {}
    for slug, (positions, filed_at) in q2.items():
        filer_id = await _fund(session, slug)
        q1 = {X: 1_000} | ({Z: 500} if slug == "golf" else {})
        if slug == "alpha":
            q1 |= {Z: 100}
        await _filing(session, filer_id, Q1, q1, at("2026-05-15"))
        accessions[slug] = await _filing(session, filer_id, Q2, positions, filed_at)
        if slug == "bravo":
            accessions["bravo-original"] = accessions[slug]
            accessions[slug] = await _filing(
                session, filer_id, Q2, {X: 1_800}, at("2026-08-20"), restates=True
            )
    return accessions


async def _timeline(client: AsyncClient, *refs: str, **window: str) -> list[dict[str, Any]]:
    response = await client.get("/v1/timeline", params={**WINDOW, **window, "ref": list(refs)})
    assert response.status_code == 200, response.text
    data: list[dict[str, Any]] = response.json()["data"]
    return data


async def test_a_followed_stock_shows_each_quarters_four_largest_moves_cut_before_the_window(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _a_quarter_of_holders(db_session)
    await _publish(db_session)

    events = await _timeline(client, f"stock:{X}")

    assert [(e["investor"]["slug"], e["change"]["action"], e["value_usd"]) for e in events] == [
        # Newest first. alpha is in the top four but filed before the window,
        # and echo, fifth, does not take its place.
        ("bravo", "add", "8000.00"),
        ("delta", "add", "5000.00"),
        ("charlie", "trim", "-6000.00"),
    ]
    bravo = events[0]
    assert bravo["kind"] == "holder_change"
    assert bravo["source"] == "13f"
    assert bravo["ref"] == {"kind": "stock", "cusip": X}
    # The restatement's: the filing the figures come from, once.
    assert bravo["id"] == f"{accessions['bravo']}:{X}"
    assert bravo["accession_no"] == accessions["bravo"]
    assert bravo["at"] == "2026-08-20T15:00:00Z"
    assert (bravo["period"], bravo["as_of"], bravo["lag_days"]) == ("2026Q2", "2026-06-30", 51)
    assert bravo["side"] == "acquire"
    assert bravo["change"] == {
        "cusip": X,
        "ticker": "XXX",
        "current_ticker": "XXX",
        "issuer_name": f"ISSUER {X}",
        "action": "add",
        "shares_delta": "800.0000",
        "shares_delta_pct": "80.000000",
        "traded_value_usd": "8000.00",
    }
    assert events[2]["side"] == "dispose"


async def test_an_exits_shares_are_the_whole_previous_position(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_quarter_of_holders(db_session)
    await _publish(db_session)

    [alpha] = [
        e
        for e in await _timeline(client, f"stock:{X}", **{"from": "2026-07-01"})
        if e["investor"]["slug"] == "alpha"
    ]

    assert alpha["change"]["action"] == "exit"
    assert alpha["change"]["shares_delta"] == "-1000.0000"
    assert alpha["value_usd"] == "-10000.00"


async def test_a_followed_investor_shows_one_summary_per_filing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _a_quarter_of_holders(db_session)
    newcomer = await _fund(db_session, "newcomer")
    newcomer_q2 = await _filing(db_session, newcomer, Q2, {Z: 10}, at("2026-08-02"))
    await _publish(db_session)

    events = await _timeline(client, "investor:golf", "investor:newcomer")

    golf, first = events
    assert golf["kind"] == "filing_summary"
    assert golf["ref"] == {"kind": "investor", "slug": "golf"}
    assert golf["id"] == golf["accession_no"] == accessions["golf"]
    # X held (not a move), Z added 200 at $10.
    assert golf["counts"] == {"new": 0, "add": 1, "trim": 0, "exit": 0}
    assert (golf["value_usd"], golf["side"]) == ("2000.00", "acquire")
    assert golf["position_count"] == 2
    assert golf["portfolio_value_usd"] == "17000.00"
    assert golf["largest_change"]["cusip"] == Z
    assert golf["largest_change"]["traded_value_usd"] == "2000.00"
    # Its first quarter: every position new only to us, so nothing counted.
    assert first["id"] == newcomer_q2
    assert first["counts"] == {"new": 0, "add": 0, "trim": 0, "exit": 0}
    assert (first["value_usd"], first["side"], first["largest_change"]) == ("0.00", "neutral", None)


async def test_a_followed_investor_and_a_top_mover_are_two_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """They answer different follows."""
    await _a_quarter_of_holders(db_session)
    await _publish(db_session)

    events = await _timeline(client, f"stock:{X}", "investor:charlie")

    assert sorted(e["kind"] for e in events if e["investor"]["slug"] == "charlie") == [
        "filing_summary",
        "holder_change",
    ]


async def test_nothing_in_the_window_is_an_empty_list(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _a_quarter_of_holders(db_session)
    await _publish(db_session)

    assert await _timeline(client, f"stock:{X}", **{"from": "2026-09-01", "to": "2026-09-30"}) == []
    assert await _timeline(client) == []


# --- Form 4 ---------------------------------------------------------------------


async def _insider(session: AsyncSession, cik: str, name: str) -> int:
    insider_id = await session.scalar(
        insert(Insider).values(cik=cik, name=name).returning(Insider.id)
    )
    assert insider_id is not None
    return insider_id


async def _form4(
    session: AsyncSession,
    insider_id: int,
    filed_at: datetime,
    *lines: tuple[str, int | None, int, str | None],
    issuer_cik: str = "0000000999",
) -> str:
    """A Form 4 filed at ``filed_at``: lines of (code, security id, shares, price)."""
    accession_no = f"0009999999-26-{next(_accessions):06d}"
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=accession_no,
            cik=issuer_cik,
            form_type="4",
            filed_at=filed_at,
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    await session.execute(
        insert(ReportingOwner).values(
            filing_id=filing_id, insider_id=insider_id, is_officer=True, officer_title="CEO"
        )
    )
    for number, (code, security_id, shares, price) in enumerate(lines, start=1):
        await session.execute(
            insert(InsiderTransaction).values(
                filing_id=filing_id,
                insider_id=insider_id,
                line_number=number,
                issuer_cik=issuer_cik,
                issuer_name="ISSUER X",
                security_id=security_id,
                transaction_date=filed_at.date() - timedelta(days=2),
                transaction_code=code,
                acquired_disposed="A" if code in ("P", "A", "M") else "D",
                is_derivative=False,
                shares=Decimal(shares),
                price_per_share=None if price is None else Decimal(price),
            )
        )
    return accession_no


async def _two_insiders(session: AsyncSession) -> dict[str, str]:
    """The CEO files a sale, a tax withholding and a grant at X; a director
    files a purchase and an option exercise at X, and a sale no security could
    be linked to."""
    x = await _security(session, X, ticker="XXX")
    ceo = await _insider(session, "0000000101", "CEO PERSON")
    director = await _insider(session, "0000000102", "DIRECTOR PERSON")
    return {
        "ceo": await _form4(
            session,
            ceo,
            at("2026-08-03", 14, 32),
            ("S", x, 100, "10"),
            ("F", x, 50, "10"),
            ("A", None, 1_000, None),
        ),
        "director": await _form4(
            session,
            director,
            at("2026-08-04"),
            ("P", x, 200, "10"),
            ("M", x, 300, "4"),
            ("S", None, 10, "10"),
        ),
    }


async def test_a_followed_insider_shows_every_code(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _two_insiders(db_session)

    events = await _timeline(client, "insider:101")

    assert [(e["id"], e["trade"]["code"], e["side"], e["value_usd"]) for e in events] == [
        (f"{accessions['ceo']}:1", "S", "dispose", "-1000.00"),
        # Withheld for tax: disposed, so negative, but neither bought nor sold.
        (f"{accessions['ceo']}:2", "F", "neutral", "-500.00"),
        # A grant with no price: a real state, not an error.
        (f"{accessions['ceo']}:3", "A", "acquire", None),
    ]
    assert {e["ref"]["kind"] for e in events} == {"insider"}
    assert events[0]["ref"] == {"kind": "insider", "insider_cik": "0000000101"}
    assert events[0]["at"] == "2026-08-03T14:32:00Z"
    assert events[0]["kind"] == "insider_trade"
    assert events[0]["trade"]["insider_name"] == "CEO PERSON"


async def test_a_followed_stock_shows_only_open_market_lines_linked_to_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _two_insiders(db_session)

    events = await _timeline(client, f"stock:{X}")

    assert [(e["id"], e["trade"]["code"], e["ref"]) for e in events] == [
        (f"{accessions['director']}:1", "P", {"kind": "stock", "cusip": X}),
        (f"{accessions['ceo']}:1", "S", {"kind": "stock", "cusip": X}),
    ]
    assert events[0]["value_usd"] == "2000.00"


async def test_an_insider_and_their_stock_both_followed_is_one_event_under_the_insider(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _two_insiders(db_session)

    events = await _timeline(client, f"stock:{X}", "insider:0000000101")

    ceo_sale = [e for e in events if e["id"] == f"{accessions['ceo']}:1"]
    assert len(ceo_sale) == 1
    assert ceo_sale[0]["ref"] == {"kind": "insider", "insider_cik": "0000000101"}
    assert len(events) == 4  # the CEO's three, and the director's purchase


LENNAR = "0000920760"
LEN = "526057104"
LENB = "526057302"


async def _lennar(session: AsyncSession) -> dict[str, str]:
    """Two classes under one CIK, as SEC lists them (LEN and LEN-B). A director
    files a sale and a gift the loader could not link to either class, and a
    sale linked to LEN. Another issuer files an unlinked sale too."""
    len_a = await _security(session, LEN, ticker="LEN")
    await _security(session, LENB, ticker="LEN.B")
    for ticker in ("LEN", "LEN-B"):
        await session.execute(
            insert(SecCompanyTicker).values(
                ticker=ticker, cik=LENNAR, name="LENNAR CORP", seen_at=at("2026-08-01")
            )
        )
    director = await _insider(session, "0000000201", "LENNAR DIRECTOR")
    stranger = await _insider(session, "0000000202", "SOMEONE ELSE")
    return {
        "unlinked": await _form4(
            session,
            director,
            at("2026-08-06"),
            ("S", None, 100, "10"),
            ("G", None, 10, None),
            issuer_cik=LENNAR,
        ),
        "linked": await _form4(
            session, director, at("2026-08-05"), ("S", len_a, 50, "10"), issuer_cik=LENNAR
        ),
        "elsewhere": await _form4(
            session, stranger, at("2026-08-07"), ("S", None, 5, "10"), issuer_cik="0000000303"
        ),
    }


async def test_a_followed_stock_shows_its_issuers_unlinked_open_market_lines(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Q6: the line is under the stock its issuer is, with no cusip on the
    trade to say the class is not known. As /v1/stocks/{ticker}/insiders has it."""
    accessions = await _lennar(db_session)

    events = await _timeline(client, f"stock:{LEN}")

    assert [(e["id"], e["ref"]["cusip"], e["trade"]["cusip"]) for e in events] == [
        (f"{accessions['unlinked']}:1", LEN, None),
        (f"{accessions['linked']}:1", LEN, LEN),
    ]


async def test_an_unlinked_line_is_also_under_the_issuers_other_class(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _lennar(db_session)

    events = await _timeline(client, f"stock:{LENB}")

    assert [(e["id"], e["ref"]["cusip"]) for e in events] == [(f"{accessions['unlinked']}:1", LENB)]


async def test_both_classes_followed_is_one_event_under_the_first_named(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    accessions = await _lennar(db_session)

    events = await _timeline(client, f"stock:{LENB}", f"stock:{LEN}")

    unlinked = [e for e in events if e["id"] == f"{accessions['unlinked']}:1"]
    assert [e["ref"]["cusip"] for e in unlinked] == [LENB]
    assert len(events) == 2


async def test_the_window_is_days_in_the_zone_asked_for(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """02:00 UTC on 1 September is 22:00 on 31 August in New York."""
    x = await _security(db_session, X, ticker="XXX")
    ceo = await _insider(db_session, "0000000101", "CEO PERSON")
    await _form4(db_session, ceo, at("2026-09-01", 2), ("S", x, 1, "10"))

    new_york = await _timeline(client, "insider:101", tz="America/New_York")
    utc = await _timeline(client, "insider:101", tz="UTC")

    assert len(new_york) == 1
    assert utc == []


async def test_a_window_shorter_than_two_weeks_is_answered_as_asked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _two_insiders(db_session)

    events = await _timeline(client, "insider:101", **{"from": "2026-08-04", "to": "2026-08-10"})

    assert events == []


async def test_without_tz_the_signed_in_users_zone_counts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_user(db_session, time_zone="Asia/Tokyo")
    await sign_in(client)

    signed_in = await client.get("/v1/timeline", params={"from": "2026-08-01", "to": "2026-08-31"})
    await client.post("/v1/auth/logout", headers={"origin": "http://localhost:5173"})
    signed_out = await client.get("/v1/timeline", params={"from": "2026-08-01", "to": "2026-08-31"})

    assert signed_in.json()["meta"]["tz"] == "Asia/Tokyo"
    assert signed_out.json()["meta"]["tz"] == "America/New_York"


async def test_ties_are_by_id_newest_first(client: AsyncClient, db_session: AsyncSession) -> None:
    accessions = await _two_insiders(db_session)

    events = await _timeline(client, "insider:101", "insider:102")

    assert [e["id"] for e in events] == [
        f"{accessions['director']}:1",
        f"{accessions['director']}:2",
        f"{accessions['director']}:3",
        f"{accessions['ceo']}:1",
        f"{accessions['ceo']}:2",
        f"{accessions['ceo']}:3",
    ]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"from": "2026-02-30", "to": "2026-03-01"}, "from"),
        ({"from": "2026-08-31", "to": "2026-08-01"}, "from"),
        ({"from": "2026-04-08", "to": "2026-08-31"}, "from"),
        ({"from": "2026-08-01", "to": "2026-08-31", "tz": "Mars/Olympus_Mons"}, "tz"),
        ({"from": "2026-08-01", "to": "2026-08-31", "ref": "ticker:AAPL"}, "ref"),
        ({"from": "2026-08-01", "to": "2026-08-31", "ref": "stock:AAPL"}, "ref"),
    ],
    ids=["not a date", "backwards", "over 120 days", "not a zone", "not a kind", "not a cusip"],
)
async def test_a_window_or_ref_that_cannot_be_answered_is_a_422(
    client: AsyncClient, params: dict[str, str], field: str
) -> None:
    response = await client.get("/v1/timeline", params=params)

    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["error"]["detail"]["errors"]] == [["query", field]]


async def test_120_days_both_ends_included_is_allowed(client: AsyncClient) -> None:
    response = await client.get("/v1/timeline", params={"from": "2026-05-04", "to": "2026-08-31"})

    assert response.status_code == 200


# --- upcoming -------------------------------------------------------------------


async def _filing_season(session: AsyncSession) -> tuple[date, date]:
    """Today's current quarter and the one before. hotel has filed the current
    quarter; india (the largest holder of X) and juliet have not."""
    current = current_quarter(date.today())
    prior = quarters_ending(current, 2)[0]
    hotel, india, juliet = [await _fund(session, slug) for slug in ("hotel", "india", "juliet")]
    await _filing(session, hotel, prior, {X: 100}, at(str(prior + timedelta(days=40))))
    await _filing(session, india, prior, {X: 900}, at(str(prior + timedelta(days=40))))
    await _filing(session, juliet, prior, {X: 300}, at(str(prior + timedelta(days=40))))
    await _filing(session, hotel, current, {X: 100}, at(str(current + timedelta(days=5))))
    await _publish(session)
    return current, prior


async def test_upcoming_lists_what_is_still_due_in_the_order_sent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    current, _ = await _filing_season(db_session)

    response = await client.get(
        "/v1/timeline/upcoming",
        params={"ref": ["investor:hotel", f"stock:{X}", "insider:101", "investor:india"]},
    )

    assert response.status_code == 200
    body = response.json()
    period, due = quarter_label(current), str(current + timedelta(days=45))
    assert body["data"] == [
        {
            "ref": {"kind": "stock", "cusip": X},
            "period": period,
            "due_at": due,
            "pending_holders": {
                "count": 2,
                "first": [
                    {"slug": "india", "display_name": "INDIA"},
                    {"slug": "juliet", "display_name": "JULIET"},
                ],
            },
        },
        {
            "ref": {"kind": "investor", "slug": "india"},
            "period": period,
            "due_at": due,
            "pending_holders": None,
        },
    ]
    assert body["meta"]["period"] == period
    assert body["meta"]["coverage"] == {"filers_reported": 1, "filers_tracked": 3}


async def test_upcoming_is_empty_once_every_filer_has_filed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    current = current_quarter(date.today())
    prior = quarters_ending(current, 2)[0]
    only = await _fund(db_session, "only")
    await _filing(db_session, only, prior, {X: 100}, at(str(prior + timedelta(days=40))))
    await _filing(db_session, only, current, {X: 100}, at(str(current + timedelta(days=5))))
    await _publish(db_session)

    response = await client.get(
        "/v1/timeline/upcoming", params={"ref": [f"stock:{X}", "investor:only"]}
    )

    assert response.json()["data"] == []


async def test_upcoming_names_at_most_three_pending_holders(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    current = current_quarter(date.today())
    prior = quarters_ending(current, 2)[0]
    for n, slug in enumerate(("aa", "bb", "cc", "dd", "ee"), start=1):
        filer_id = await _fund(db_session, slug)
        await _filing(
            db_session, filer_id, prior, {X: 100 * n}, at(str(prior + timedelta(days=40)))
        )
    await _publish(db_session)

    response = await client.get("/v1/timeline/upcoming", params={"ref": f"stock:{X}"})

    [pending] = [item["pending_holders"] for item in response.json()["data"]]
    assert pending["count"] == 5
    assert [h["slug"] for h in pending["first"]] == ["ee", "dd", "cc"]
