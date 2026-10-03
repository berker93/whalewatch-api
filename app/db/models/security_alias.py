"""Other names a security is looked up by: former tickers and other spellings.

``security.ticker`` holds one ticker, the one resolution last found. A client
asking for a stock may have another in mind: the ticker before a rename
(``FB`` for what is now ``META``), or a share class spelled the way its own
data source spells it (``BRK.B``, ``BRK-B``, ``BRK/B``). Each of those is a row
here, pointing at the security it means, and ``/v1/stocks/{ticker}`` checks
them after ``security.ticker``.

An alias is not unique. Tickers are recycled between companies, so the same
letters can name one security until a delisting and another after it. The
lookup takes the one held most recently (see :mod:`app.api.routers.stocks`).
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class SecurityAlias(Base):
    """One more name for one security."""

    __tablename__ = "security_alias"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    security_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )
    """Cascades: an alias of a security that no longer exists names nothing."""

    alias: Mapped[str] = mapped_column(Text)
    """As it was found. Matched ignoring case, so ``brk.b`` finds ``BRK.B``."""

    source: Mapped[str | None] = mapped_column(Text)
    """Where the alias came from, for :attr:`Security.resolution_source`'s reason:
    a wrong one is found again by asking which others came from the same place."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("alias <> ''", name="alias_is_not_empty"),
        CheckConstraint(
            "source IS NULL OR source IN ('openfigi', '13f_column', 'manual')",
            name="source_is_known",
        ),
        # The lookup, an equality on upper(alias), and the 404's suggestions,
        # a prefix of it. text_pattern_ops serves both under any collation;
        # the default operator class serves a prefix LIKE only under C.
        # Unique with security_id: one security, one spelling, once.
        Index(
            "uq_security_alias_upper_alias_security_id",
            func.upper(alias).label("upper_alias"),
            "security_id",
            unique=True,
            postgresql_ops={"upper_alias": "text_pattern_ops"},
        ),
    )
