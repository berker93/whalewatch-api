"""A person's word on what a PTR asset is: the manual override, as SEC-3's
``resolve-cusip`` is for a CUSIP.

Keyed on the asset as filed (:func:`~app.ingestion.congress.assets.asset_key`:
the code, the ticker and the folded name), so one override covers every line
that names the asset that way — in every filing, by any member, those to come
included. ``resolve-congress-assets`` reads it before anything else, and
nothing automatic overrides it.

Either a stock (or an option on one), with the ``security`` it is, or a class
that has none: ``Taylor TX ISD BE/R/`` is a municipal bond whatever the words
in its name did not say. Made in the console
(``/admin/congress/assets/overrides``) or with ``congress-asset-override``;
either is logged in ``admin_action``.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.ingestion.congress.assets import WITH_SECURITY, AssetClass


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


#: What a person may say a line is: any class but ``unknown``, which is a
#: run's word for not knowing.
OVERRIDE_CLASSES = tuple(kind.value for kind in AssetClass if kind is not AssetClass.UNKNOWN)


class CongressAssetOverride(Base):
    """What every line under one asset key is."""

    __tablename__ = "congress_asset_override"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    asset_key: Mapped[str] = mapped_column(Text, unique=True)
    """:func:`~app.ingestion.congress.assets.asset_key`, as on the lines."""

    asset_class: Mapped[str] = mapped_column(Text)
    """One :class:`~app.ingestion.congress.assets.AssetClass` value, not ``unknown``."""

    security_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="RESTRICT")
    )
    """The stock, for ``equity`` and ``stock_option``; null for every other class."""

    note: Mapped[str | None] = mapped_column(Text)
    """Why, for the next person."""

    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="RESTRICT")
    )
    """The admin who last set it, from the console; null from the CLI."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("asset_class", OVERRIDE_CLASSES), name="asset_class_is_known"),
        CheckConstraint(
            f"({_in('asset_class', tuple(kind.value for kind in WITH_SECURITY))}) "
            "= (security_id IS NOT NULL)",
            name="a_stock_names_its_security",
        ),
        CheckConstraint("asset_key <> ''", name="asset_key_is_not_empty"),
    )
