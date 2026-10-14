"""How far an incremental crawl has got: one row per job, holding the last day it finished.

The Form 4 crawl walks EDGAR's daily index a day at a time
(:mod:`app.ingestion.insider_discovery`). Kept as a table, not derived from
what is queued, because "nothing was queued for 25 December" and "25 December
was never looked at" leave the same queue behind.

The watermark only moves forward through days that finished, one after
another. A day that failed stops it, and a day crawled out of order (a
``--date`` well past the watermark) is crawled without moving it, since the
days in between have not been. So whatever is at or before the watermark has
been read in full, and ``--catch-up`` starts the day after it.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class CrawlWatermark(Base):
    """The last day one job has read in full."""

    __tablename__ = "crawl_watermark"

    job: Mapped[str] = mapped_column(Text, primary_key=True)
    """The crawl's name, ``crawl-form4``: the CLI verb that moves it."""

    last_date: Mapped[date] = mapped_column(Date)
    """Every day up to and including this one has been read and its filings
    queued. A day with no index (a weekend, a holiday) counts as read."""

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    """When :attr:`last_date` last moved."""
