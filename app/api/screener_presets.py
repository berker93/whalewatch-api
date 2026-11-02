"""Presets: screens chosen by hand, behind a name and one sentence.

A preset is a row of ``screener_preset`` (:class:`~app.db.models.ScreenerPreset`):
the screener's named filters, a sort and an explanation. Rows, so that the
next preset is a migration, not a release. It is read here as a request is,
through :data:`~app.api.screener.VOCABULARY`, so a preset can ask nothing a
person at the builder could not, its results are the screener's for the same
filters, and opening it in the builder is :meth:`Preset.query`, filled in.

A preset that does not read is a mistake in the migration that wrote it, and
``tests/integration/test_screener_presets.py`` runs every one a migration
writes. Should one reach a database anyway, the listing leaves it out and logs
why, and reading it is a 500: never a screen of fewer filters than it names.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.portfolio import SortOrder
from app.api.screener import Screen, ScreenSort, query_string, read_filters
from app.core.logging import get_logger
from app.db.models import ScreenerPreset

logger = get_logger(__name__)


class InvalidPresetError(Exception):
    """A preset's row names a filter, a value or a sort the screener does not take."""


@dataclass(frozen=True, slots=True)
class Preset:
    """One preset, its filters parsed."""

    slug: str
    name: str
    explanation: str
    screen: Screen
    sort: ScreenSort
    order: SortOrder

    def query(self) -> str:
        """Its filters, sort and order as ``GET /v1/screener``'s query string,
        without the period: what the builder opens with."""
        return query_string(self.screen, self.sort, self.order)


def parse_preset(row: ScreenerPreset) -> Preset:
    """``row``, read through the vocabulary.

    :raises InvalidPresetError: Naming everything wrong with it at once.
    """
    screen, unread = read_filters(row.query)
    errors = [f"{u.key}: {u.reason}" for u in unread]
    try:
        sort = ScreenSort(row.sort)
    except ValueError:
        errors.append(f"unknown sort {row.sort!r}")
    if errors:
        raise InvalidPresetError(f"preset {row.slug!r}: {'; '.join(errors)}")
    return Preset(
        slug=row.slug,
        name=row.name,
        explanation=row.explanation,
        screen=screen,
        sort=sort,
        order=SortOrder(row.sort_order),
    )


async def list_presets(session: AsyncSession) -> list[Preset]:
    """Every preset that reads, in its position's order."""
    rows: Sequence[ScreenerPreset] = (
        await session.scalars(select(ScreenerPreset).order_by(ScreenerPreset.position))
    ).all()
    presets = []
    for row in rows:
        try:
            presets.append(parse_preset(row))
        except InvalidPresetError as error:
            logger.error("screener.preset_invalid", slug=row.slug, error=str(error))
    return presets


async def get_preset(session: AsyncSession, slug: str) -> Preset | None:
    """The preset ``slug`` names, or None.

    :raises InvalidPresetError: For one whose row does not read.
    """
    row = await session.get(ScreenerPreset, slug)
    return None if row is None else parse_preset(row)
