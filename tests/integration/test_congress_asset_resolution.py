"""CON-6, against a real Postgres: each PTR line's asset resolved to a class, and
a stock's to the ``security`` a 13F holding of it has.

The securities are a handful, as ``resolve-cusips`` leaves them: a ticker and
OpenFIGI's name, the filer's name beside it, a ticker history for one, a
delisting for another. The lines are inserted as the PTR readers leave them,
and :func:`~app.ingestion.congress.asset_resolution.resolve_assets` run over
them as both PTR jobs do.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count
from typing import Any, Final

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import congress, congress_assets
from app.admin.congress import ReviewError
from app.db.models import (
    AdminAction,
    CongressAssetOverride,
    CongressFiling,
    CongressTransaction,
    Filer,
    FilerCik,
    Filing,
    PositionSnapshot,
    Security,
    SecurityAlias,
)
from app.ingestion.congress.asset_resolution import asset_coverage, resolve_assets
from app.ingestion.congress.assets import AssetClass, asset_key
from tests.integration.test_admin import make_admin

ON: Final = date(2026, 6, 9)
ON_RENAME: Final = date(2022, 6, 9)
_numbers = count(1)


async def _security(
    session: AsyncSession,
    cusip: str,
    name: str,
    ticker: str | None = None,
    *,
    filed_name: str | None = None,
    delisted_by: date | None = None,
) -> int:
    security_id = await session.scalar(
        insert(Security)
        .values(
            cusip=cusip,
            name=name,
            filed_name=filed_name or name,
            ticker=ticker,
            resolution_source="openfigi" if ticker else "13f_list",
            delisted_by=delisted_by,
        )
        .returning(Security.id)
    )
    assert security_id is not None
    return security_id


async def _held(session: AsyncSession, security_id: int, value: int, period: date) -> None:
    """``value`` dollars of it in one made-up filer's 13F for ``period``."""
    number = next(_numbers)
    cik = f"{number + 9_000_000:010d}"
    filer_id = await session.scalar(
        insert(Filer).values(name=f"fund {cik}", slug=f"fund-{cik}").returning(Filer.id)
    )
    await session.execute(insert(FilerCik).values(filer_id=filer_id, cik=cik, priority=1))
    filing_id = await session.scalar(
        insert(Filing)
        .values(
            accession_no=f"{cik}-26-{number:06d}",
            cik=cik,
            filer_id=filer_id,
            form_type="13F-HR",
            period_of_report=period,
            filed_at=datetime(2026, 5, 1, tzinfo=UTC),
            value_multiplier=1,
            parse_status="ok",
        )
        .returning(Filing.id)
    )
    await session.execute(
        insert(PositionSnapshot).values(
            filer_id=filer_id,
            period_of_report=period,
            security_id=security_id,
            shares=1,
            value_usd=value,
            source_filing_id=filing_id,
            suspect=False,
        )
    )


@pytest.fixture
async def filing(db_session: AsyncSession) -> int:
    filing_id = await db_session.scalar(
        insert(CongressFiling)
        .values(
            chamber="house",
            doc_id="20033725",
            report_type="ptr",
            last_name="Pelosi",
            first_name="Nancy",
            office="CA11",
            filing_date=date(2026, 7, 8),
            year=2026,
            source_key="raw/house-clerk/index/2026/x.zip",
            parser_version=1,
            extraction="text",
        )
        .returning(CongressFiling.id)
    )
    assert filing_id is not None
    return filing_id


_lines = count(1)


async def _line(
    session: AsyncSession,
    filing_id: int,
    name: str | None,
    ticker: str | None = None,
    **values: Any,
) -> int:
    defaults: dict[str, Any] = {
        "parse_status": "parsed",
        "extraction": "text",
        "asset_type": "ST",
        "transaction_type": "purchase",
        "transaction_date": ON,
        "amount_low": 1_001,
        "amount_high": 15_000,
    }
    line_id = await session.scalar(
        insert(CongressTransaction)
        .values(
            filing_id=filing_id,
            line=next(_lines),
            asset_name=name,
            ticker=ticker,
            **(defaults | values),
        )
        .returning(CongressTransaction.id)
    )
    assert line_id is not None
    return line_id


