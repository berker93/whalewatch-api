"""The issuer-name fallback: OpenFIGI's name, else the latest 13(f) list's.

Against the real table, inside the rolled-back ``db_session``, because "the
latest list that has the CUSIP" is a ``DISTINCT ON`` worth running rather than
trusting.
"""

from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Sec13FSecurity
from app.db.models.figi_response_log import FIGIOutcome
from app.db.models.sec_13f_security import ListStatus
from app.ingestion.enrichment.figi import FIGIResult
from app.ingestion.enrichment.issuer_names import (
    IssuerName,
    IssuerNameSource,
    ListedName,
    issuer_name,
    issuer_names,
    latest_listed_names,
)

APPLE: Final = "037833100"
LIFEWARD: Final = "M8216Q119"
UNLISTED: Final = "999999999"
ASKED_AT: Final = datetime(2026, 10, 12, tzinfo=UTC)


def _found(cusip: str, *names: str | None) -> FIGIResult:
    response = {"data": [{"figi": f"BBG00000000{i}", "name": name} for i, name in enumerate(names)]}
    return FIGIResult.from_response(cusip, response, ASKED_AT)


def _not_found(cusip: str) -> FIGIResult:
    return FIGIResult.from_response(cusip, {"warning": "No identifier found."}, ASKED_AT)


async def _list(session: AsyncSession, period: date, cusip: str, name: str, status: str) -> None:
    await session.execute(
        insert(Sec13FSecurity).values(
            period_of_report=period,
            cusip=cusip,
            issuer_name=name,
            status=status,
            has_listed_options=False,
        )
    )


async def test_the_latest_list_naming_the_cusip_wins_whatever_its_status(
    db_session: AsyncSession,
) -> None:
    """Deleted in 2026Q1 is still what the SEC last said about LIFEWARD."""
    await _list(db_session, date(2025, 12, 31), LIFEWARD, "RE WALK ROBOTICS LTD", "listed")
    await _list(db_session, date(2026, 3, 31), LIFEWARD, "LIFEWARD LTD", "deleted")
    await _list(db_session, date(2026, 3, 31), APPLE, "APPLE INC", "listed")

    listed = await latest_listed_names(db_session, [LIFEWARD, APPLE, UNLISTED])

    assert listed == {
        APPLE: ListedName(APPLE, "APPLE INC", date(2026, 3, 31), ListStatus.LISTED),
        LIFEWARD: ListedName(LIFEWARD, "LIFEWARD LTD", date(2026, 3, 31), ListStatus.DELETED),
    }


async def test_no_cusips_is_no_query(db_session: AsyncSession) -> None:
    assert await latest_listed_names(db_session, []) == {}


def test_openfigi_wins_when_it_matched_with_a_name() -> None:
    listed = ListedName(APPLE, "APPLE INC", date(2026, 3, 31), ListStatus.LISTED)

    assert issuer_name(_found(APPLE, "APPLE INC."), listed) == IssuerName(
        "APPLE INC.", IssuerNameSource.OPENFIGI
    )


def test_the_first_named_match_is_openfigis_name() -> None:
    assert issuer_name(_found(APPLE, None, "  ", "APPLE INC"), None) == IssuerName(
        "APPLE INC", IssuerNameSource.OPENFIGI
    )


def test_the_list_names_what_openfigi_could_not() -> None:
    listed = ListedName(LIFEWARD, "LIFEWARD LTD", date(2026, 3, 31), ListStatus.DELETED)
    expected = IssuerName("LIFEWARD LTD", IssuerNameSource.SEC_13F_LIST, date(2026, 3, 31))

    assert issuer_name(_not_found(LIFEWARD), listed) == expected
    assert issuer_name(_found(LIFEWARD, None), listed) == expected
    assert issuer_name(None, listed) == expected


def test_nothing_from_either_is_none() -> None:
    assert issuer_name(_not_found(UNLISTED), None) is None


async def test_issuer_names_reads_the_list_only_for_what_openfigi_left_unnamed(
    db_session: AsyncSession,
) -> None:
    await _list(db_session, date(2026, 3, 31), APPLE, "APPLE INC", "listed")
    await _list(db_session, date(2026, 3, 31), LIFEWARD, "LIFEWARD LTD", "listed")

    names = await issuer_names(
        db_session,
        {LIFEWARD: _not_found(LIFEWARD), APPLE: _found(APPLE, "APPLE INC."), UNLISTED: None},
    )

    assert names == {
        LIFEWARD: IssuerName("LIFEWARD LTD", IssuerNameSource.SEC_13F_LIST, date(2026, 3, 31)),
        APPLE: IssuerName("APPLE INC.", IssuerNameSource.OPENFIGI),
    }
    assert list(names) == [LIFEWARD, APPLE]


async def test_an_error_outcome_falls_back_too(db_session: AsyncSession) -> None:
    await _list(db_session, date(2026, 3, 31), APPLE, "APPLE INC", "listed")
    errored = FIGIResult.from_response(APPLE, {"error": "Invalid idValue format"}, ASKED_AT)
    assert errored.outcome is FIGIOutcome.ERROR

    names = await issuer_names(db_session, {APPLE: errored})

    assert names[APPLE].source is IssuerNameSource.SEC_13F_LIST
