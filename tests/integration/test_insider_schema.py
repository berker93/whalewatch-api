"""The Form 4 schema, asserted against a real Postgres (0028).

The same bias as test_core_schema.py: towards what happens on the second write.
A Form 4 line has no identity of its own, so the constraint that keeps a
re-ingested filing from doubling its trades is a choice, and the tests below
are what holds that choice in place.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Filing, Insider, InsiderTransaction, ReportingOwner, Security

NVIDIA_CIK = "0001045810"
HUANG_CIK = "0001197647"
STEVENS_CIK = "0001221000"
TRADE_DAY = date(2026, 6, 20)


async def _a_form4(session: AsyncSession, accession_no: str = "0001197647-26-000042") -> Filing:
    filing = Filing(
        accession_no=accession_no,
        cik=HUANG_CIK,
        form_type="4",
        period_of_report=None,
        filed_at=datetime(2026, 6, 23, 21, 5, tzinfo=UTC),
        value_multiplier=1,
    )
    session.add(filing)
    await session.flush()
    return filing


async def _an_insider(session: AsyncSession, cik: str = HUANG_CIK) -> Insider:
    insider = Insider(cik=cik, name="HUANG JEN HSUN")
    session.add(insider)
    await session.flush()
    return insider


async def _an_owner(
    session: AsyncSession, filing: Filing, insider: Insider, **roles: Any
) -> ReportingOwner:
    owner = ReportingOwner(filing_id=filing.id, insider_id=insider.id, **roles)
    session.add(owner)
    await session.flush()
    return owner


def _a_line(filing: Filing, insider: Insider, line_number: int, **overrides: Any) -> dict[str, Any]:
    """A 10b5-1 sale of 500 NVDA shares at $141.20: one tranche of many."""
    return {
        "filing_id": filing.id,
        "insider_id": insider.id,
        "line_number": line_number,
        "transaction_date": TRADE_DAY,
        "issuer_cik": NVIDIA_CIK,
        "issuer_name": "NVIDIA CORP",
        "issuer_ticker": "NVDA",
        "security_title": "Common Stock",
        "is_derivative": False,
        "transaction_code": "S",
        "acquired_disposed": "D",
        "shares": Decimal("500"),
        "price_per_share": Decimal("141.20"),
        "shares_owned_after": Decimal("75000000"),
        "is_10b5_1": True,
        **overrides,
    }


async def _load(session: AsyncSession, lines: list[dict[str, Any]]) -> None:
    """The loader's insert: ``ON CONFLICT DO NOTHING`` on the natural key."""
    await session.execute(
        insert(InsiderTransaction)
        .values(lines)
        .on_conflict_do_nothing(
            constraint="uq_insider_transaction_filing_id_line_number_transaction_date"
        )
    )


async def partition_indexes(session: AsyncSession, index: str) -> set[str]:
    """The per-partition indexes under a partitioned index. A plan names these,
    never the parent, and Postgres names them after the partition."""
    names = await session.scalars(
        text("""
            SELECT child.relname FROM pg_inherits
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            WHERE pg_inherits.inhparent = CAST(:index AS regclass)
        """),
        {"index": index},
    )
    return set(names)


# --- the natural key ----------------------------------------------------------