async def _resolved(session: AsyncSession, line_id: int) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(
                CongressTransaction.asset_class,
                Security.cusip,
                CongressTransaction.resolution_method,
                CongressTransaction.resolution_confidence,
            )
            .outerjoin(Security, Security.id == CongressTransaction.security_id)
            .where(CongressTransaction.id == line_id)
        )
    ).one()
    return tuple(row)


# --- by ticker ----------------------------------------------------------------------


async def test_the_filers_ticker_finds_the_security_a_13f_holding_has(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "594918104", "MICROSOFT CORP", "MSFT")
    line = await _line(db_session, filing, "Microsoft Corporation - Common Stock", "MSFT")

    passed = await resolve_assets(db_session)

    assert await _resolved(db_session, line) == ("equity", "594918104", "ticker", Decimal("1"))
    assert (passed.lines, passed.resolved, passed.unresolved) == (1, 1, 0)


async def test_a_ticker_whose_name_does_not_agree_is_still_taken_less_surely(
    db_session: AsyncSession, filing: int
) -> None:
    """GE Aerospace trades as GE; our name for it is still General Electric's."""
    await _security(db_session, "369604301", "GENERAL ELECTRIC", "GE")
    line = await _line(db_session, filing, "GE Aerospace Common Stock", "GE")

    await resolve_assets(db_session)

    assert await _resolved(db_session, line) == (
        "equity",
        "369604301",
        "ticker",
        Decimal("0.800"),
    )


async def test_a_ticker_of_another_time_counts_only_if_the_name_agrees(
    db_session: AsyncSession, filing: int
) -> None:
    meta = await _security(
        db_session, "30303M102", "META PLATFORMS INC-CLASS A", "META", filed_name="FACEBOOK INC"
    )
    await db_session.execute(
        insert(SecurityAlias).values(
            [
                {"security_id": meta, "ticker": "FB", "valid_from": None, "valid_to": ON_RENAME},
                {"security_id": meta, "ticker": "META", "valid_from": ON_RENAME, "valid_to": None},
            ]
        )
    )
    agrees = await _line(db_session, filing, "Facebook, Inc. - Class A Common Stock", "FB")
    other = await _line(db_session, filing, "First Bancorp", "FB")

    await resolve_assets(db_session)

    assert await _resolved(db_session, agrees) == (
        "equity",
        "30303M102",
        "former_ticker",
        Decimal("0.900"),
    )
    assert await _resolved(db_session, other) == ("equity", None, None, None)


async def test_a_ticker_in_a_scans_name_is_read_out_of_it(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "892356106", "TRACTOR SUPPLY COMPANY", "TSCO")
    await _security(db_session, "67066G104", "NVIDIA CORP", "NVDA")
    trailing = await _line(
        db_session, filing, "Tractor Supply Co TSCO", asset_type=None, extraction="ocr"
    )
    parenthesised = await _line(
        db_session, filing, "Nvidia Corporation (NVDA)", asset_type=None, extraction="ocr"
    )

    await resolve_assets(db_session)

    assert await _resolved(db_session, trailing) == (
        "equity",
        "892356106",
        "ticker",
        Decimal("0.900"),
    )
    assert await _resolved(db_session, parenthesised) == (
        "equity",
        "67066G104",
        "ticker",
        Decimal("1"),
    )


# --- by name ------------------------------------------------------------------------


async def test_a_stock_with_no_ticker_is_found_by_its_issuers_name(
    db_session: AsyncSession, filing: int
) -> None:
    """Electronic Arts, taken private: its CUSIP has a name and no ticker, so the
    filer's EA finds nothing, and the name finds it."""
    common = await _security(db_session, "285512109", "ELECTRONIC ARTS INC")
    option = await _security(db_session, "285512909", "ELECTRONIC ARTS INC")
    await _held(db_session, common, 5_000_000, date(2026, 3, 31))
    await _held(db_session, option, 10_000, date(2026, 3, 31))
    line = await _line(db_session, filing, "Electronic Arts Inc. - Common Stock", "EA")

    await resolve_assets(db_session)

    assert await _resolved(db_session, line) == (
        "equity",
        "285512109",
        "name",
        Decimal("0.950"),
    )


