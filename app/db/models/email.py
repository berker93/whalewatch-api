"""Email delivery: what was sent to whom, and the addresses nothing may be sent to.

``email_log``
    One row per message the site tried to send: its kind, the stream it went
    out on, the provider's id for it, and where it got to. Written before the
    provider is called and moved on by the provider's webhooks. Goes with the
    account when it is purged.
``email_suppression``
    The addresses a hard bounce or a spam complaint has stopped mail to, by a
    SHA-256 of the address rather than the address: it outlives the account
    the address was the login of, so that erasing an account does not start
    mailing someone who said it was spam, and holds nothing readable about
    them while it does.

**Two streams, two reputations.** Every kind of email is sent on one of two
:class:`Stream`\\s, which are two Postmark message streams. ``transactional``
is the account: verify, reset, change, billing. ``broadcast`` is everything a
user can unsubscribe from: alerts and digests, and product or marketing email
once anything sends it. A complaint about a digest suppresses the broadcast
stream only, so a password reset still arrives; a hard bounce suppresses
both, since the mailbox is gone.

See :mod:`app.accounts.delivery` for the sending and the webhooks.
"""

from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Stream(StrEnum):
    """Which of the two sending reputations a message is sent under."""

    TRANSACTIONAL = "transactional"
    """The account's own mail: sent whatever the user chose, never unsubscribable."""
    BROADCAST = "broadcast"
    """Mail the user asked for and may stop: alerts, digests, product, marketing."""


class EmailKind(StrEnum):
    """What a message is. Text with a ``CHECK``: the set will grow."""

    VERIFY_EMAIL = "verify_email"
    REGISTRATION_ATTEMPT = "registration_attempt"
    EMAIL_CHANGE_CONFIRMATION = "email_change_confirmation"
    EMAIL_CHANGE_NOTICE = "email_change_notice"
    PASSWORD_RESET = "password_reset"
    ACCOUNT_DELETED = "account_deleted"
    QUOTA_WARNING = "quota_warning"
    PAYMENT_FAILED = "payment_failed"
    GRACE_ENDING = "grace_ending"
    DOWNGRADED = "downgraded"
    PAYMENT_RECOVERED = "payment_recovered"
    ALERT = "alert"
    """One notification of an alert rule with the email channel, digests included."""


#: The stream each kind goes out on. Only what a user can unsubscribe from is
#: broadcast.
STREAMS: Final[dict[EmailKind, Stream]] = {
    kind: Stream.BROADCAST if kind is EmailKind.ALERT else Stream.TRANSACTIONAL
    for kind in EmailKind
}


class EmailStatus(StrEnum):
    """Where a message got to. Webhooks only ever move a row up :data:`STATUS_RANKS`."""

    SENDING = "sending"
    """Logged, and being handed to the provider."""
    SENT = "sent"
    """The provider accepted it (or, with the log backend, the log has it)."""
    SOFT_BOUNCED = "soft_bounced"
    """A temporary refusal: a full mailbox, a server down. The provider retries."""
    DELIVERED = "delivered"
    """The receiving server took it."""
    BOUNCED = "bounced"
    """Refused for good: the address does not exist. The address is suppressed."""
    COMPLAINED = "complained"
    """The recipient marked it as spam. The address is suppressed for its stream."""
    FAILED = "failed"
    """The provider refused it, or could not be reached: not sent, or not known to be."""
    SUPPRESSED = "suppressed"
    """Not sent: the address is suppressed, or the user unsubscribed from it."""


#: How far along each status is. A webhook moves a row to a status ranked
#: higher than its own and never back: a delivery that arrives after the
#: complaint about it is old news. ``failed`` ranks with ``sending`` so that a
#: send whose answer was lost is put right by the webhook saying it arrived.
STATUS_RANKS: Final[dict[EmailStatus, int]] = {
    EmailStatus.SENDING: 0,
    EmailStatus.FAILED: 0,
    EmailStatus.SENT: 1,
    EmailStatus.SOFT_BOUNCED: 2,
    EmailStatus.DELIVERED: 3,
    EmailStatus.BOUNCED: 4,
    EmailStatus.COMPLAINED: 5,
    EmailStatus.SUPPRESSED: 9,
}


class SuppressionScope(StrEnum):
    """What a suppressed address is no longer sent."""

    ALL = "all"
    """Nothing at all, password resets included: the mailbox does not exist."""
    BROADCAST = "broadcast"
    """Nothing on the broadcast stream; the account's own mail still goes."""


class SuppressionReason(StrEnum):
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    PROVIDER = "provider"
    """The provider suppressed it for a reason of its own, or by hand in its dashboard."""


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class EmailLog(Base):
    """One message the site tried to send."""

    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(Text)
    """One :class:`EmailKind` value."""

    stream: Mapped[str] = mapped_column(Text)
    """One :class:`Stream` value: the kind's, as of the send."""

    provider_id: Mapped[str | None] = mapped_column(Text)
    """The provider's id for the message (Postmark's ``MessageID``). Null until
    it answers, and for the log backend, a refusal, or a suppressed send."""

    status: Mapped[str] = mapped_column(Text)
    """One :class:`EmailStatus` value."""

    detail: Mapped[str | None] = mapped_column(Text)
    """Why, for a failure, a bounce or a suppression, in the provider's words or ours."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"kind IN ({_values(EmailKind)})", name="kind_is_known"),
        CheckConstraint(f"stream IN ({_values(Stream)})", name="stream_is_known"),
        CheckConstraint(f"status IN ({_values(EmailStatus)})", name="status_is_known"),
        # What a webhook looks a message up by, when its metadata has no id.
        Index(
            "uq_email_log_provider_id",
            "provider_id",
            unique=True,
            postgresql_where=text("provider_id IS NOT NULL"),
        ),
        # One user's mail, newest first: the export, and the CASCADE.
        Index("ix_email_log_user_id", "user_id", "id"),
    )


class EmailSuppression(Base):
    """One address that a stream, or everything, is no longer sent to."""

    __tablename__ = "email_suppression"

    address_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    """Hex SHA-256 of the address, trimmed and lower-cased
    (:func:`app.accounts.delivery.address_hash`)."""

    scope: Mapped[str] = mapped_column(Text)
    """One :class:`SuppressionScope` value. Only ever widened, by a later event."""

    reason: Mapped[str] = mapped_column(Text)
    """One :class:`SuppressionReason` value: the event that set the scope."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("address_hash ~ '^[0-9a-f]{64}$'", name="address_hash_is_sha256"),
        CheckConstraint(f"scope IN ({_values(SuppressionScope)})", name="scope_is_known"),
        CheckConstraint(f"reason IN ({_values(SuppressionReason)})", name="reason_is_known"),
    )
