"""Screener presets: hand-picked screens behind plain-language names, as rows.

A preset is a set of the screener's named filters, a sort, and one sentence
saying what the screen shows. Rows rather than code, so adding one is a
migration and not a release of the API. The API reads a preset's filters as
it reads a request's, through the vocabulary (:mod:`app.api.screener_presets`):
a preset can ask nothing a person at the builder could not, and opening it in
the builder is those filters, filled in.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base

#: The longest a preset's name may be: a heading, not a sentence.
MAX_PRESET_NAME: Final = 80
#: The longest its explanation may be: one sentence, shown above the results.
MAX_PRESET_EXPLANATION: Final = 300


class ScreenerPreset(Base):
    """One preset screen, listed by :attr:`position`."""

    __tablename__ = "screener_preset"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    """Its URL: ``/v1/screener/presets/{preset}``. Shared and crawled, so never
    renamed once published."""

    position: Mapped[int] = mapped_column(SmallInteger)
    """Where it is listed, smallest first."""

    name: Mapped[str] = mapped_column(Text)
    """What it is called: "Quiet accumulation"."""

    explanation: Mapped[str] = mapped_column(Text)
    """The sentence shown above its results, saying what they are."""

    query: Mapped[dict[str, list[str]]] = mapped_column(JSONB)
    """The screener's filters, ``{key: [value, ...]}``, every value a string as
    a client would send it: ``{"action": ["new", "add"], "min_weight": ["10"]}``.
    Never the period: a preset is every quarter's, and the latest by default."""

    sort: Mapped[str] = mapped_column(Text)
    """One :class:`~app.api.screener.ScreenSort` value. Checked by the API, as
    the filters are, since the sorts are the API's."""

    sort_order: Mapped[str] = mapped_column(Text)
    """``desc`` or ``asc``."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_is_a_slug"),
        CheckConstraint(
            f"name = btrim(name) AND length(name) BETWEEN 1 AND {MAX_PRESET_NAME}",
            name="name_is_named",
        ),
        CheckConstraint(
            "explanation = btrim(explanation) AND length(explanation) BETWEEN 1 AND "
            f"{MAX_PRESET_EXPLANATION}",
            name="explanation_is_given",
        ),
        CheckConstraint(
            "jsonb_typeof(query) = 'object' AND NOT query ? 'period'",
            name="query_is_filters",
        ),
        CheckConstraint("sort_order IN ('desc', 'asc')", name="sort_order_is_known"),
        UniqueConstraint("position"),
    )
