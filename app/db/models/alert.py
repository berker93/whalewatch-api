"""Alerts: what each user asked to be told about, and what they were told.

``alert_rule``
    One thing one user wants to hear about: a kind of event (:class:`AlertKind`)
    and, for most kinds, the filer or stock it is about. Turned off with
    ``is_active`` rather than deleted, so turning it back on keeps its
    settings.
``notification``
    One thing one user was told. Written by the jobs that see events happen,
    never by a request; a request only marks it read.

**``dedupe_key`` is the whole idempotency story.** It names the event, not
the write: ``filer_filed:berkshire:2026Q3`` is Berkshire's 13F for 2026Q3
however many times it is seen. A recompute re-emits every event it ever
emitted, the writer inserts with ``ON CONFLICT DO NOTHING`` on
``uq_notification_user_id_dedupe_key``, and the second emission is nothing:
not a second row, and not an unread mark on a row already read. Two rules of
one user that match one event are one notification for the same reason. It
follows that a notification must never be deleted to dismiss it: a deleted
row is one the next recompute writes again.

**The payload renders the notification on its own.** Every payload carries
:data:`PAYLOAD_KEYS`, null where a key does not apply to its kind (a
``filer_filed`` has no ticker, an ``insider_cluster`` no filer), so the bell
and the email draw it without a second query, and draw it as it was when it
was sent rather than as the data reads now.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, TypedDict

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base
from app.db.models.holding import MONEY


class AlertKind(StrEnum):
    """What an alert is about. Text with a ``CHECK``, by the rule in
    :mod:`app.db.models.enums`: the set is ours and will grow."""

    FILER_FILED = "filer_filed"
    """A followed filer's 13F for a quarter arrived. Targets a filer."""
    FILER_POSITION_CHANGE = "filer_position_change"
    """A followed filer bought, sold, opened or closed a position. Targets a filer."""
    STOCK_OWNERSHIP_CHANGE = "stock_ownership_change"
    """The tracked filers' holding of a followed stock moved. Targets a stock."""
    INSIDER_CLUSTER = "insider_cluster"
    """Several insiders of a followed stock bought together. Targets a stock."""
    SEASON_DIGEST = "season_digest"
    """A 13F season's summary. Targets nothing."""


#: The kinds about one filer: their rule names it in ``filer_id``.
FILER_KINDS: Final = (AlertKind.FILER_FILED, AlertKind.FILER_POSITION_CHANGE)

#: The kinds about one stock: their rule names it in ``security_id``.
SECURITY_KINDS: Final = (AlertKind.STOCK_OWNERSHIP_CHANGE, AlertKind.INSIDER_CLUSTER)


class Channel(StrEnum):
    """Where a rule's notifications go, besides the table."""

    IN_APP = "in_app"
    EMAIL = "email"


#: The keys every payload has, null where one does not apply to its kind.
PAYLOAD_KEYS: Final = ("ticker", "filer_slug", "period", "delta", "confidence", "as_of")


class NotificationPayload(TypedDict):
    """What :attr:`Notification.payload` holds at least. A kind may add keys
    of its own; these are the ones a renderer can count on."""

    ticker: str | None
    """The stock's ticker as of the event, as the API shows it."""
    filer_slug: str | None
    """The filer's :attr:`app.db.models.Filer.slug`, for the link."""
    period: str | None
    """The quarter, as the API labels it: ``2026Q3``."""
    delta: str | None
    """The change, as the API's decimal strings: dollars for a value."""
    confidence: str | None
    """How sure the numbers are: an
    :class:`app.db.models.PriceConfidence` value where a price was estimated."""
    as_of: str | None
    """The ISO date the event is as of: the filing, trade or quarter-end date."""


def _quoted(values: tuple[StrEnum, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


KIND_CHECK: Final = f"kind IN ({_quoted(tuple(AlertKind))})"


class AlertRule(Base):
    """One thing one user wants to hear about."""

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(Text)
    """One :class:`AlertKind` value."""

    # The target. Which of the two is set is the kind's: FILER_KINDS name a
    # filer, SECURITY_KINDS a stock, the digest neither. CASCADE, as on the
    # watchlist: a deleted filer or stock is no longer followed.
    filer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("filer.id", ondelete="CASCADE")
    )
    security_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("security.id", ondelete="CASCADE")
    )

    min_value: Mapped[Decimal | None] = mapped_column(MONEY)
    """Dollars below which an event is not worth telling. Null for every event."""

    channels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text(f"'{{{Channel.IN_APP}}}'")
    )
    """:class:`Channel` values, at least one. ``is_active`` is the off switch."""

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(KIND_CHECK, name="kind_is_known"),
        CheckConstraint(
            f"(filer_id IS NOT NULL) = (kind IN ({_quoted(FILER_KINDS)}))",
            name="filer_kinds_name_a_filer",
        ),
        CheckConstraint(
            f"(security_id IS NOT NULL) = (kind IN ({_quoted(SECURITY_KINDS)}))",
            name="security_kinds_name_a_security",
        ),
        CheckConstraint("min_value > 0", name="min_value_is_positive"),
        CheckConstraint(
            f"cardinality(channels) >= 1 AND channels <@ ARRAY[{_quoted(tuple(Channel))}]",
            name="channels_are_known",
        ),
        # One rule per kind and target per user: a second is an edit of the
        # first. NULLS NOT DISTINCT, or the targets left null would never
        # collide and the digest could be asked for any number of times.
        Index(
            "uq_alert_rule_user_id_kind_target",
            "user_id",
            "kind",
            "filer_id",
            "security_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # Who to tell when something happens to this filer or stock, and what
        # a delete of the filer or stock cascades through.
        Index(
            "ix_alert_rule_filer_id",
            "filer_id",
            "kind",
            postgresql_where=text("filer_id IS NOT NULL"),
        ),
        Index(
            "ix_alert_rule_security_id",
            "security_id",
            "kind",
            postgresql_where=text("security_id IS NOT NULL"),
        ),
    )


class Notification(Base):
    """One thing one user was told, once however often it was emitted."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(Text)
    """One :class:`AlertKind` value."""

    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """A :class:`NotificationPayload`, and whatever the kind adds."""

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the user read it. Null while unread."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    dedupe_key: Mapped[str] = mapped_column(Text)
    """The event, named: the kind, then what makes it this event, ``:``-separated.
    ``filer_filed:berkshire:2026Q3``. Unique per user."""

    __table_args__ = (
        CheckConstraint(KIND_CHECK, name="kind_is_known"),
        CheckConstraint("length(btrim(title)) > 0", name="title_is_titled"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND payload ?& ARRAY[{}]".format(
                ", ".join(f"'{key}'" for key in PAYLOAD_KEYS)
            ),
            name="payload_renders",
        ),
        # Starts with its kind, so two kinds' keys can never collide.
        CheckConstraint(
            "starts_with(dedupe_key, kind || ':') AND length(dedupe_key) > length(kind) + 1",
            name="dedupe_key_names_its_kind",
        ),
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_user_id_dedupe_key"),
        # The unread count, and the unread list newest first.
        Index(
            "ix_notification_unread",
            "user_id",
            desc("created_at"),
            postgresql_where=text("read_at IS NULL"),
        ),
    )