async def test_a_name_match_prefers_the_cusip_listed_on_the_trades_day(
    db_session: AsyncSession, filing: int
) -> None:
    """Honeywell's CUSIP changed in 2026: the old one is still the more held."""
    old = await _security(
        db_session, "438516106", "HONEYWELL INTL INC", delisted_by=date(2026, 6, 30)
    )
    new = await _security(
        db_session, "438516205", "HONEYWELL INTERNATIONAL INC", "HON", filed_name="HONEYWELL INTL"
    )
    await _held(db_session, old, 14_000_000, date(2026, 3, 31))
    await _held(db_session, new, 900_000, date(2026, 6, 30))
    before = await _line(db_session, filing, "HONEYWELL INTL INC", asset_type=None)
    after = await _line(
        db_session, filing, "HONEYWELL INTL INC", asset_type=None, transaction_date=date(2026, 8, 3)
    )

    await resolve_assets(db_session)

    assert (await _resolved(db_session, before))[1] == "438516106"
    assert (await _resolved(db_session, after))[1] == "438516205"


async def test_a_name_match_keeps_to_the_share_class_the_line_names(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "02079K305", "ALPHABET INC-CL A", "GOOGL")
    await _security(db_session, "02079K107", "ALPHABET INC-CL C", "GOOG")
    line = await _line(db_session, filing, "ALPHABET INC CLC", asset_type=None)

    await resolve_assets(db_session)

    assert (await _resolved(db_session, line))[1:3] == ("02079K107", "name")


async def test_a_foreign_share_found_as_its_adr_is_only_so_sure(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "654902204", "NOKIA CORP-SPON ADR", "NOK")
    line = await _line(db_session, filing, "NOKIA A SHS", "NOKBF")

    await resolve_assets(db_session)

    assert await _resolved(db_session, line) == ("equity", "654902204", "name", Decimal("0.800"))


async def test_a_preferred_is_not_taken_for_its_issuers_common(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "446150104", "HUNTINGTON BANCSHARES INC", "HBAN")
    line = await _line(
        db_session,
        filing,
        "Huntington Bancshares Incorporated - Depositary Shares L Preferred Stock",
        "HBANZ",
    )

    await resolve_assets(db_session)

    assert await _resolved(db_session, line) == ("equity", None, None, None)


# --- what has no security -------------------------------------------------------------


async def test_bonds_funds_and_crypto_are_classified_and_never_looked_up(
    db_session: AsyncSession, filing: int
) -> None:
    # A stock named like the muni, which a name search would find.
    await _security(db_session, "13000A101", "CALIFORNIA ST WTR CO", "CWT")
    muni = await _line(
        db_session, filing, "California St Go Call 12/1/27 4% due 12/1/47", None, asset_type="GS"
    )
    treasury = await _line(db_session, filing, "U.S. Treasury Bills", asset_type="GS")
    fund = await _line(db_session, filing, "NORTHERN INTERMED TAX EXEMPT FUND", asset_type=None)
    coin = await _line(db_session, filing, "Bitcoin (CRYPTO:BTC)", asset_type="CT")

    passed = await resolve_assets(db_session)

    assert [(await _resolved(db_session, line))[:2] for line in (muni, treasury, fund, coin)] == [
        ("municipal_bond", None),
        ("treasury", None),
        ("mutual_fund", None),
        ("crypto", None),
    ]
    assert passed.unresolved == 0


async def test_an_unresolved_line_keeps_its_description_and_a_scans_is_unknown(
    db_session: AsyncSession, filing: int
) -> None:
    stock = await _line(db_session, filing, "Ajinomoto Company, Inc. Ordinary Shares", "AJINF")
    scan = await _line(db_session, filing, "Ex mp! Meg Corp Common Stock", asset_type=None)

    passed = await resolve_assets(db_session)

    assert await _resolved(db_session, stock) == ("equity", None, None, None)
    assert await _resolved(db_session, scan) == ("unknown", None, None, None)
    assert passed.unresolved == 2
    kept = await db_session.execute(
        select(CongressTransaction.asset_name, CongressTransaction.ticker).where(
            CongressTransaction.id == stock
        )
    )
    assert kept.one() == ("Ajinomoto Company, Inc. Ordinary Shares", "AJINF")


