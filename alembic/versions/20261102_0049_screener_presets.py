"""screener presets: hand-picked screens as rows, and the launch set

``screener_preset`` holds the screens behind plain-language names that
``GET /v1/screener/presets`` lists: each a set of the screener's named
filters, a sort and one sentence. Rows, so the next preset is a migration
like this one and not a release. The API reads a preset's filters through the
vocabulary, as it reads a request's, and a test runs every preset a migration
writes, so a misspelt filter fails CI rather than the page.

The launch set, in the order listed. Quiet accumulation and insiders buying
what funds are buying come first: the two that read data nobody else has
cleanly, a run of adds across quarters and Form 4 purchases by the rules a
cluster counts them by.

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-02 10:00:00.000000

"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRESETS: list[dict[str, Any]] = [
    {
        "slug": "quiet-accumulation",
        "name": "Quiet accumulation",
        "explanation": (
            "Positions an investor has added shares to three quarters running, a stake "
            "built steadily rather than bought all at once."
        ),
        "query": {"added_quarters": ["3"]},
        "sort": "value",
    },
    {
        "slug": "insiders-buying-what-funds-are-buying",
        "name": "Insiders buying what funds are buying",
        "explanation": (
            "Investors buying a stock in the same quarter its own officers or directors "
            "bought shares on the open market with their own money."
        ),
        "query": {"action": ["new", "add"], "insider_buyers": ["1"]},
        "sort": "traded",
    },
    {
        "slug": "most-bought",
        "name": "Most bought this quarter",
        "explanation": (
            "The quarter's largest purchases, in stocks investors bought more of than "
            "they sold overall."
        ),
        "query": {"action": ["new", "add"], "net_flow": ["buy"]},
        "sort": "traded",
    },
    {
        "slug": "most-sold",
        "name": "Most sold this quarter",
        "explanation": (
            "The quarter's largest sales, in stocks investors sold more of than they "
            "bought overall."
        ),
        "query": {"action": ["trim", "exit"], "net_flow": ["sell"]},
        "sort": "traded",
    },
    {
        "slug": "brand-new-positions",
        "name": "Brand new positions",
        "explanation": (
            "Stocks investors bought this quarter that they did not hold the quarter "
            "before, largest first."
        ),
        "query": {"is_new_position": ["true"]},
        "sort": "value",
    },
    {
        "slug": "everyone-sold-out",
        "name": "Everyone sold out of these",
        "explanation": (
            "Stocks five or more investors sold their whole position in this quarter, "
            "one row for each investor who did."
        ),
        "query": {"action": ["exit"], "min_exits": ["5"]},
        "sort": "traded",
    },
    {
        "slug": "big-bets",
        "name": "Big bets",
        "explanation": (
            "Positions worth 10% or more of the investor's whole portfolio, the biggest "
            "share first."
        ),
        "query": {"min_weight": ["10"]},
        "sort": "weight",
    },
]


def upgrade() -> None:
    op.create_table(
        "screener_preset",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("query", JSONB(), nullable=False),
        sa.Column("sort", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("slug", name="pk_screener_preset"),
        sa.UniqueConstraint("position", name="uq_screener_preset_position"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_is_a_slug"),
        sa.CheckConstraint(
            "name = btrim(name) AND length(name) BETWEEN 1 AND 80", name="name_is_named"
        ),
        sa.CheckConstraint(
            "explanation = btrim(explanation) AND length(explanation) BETWEEN 1 AND 300",
            name="explanation_is_given",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(query) = 'object' AND NOT query ? 'period'", name="query_is_filters"
        ),
        sa.CheckConstraint("sort_order IN ('desc', 'asc')", name="sort_order_is_known"),
    )
    # Literals rather than bulk_insert's parameters, so the chain still renders
    # to SQL offline: a parameter there would have to render a dict.
    rows = ",\n".join(
        f"({_quoted(row['slug'])}, {position}, {_quoted(row['name'])}, "
        f"{_quoted(row['explanation'])}, {_quoted(json.dumps(row['query']))}::jsonb, "
        f"{_quoted(row['sort'])}, 'desc')"
        for position, row in enumerate(PRESETS, start=1)
    )
    op.execute(f"""
        INSERT INTO screener_preset (slug, position, name, explanation, query, sort, sort_order)
        VALUES {rows}
    """)


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def downgrade() -> None:
    op.drop_table("screener_preset")
