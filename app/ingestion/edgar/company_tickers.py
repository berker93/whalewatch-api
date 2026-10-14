"""SEC's ticker-to-CIK map, ``company_tickers.json``.

One document, about 10,000 rows, keyed by row number for no reason anyone has
explained: ``{"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA
CORP"}, ...}``. Only companies listed today. Stored in ``sec_company_ticker``
(:mod:`app.db.models.sec_company_ticker`).

Through :class:`~app.ingestion.edgar.client.EdgarClient`: it is a www.sec.gov
request like any other, with the same User-Agent and rate limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from app.ingestion.edgar.client import EdgarClient

COMPANY_TICKERS_URL: Final = "https://www.sec.gov/files/company_tickers.json"


class CompanyTickersMalformedError(ValueError):
    """The document is not shaped as expected. Nothing from it is used."""


@dataclass(frozen=True, slots=True)
class CompanyTicker:
    ticker: str
    """As SEC spells it, ``BRK-B``."""

    cik: str
    """Zero-padded."""

    name: str


async def fetch_company_tickers(edgar: EdgarClient) -> list[CompanyTicker]:
    """Today's map, never from the development cache: it changes as companies
    list and delist, and a crawl bounded by a stale one misses the new ones.

    :raises CompanyTickersMalformedError: See :func:`parse_company_tickers`.
    """
    return parse_company_tickers(await edgar.get_json(COMPANY_TICKERS_URL, fresh=True))


def parse_company_tickers(document: Any) -> list[CompanyTicker]:
    """Every row, tickers upper-cased. A ticker listed twice keeps its first row.

    :raises CompanyTickersMalformedError: Not an object of objects with a
        ``cik_str``, ``ticker`` and ``title``, or no rows at all. An empty map
        would bound the crawl to nothing.
    """
    if not isinstance(document, dict) or not document:
        raise CompanyTickersMalformedError("expected a non-empty object of rows")
    rows: dict[str, CompanyTicker] = {}
    for key, row in document.items():
        try:
            cik = int(row["cik_str"])
            ticker = str(row["ticker"]).strip().upper()
            name = str(row["title"]).strip()
        except (KeyError, TypeError, ValueError) as failure:
            raise CompanyTickersMalformedError(f"row {key!r} is unreadable: {row!r}") from failure
        if ticker:
            rows.setdefault(ticker, CompanyTicker(ticker=ticker, cik=f"{cik:010d}", name=name))
    return list(rows.values())