async def test_placeholders_and_rejected_lines_are_not_resolved(
    db_session: AsyncSession, filing: int
) -> None:
    placeholder = await _line(
        db_session,
        filing,
        None,
        parse_status="placeholder",
        extraction="none",
        asset_type=None,
        transaction_type=None,
        transaction_date=None,
        amount_low=None,
        amount_high=None,
    )

    passed = await resolve_assets(db_session)

    assert passed.lines == 0
    assert await _resolved(db_session, placeholder) == (None, None, None, None)


# --- what runs again ----------------------------------------------------------------


async def test_a_run_retries_the_unresolved_and_leaves_the_resolved(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "594918104", "MICROSOFT CORP", "MSFT")
    resolved = await _line(db_session, filing, "Microsoft Corporation", "MSFT")
    waiting = await _line(db_session, filing, "Figma, Inc. Class A Common Stock", "FIG")
    await resolve_assets(db_session)
    first = await db_session.scalar(
        select(CongressTransaction.resolved_at).where(CongressTransaction.id == resolved)
    )
    # resolve-cusips gives Figma's CUSIP its ticker.
    await _security(db_session, "316841105", "FIGMA INC-CL A", "FIG")

    passed = await resolve_assets(db_session)

    assert passed.lines == 1
    assert (await _resolved(db_session, waiting))[1] == "316841105"
    assert (
        await db_session.scalar(
            select(CongressTransaction.resolved_at).where(CongressTransaction.id == resolved)
        )
        == first
    )
    assert (await resolve_assets(db_session, force=True)).lines == 2


# --- overrides ----------------------------------------------------------------------


async def test_an_override_covers_every_line_under_its_key_those_to_come_too(
    db_session: AsyncSession, filing: int
) -> None:
    adr = await _security(db_session, "654445303", "NINTENDO CO LTD-UNSPONS ADR", "NTDOY")
    first = await _line(db_session, filing, "NINTENDO CO LTD ORD", "NTDOF")
    await resolve_assets(db_session)
    assert (await _resolved(db_session, first))[1] is None
    key = asset_key("ST", "NTDOF", "NINTENDO CO LTD ORD")
    admin_id = await make_admin(db_session)

    made = await congress_assets.set_override(
        db_session,
        key,
        asset_class=AssetClass.EQUITY,
        cusip="654445303",
        ticker=None,
        note="the ordinary, as its ADR",
        admin_id=admin_id,
    )
    later = await _line(db_session, filing, "Nintendo Co Ltd Ord", "ntdof")
    await resolve_assets(db_session)

    assert (made.lines, made.override.security_id) == (1, adr)
    for line in (first, later):
        assert await _resolved(db_session, line) == ("equity", "654445303", "manual", Decimal("1"))
    # Nothing automatic overrides it, --force included.
    await resolve_assets(db_session, force=True)
    assert (await _resolved(db_session, first))[2] == "manual"

    removed = await congress_assets.remove_override(db_session, made.override.id, admin_id=None)

    assert removed == 2
    assert (await _resolved(db_session, later))[1:3] == (None, None)
    actions = await db_session.execute(
        select(AdminAction.action, AdminAction.source, AdminAction.detail["lines"].as_integer())
        .where(AdminAction.action.like("asset_%"), AdminAction.detail["asset_key"].astext == key)
        .order_by(AdminAction.id)
    )
    assert [tuple(row) for row in actions] == [
        ("asset_overridden", "console", 1),
        ("asset_override_removed", "cli", 2),
    ]


async def test_an_override_can_make_a_line_not_a_stock(
    db_session: AsyncSession, filing: int
) -> None:
    line = await _line(db_session, filing, "CALIFORNIA INFRASTRUCTURE", asset_type=None)
    await resolve_assets(db_session)
    key = asset_key(None, None, "CALIFORNIA INFRASTRUCTURE")

    await congress_assets.set_override(
        db_session,
        key,
        asset_class=AssetClass.MUNICIPAL_BOND,
        cusip=None,
        ticker=None,
        note=None,
        admin_id=None,
    )

    assert await _resolved(db_session, line) == ("municipal_bond", None, "manual", None)
    assert (await asset_coverage(db_session, "house")).resolvable == 0