async def test_the_same_filing_three_times_is_the_same_rows(db_session: AsyncSession) -> None:
    """Two tranches that agree on every value, and an option exercise, loaded
    three times. Three rows, because the tranches differ by their line in the
    document and a re-ingest repeats the line numbers along with everything
    else. A key on the values would have kept one tranche."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider, is_director=True, is_officer=True)
    lines = [
        _a_line(filing, insider, 1),
        _a_line(filing, insider, 2),
        _a_line(
            filing,
            insider,
            3,
            is_derivative=True,
            security_title="Stock Option (Right to Buy)",
            transaction_code="M",
            price_per_share=Decimal("3.97"),
        ),
    ]

    for _ in range(3):
        await _load(db_session, lines)

    rows = (
        await db_session.execute(
            select(
                InsiderTransaction.line_number,
                InsiderTransaction.is_derivative,
                InsiderTransaction.shares,
            ).order_by(InsiderTransaction.line_number)
        )
    ).all()

    assert [tuple(row) for row in rows] == [
        (1, False, Decimal("500.0000")),
        (2, False, Decimal("500.0000")),
        (3, True, Decimal("500.0000")),
    ]


async def test_the_partition_key_is_in_every_unique_constraint(db_session: AsyncSession) -> None:
    """Postgres refuses a unique constraint on a partitioned table that leaves
    out the partition key. ``transaction_date`` is in both now, so partitioning
    on it later does not change what counts as a duplicate."""
    rows = (
        await db_session.execute(
            text("""
                SELECT c.conname, array_agg(a.attname::text ORDER BY a.attname)
                FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.conrelid = 'insider_transaction'::regclass AND c.contype IN ('p', 'u')
                GROUP BY c.conname
            """)
        )
    ).all()
    keys: dict[str, list[str]] = {name: columns for name, columns in rows}

    assert keys == {
        "pk_insider_transaction": ["id", "transaction_date"],
        "uq_insider_transaction_filing_id_line_number_transaction_date": [
            "filing_id",
            "line_number",
            "transaction_date",
        ],
    }


# --- value_usd ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("shares", "price", "value"),
    [
        (Decimal("500"), Decimal("141.20"), Decimal("70600.00")),
        # A weighted-average price to six places, rounded to the cent.
        (Decimal("1234"), Decimal("141.123456"), Decimal("174146.34")),
        # A grant or a gift: no price, so no value. Never zero.
        (Decimal("10000"), None, None),
    ],
)
async def test_value_is_shares_times_price_and_null_without_a_price(
    db_session: AsyncSession, shares: Decimal, price: Decimal | None, value: Decimal | None
) -> None:
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)
    await _load(db_session, [_a_line(filing, insider, 1, shares=shares, price_per_share=price)])

    found = (await db_session.execute(select(InsiderTransaction.value_usd))).scalar_one()

    assert found == value


async def test_value_cannot_be_written_to(db_session: AsyncSession) -> None:
    """Generated, so the loader cannot write a value that disagrees with the
    shares and price beside it."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)

    with pytest.raises(DBAPIError, match="value_usd"):
        async with db_session.begin_nested():
            await _load(db_session, [_a_line(filing, insider, 1, value_usd=Decimal("1.00"))])


# --- invariants ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        # Null it, do not zero it: a $0 purchase averages into every mean.
        ("price_per_share", Decimal("0"), "ck_insider_transaction_a_price_is_positive_or_null"),
        ("acquired_disposed", "X", "ck_insider_transaction_acquired_disposed_is_known"),
        ("line_number", 0, "ck_insider_transaction_line_number_is_positive"),
        ("shares", Decimal("-500"), "ck_insider_transaction_shares_is_not_negative"),
        (
            "shares_owned_after",
            Decimal("-1"),
            "ck_insider_transaction_shares_owned_after_is_not_negative",
        ),
        ("footnotes", ["F1"], "ck_insider_transaction_footnotes_is_an_object"),
    ],
)
async def test_a_line_that_cannot_be_right_is_refused(
    db_session: AsyncSession, field: str, value: object, constraint: str
) -> None:
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)

    with pytest.raises(IntegrityError, match=constraint):
        async with db_session.begin_nested():
            await _load(db_session, [{**_a_line(filing, insider, 1), field: value}])


