"""form4 crawl

``crawl_watermark``: the last day each incremental crawl has read in full, for
``crawl-form4``. ``sec_company_ticker``: SEC's ticker-to-CIK map, which bounds
that crawl to the issuers tracked investors hold. See
:class:`~app.db.models.crawl_watermark.CrawlWatermark` and
:class:`~app.db.models.sec_company_ticker.SecCompanyTicker`.

Created empty. ``crawl-form4`` fills both.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-14 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_watermark",
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("last_date", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("job", name="pk_crawl_watermark"),
    )
    op.create_table(
        "sec_company_ticker",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("cik", sa.CHAR(length=10), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ticker", name="pk_sec_company_ticker"),
        # Bare names: the ck template prefixes them (see 0002).
        sa.CheckConstraint("ticker <> ''", name="ticker_is_not_empty"),
    )
    op.create_index("ix_sec_company_ticker_cik", "sec_company_ticker", ["cik"])


def downgrade() -> None:
    """Loses where the Form 4 crawl had got to: the next ``--catch-up`` needs a
    ``--from`` first. The ticker map is fetched again on the next crawl."""
    op.drop_index("ix_sec_company_ticker_cik", table_name="sec_company_ticker")
    op.drop_table("sec_company_ticker")
    op.drop_table("crawl_watermark")