@pytest.mark.parametrize(
    ("asset_class", "cusip", "code"),
    [
        (AssetClass.EQUITY, None, "validation_error"),
        (AssetClass.MUNICIPAL_BOND, "594918104", "validation_error"),
        (AssetClass.UNKNOWN, None, "validation_error"),
        (AssetClass.EQUITY, "000000000", "not_found"),
    ],
)
async def test_an_override_says_a_security_for_a_stock_and_none_for_the_rest(
    db_session: AsyncSession, filing: int, asset_class: AssetClass, cusip: str | None, code: str
) -> None:
    await _security(db_session, "594918104", "MICROSOFT CORP", "MSFT")
    await _line(db_session, filing, "Microsoft Corp", "MSFT")
    await resolve_assets(db_session)

    with pytest.raises(ReviewError) as refused:
        await congress_assets.set_override(
            db_session,
            asset_key("ST", "MSFT", "Microsoft Corp"),
            asset_class=asset_class,
            cusip=cusip,
            ticker=None,
            note=None,
            admin_id=None,
        )

    assert refused.value.code == code
    assert await db_session.scalar(select(CongressAssetOverride.id)) is None


async def test_an_override_for_a_key_no_line_has_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(ReviewError) as refused:
        await congress_assets.set_override(
            db_session,
            "ST|XX|nothing",
            asset_class=AssetClass.OTHER,
            cusip=None,
            ticker=None,
            note=None,
            admin_id=None,
        )

    assert refused.value.code == "not_found"


# --- coverage, and the review queue ---------------------------------------------------


async def test_coverage_counts_stock_lines_and_filings(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "594918104", "MICROSOFT CORP", "MSFT")
    await _line(db_session, filing, "Microsoft Corp", "MSFT")
    await _line(db_session, filing, "Ajinomoto Company, Inc. Ordinary Shares", "AJINF")
    await _line(db_session, filing, "U.S. Treasury Bills", asset_type="GS")
    await _line(db_session, filing, "In review", parse_status="needs_review")
    other = await db_session.scalar(
        insert(CongressFiling)
        .values(
            chamber="house",
            doc_id="20033801",
            report_type="ptr",
            last_name="Khanna",
            filing_date=date(2026, 8, 1),
            year=2026,
            source_key="raw/house-clerk/index/2026/x.zip",
        )
        .returning(CongressFiling.id)
    )
    assert other is not None
    await _line(db_session, other, "Microsoft Corp", "MSFT")
    pending = await asset_coverage(db_session, "house")
    await resolve_assets(db_session)

    counted = await asset_coverage(db_session, "house")

    assert (pending.lines, pending.pending) == (4, 4)
    assert counted.by_class == {"equity": 3, "treasury": 1}
    assert (counted.lines, counted.resolvable, counted.resolved) == (4, 3, 2)
    assert counted.by_method == {"ticker": 2}
    assert (counted.filings, counted.filings_resolved, counted.filings_partly) == (2, 1, 1)
    assert (counted.percent, counted.filing_percent) == (Decimal("66.7"), Decimal("50.0"))


async def test_a_line_accepted_from_review_is_resolved_as_corrected(
    db_session: AsyncSession, filing: int
) -> None:
    await _security(db_session, "892356106", "TRACTOR SUPPLY COMPANY", "TSCO")
    line = await _line(
        db_session,
        filing,
        "Tractor Supp1y Co",
        parse_status="needs_review",
        asset_type=None,
        extraction="ocr",
    )
    await resolve_assets(db_session)
    assert (await _resolved(db_session, line))[0] == "unknown"
    admin_id = await make_admin(db_session)

    found = await congress.review(
        db_session,
        line,
        decision="accept",
        corrections={"asset_name": "Tractor Supply Co", "ticker": "TSCO", "asset_type": "ST"},
        note=None,
        admin_id=admin_id,
        request_id=None,
    )

    assert found.security is not None and found.security.ticker == "TSCO"
    assert await _resolved(db_session, line) == ("equity", "892356106", "ticker", Decimal("1"))