async def test_any_transaction_code_is_stored_as_filed(db_session: AsyncSession) -> None:
    """The code set is the SEC's, so nothing here closes it. A code no one has
    seen yet lands, for the read layer to label."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)
    await _load(db_session, [_a_line(filing, insider, 1, transaction_code="Q")])

    code = (await db_session.execute(select(InsiderTransaction.transaction_code))).scalar_one()

    assert code == "Q"


# --- insiders and their roles ---------------------------------------------------


async def test_an_insider_is_one_cik(db_session: AsyncSession) -> None:
    await _an_insider(db_session)

    with pytest.raises(IntegrityError, match="uq_insider_cik"):
        async with db_session.begin_nested():
            await _an_insider(db_session)


async def test_roles_are_several_at_once_and_belong_to_the_filing(
    db_session: AsyncSession,
) -> None:
    """A CEO on the board is an officer and a director on one filing, and the
    same person can be only a director on another, because roles are what an
    insider was to an issuer when they filed."""
    insider = await _an_insider(db_session)
    as_ceo = await _a_form4(db_session, "0001197647-26-000042")
    as_director = await _a_form4(db_session, "0001197647-26-000043")
    await _an_owner(
        db_session,
        as_ceo,
        insider,
        is_director=True,
        is_officer=True,
        officer_title="President and CEO",
    )
    await _an_owner(db_session, as_director, insider, is_director=True)

    rows = (
        await db_session.execute(
            select(
                ReportingOwner.is_director,
                ReportingOwner.is_officer,
                ReportingOwner.is_ten_percent_owner,
                ReportingOwner.officer_title,
            ).order_by(ReportingOwner.filing_id)
        )
    ).all()

    assert [tuple(row) for row in rows] == [
        (True, True, False, "President and CEO"),
        (True, False, False, None),
    ]


async def test_a_joint_filing_records_every_owner_and_each_trade_once(
    db_session: AsyncSession,
) -> None:
    """Two reporting owners, one purchase: one transaction row, attributed to
    the first owner, with both owners on the filing."""
    filing = await _a_form4(db_session)
    first = await _an_insider(db_session)
    second = await _an_insider(db_session, STEVENS_CIK)
    await _an_owner(db_session, filing, first, is_ten_percent_owner=True)
    await _an_owner(db_session, filing, second, is_ten_percent_owner=True)
    await _load(
        db_session, [_a_line(filing, first, 1, transaction_code="P", acquired_disposed="A")]
    )

    owners = (
        await db_session.execute(
            select(func.count()).select_from(ReportingOwner).filter_by(filing_id=filing.id)
        )
    ).scalar_one()
    trades = (
        await db_session.execute(select(func.count()).select_from(InsiderTransaction))
    ).scalar_one()

    assert (owners, trades) == (2, 1)


async def test_a_transaction_belongs_to_an_owner_on_its_own_filing(
    db_session: AsyncSession,
) -> None:
    """The composite FK: an insider who is not on the filing cannot be given
    its trades."""
    filing = await _a_form4(db_session)
    owner = await _an_insider(db_session)
    stranger = await _an_insider(db_session, STEVENS_CIK)
    await _an_owner(db_session, filing, owner)

    with pytest.raises(
        IntegrityError, match="fk_insider_transaction_filing_id_insider_id_reporting_owner"
    ):
        async with db_session.begin_nested():
            await _load(db_session, [_a_line(filing, stranger, 1)])


# --- the security link and deletion -------------------------------------------


async def test_the_security_link_is_optional_and_never_blocks(db_session: AsyncSession) -> None:
    """Null when the loader cannot tell which security, and set back to null,
    not refused, when the security it named goes."""
    security = Security(cusip="67066G104", name="NVIDIA CORP", ticker=None)
    db_session.add(security)
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)
    await _load(
        db_session,
        [
            _a_line(filing, insider, 1, security_id=security.id),
            _a_line(filing, insider, 2, security_id=None),
        ],
    )

    await db_session.execute(text("DELETE FROM security WHERE id = :id"), {"id": security.id})
    links = (
        (
            await db_session.execute(
                select(InsiderTransaction.security_id).order_by(InsiderTransaction.line_number)
            )
        )
        .scalars()
        .all()
    )

    assert links == [None, None]


async def test_deleting_a_filing_takes_its_owners_and_transactions(
    db_session: AsyncSession,
) -> None:
    """How a 4/A supersedes its original, and how a bad parse is redone. The
    insider stays: it is on other filings, or will be."""
    filing = await _a_form4(db_session)
    insider = await _an_insider(db_session)
    await _an_owner(db_session, filing, insider)
    await _load(db_session, [_a_line(filing, insider, 1), _a_line(filing, insider, 2)])

    await db_session.execute(text("DELETE FROM filing WHERE id = :id"), {"id": filing.id})
    remaining = (
        await db_session.execute(
            text("""
                SELECT (SELECT count(*) FROM reporting_owner),
                       (SELECT count(*) FROM insider_transaction),
                       (SELECT count(*) FROM insider)
            """)
        )
    ).one()

    assert tuple(remaining) == (0, 0, 1)


# --- the feed's index ---------------------------------------------------------


async def test_the_open_market_index_serves_the_feed(db_session: AsyncSession) -> None:
    """Partial on P and S, newest first and then by id (0031): the shape and
    the order of the default feed. With sequential scans off, so that an empty
    table still shows which index the planner can use rather than which is
    cheapest on no rows."""
    definition = (
        await db_session.execute(
            text("""
                SELECT indexdef FROM pg_indexes
                WHERE indexname = 'ix_insider_transaction_transaction_date_open_market'
            """)
        )
    ).scalar_one()
    indexes = await partition_indexes(
        db_session, "ix_insider_transaction_transaction_date_open_market"
    )
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        (
            await db_session.execute(
                text("""
                    EXPLAIN SELECT id FROM insider_transaction
                    WHERE transaction_code IN ('P', 'S')
                    ORDER BY transaction_date DESC LIMIT 50
                """)
            )
        )
        .scalars()
        .all()
    )

    assert "(transaction_date DESC, id DESC)" in definition
    assert "WHERE (transaction_code = ANY (ARRAY['P'::bpchar, 'S'::bpchar]))" in definition
    # Partitioned (0032): the plan names the index on each partition. Scanned
    # in order or as a bitmap, depending on what other tests left in the stats.
    assert any(index in plan for index in indexes), plan
