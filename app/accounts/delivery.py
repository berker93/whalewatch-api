"""Sending email, and what the provider says happened to it.

Everything that emails goes through :class:`Courier`, which for each message:

1. Asks whether it may go, at that moment and not earlier: not to an address
   a hard bounce or a complaint suppressed (:class:`~app.db.models.EmailSuppression`),
   and, for a broadcast message, only while the user is still on its list
   (:func:`app.accounts.email_lists.is_subscribed`). That is what makes an
   unsubscribe count within seconds: a message queued before it is refused
   after it.
2. Writes its ``email_log`` row, ``sending``, or ``suppressed`` with why.
3. Hands it to the :class:`~app.accounts.mailer.Mailer`, and records the
   answer: ``sent`` with the provider's id, or ``failed``.

Sending is best effort, as it always was. A message that cannot be sent is
logged and the request that asked for it still succeeds: the change it
describes is committed, and the user can ask again.

Then the provider's webhooks (:func:`record_event`) move the row on:
delivered, bounced, complained. A hard bounce suppresses the address for
everything; a complaint suppresses the stream it was made on, so a complaint
about a digest leaves password resets alone, and a complaint about a
password reset stops everything. Either takes effect for the next send.
"""

import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx
from sqlalchemy import case, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.accounts.email_lists import is_subscribed
from app.accounts.mail import Message
from app.accounts.mailer import InactiveRecipientError, LogMailer, Mailer, MailError
from app.accounts.postmark import Event, EventKind, PostmarkMailer
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import (
    STATUS_RANKS,
    EmailLog,
    EmailStatus,
    EmailSuppression,
    Stream,
    SuppressionReason,
    SuppressionScope,
)
from app.db.session import create_engine, create_session_factory

logger = get_logger(__name__)

#: What opens a session: an ``async_sessionmaker``, or in a test, the test's own.
type Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def address_hash(address: str) -> str:
    """What ``email_suppression`` knows an address by: hex SHA-256, trimmed and
    lower-cased, as the login rate limit hashes it."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def mailer_for(settings: Settings) -> Mailer:
    return PostmarkMailer(settings) if settings.email_backend == "postmark" else LogMailer()


#: The scope each stream's complaint, or provider suppression, sets. A stream
#: the API does not know is treated as the account's: the stricter.
_STREAM_SCOPES = {
    Stream.TRANSACTIONAL: SuppressionScope.ALL,
    Stream.BROADCAST: SuppressionScope.BROADCAST,
    None: SuppressionScope.ALL,
}


async def suppression_of(session: AsyncSession, address: str) -> EmailSuppression | None:
    return await session.get(EmailSuppression, address_hash(address))


async def _refusal(session: AsyncSession, message: Message) -> str | None:
    """Why ``message`` may not go now, or None if it may."""
    suppressed = await suppression_of(session, message.to)
    if suppressed is not None and (
        suppressed.scope == SuppressionScope.ALL or message.stream is Stream.BROADCAST
    ):
        return f"address suppressed for {suppressed.scope}: {suppressed.reason}"
    if message.unsubscribe is not None and not await is_subscribed(
        session, message.user_id, message.unsubscribe.mailing_list
    ):
        return f"not on {message.unsubscribe.mailing_list.id}"
    return None


async def suppress(
    session: AsyncSession, address: str, scope: SuppressionScope, reason: SuppressionReason
) -> None:
    """Stop ``scope`` going to ``address``, from the next send. A scope is only
    ever widened: a complaint about a digest after a hard bounce leaves the
    address stopped for everything. Does not commit."""
    table = EmailSuppression.__table__
    statement = insert(EmailSuppression).values(
        address_hash=address_hash(address), scope=scope.value, reason=reason.value
    )
    widens = (statement.excluded.scope == SuppressionScope.ALL.value) & (
        table.c.scope != SuppressionScope.ALL.value
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.address_hash],
            set_={
                "scope": case((widens, statement.excluded.scope), else_=table.c.scope),
                "reason": case((widens, statement.excluded.reason), else_=table.c.reason),
                "updated_at": func.now(),
            },
        )
    )


async def lift(session: AsyncSession, address: str, scope: SuppressionScope) -> bool:
    """Let ``scope`` go to ``address`` again: lifting ``all`` lifts any
    suppression, lifting ``broadcast`` only a broadcast one. Does not commit."""
    statement = delete(EmailSuppression).where(
        EmailSuppression.address_hash == address_hash(address)
    )
    if scope is SuppressionScope.BROADCAST:
        statement = statement.where(EmailSuppression.scope == SuppressionScope.BROADCAST.value)
    result = await session.execute(statement)
    return bool(result.rowcount)  # type: ignore[attr-defined]


def _rank(status: InstrumentedAttribute[str]) -> ColumnElement[int]:
    return case(
        *((status == value.value, literal(rank)) for value, rank in STATUS_RANKS.items()),
        else_=literal(0),
    )


class Courier:
    """Sends messages, one at a time, each through the checks and into the log.

    :param sessions: Opens a session of its own, never a request's: it runs
        after the answer has gone, when the request's session is closed. Each
        is used for one short transaction and closed.
    """

    def __init__(self, settings: Settings, sessions: Sessions, mailer: Mailer) -> None:
        self._settings = settings
        self._sessions = sessions
        self._mailer = mailer

    async def send(self, message: Message) -> EmailStatus | None:
        """Send ``message`` if it may go, and say how far it got: ``sent``,
        ``suppressed`` or ``failed``. Never raises: None when even the log
        could not be written, and so nothing was sent."""
        try:
            return await self._send(message)
        except (SQLAlchemyError, OSError) as exc:
            logger.error(
                "mail.unlogged",
                kind=message.kind.value,
                user_id=message.user_id,
                error=repr(exc),
            )
            return None

    async def _send(self, message: Message) -> EmailStatus:
        async with self._sessions() as session:
            refusal = await _refusal(session, message)
            row = EmailLog(
                user_id=message.user_id,
                kind=message.kind.value,
                stream=message.stream.value,
                status=(EmailStatus.SUPPRESSED if refusal else EmailStatus.SENDING).value,
                detail=refusal,
            )
            session.add(row)
            await session.commit()
        log = logger.bind(email_log_id=row.id, kind=message.kind.value, user_id=message.user_id)
        if refusal is not None:
            log.info("mail.suppressed", reason=refusal)
            return EmailStatus.SUPPRESSED

        provider_id: str | None = None
        detail: str | None = None
        try:
            provider_id = await self._mailer.send(message, reference=str(row.id))
            status = EmailStatus.SENT
        except InactiveRecipientError as exc:
            # The provider stopped this address on this stream and its webhook
            # is lost or yet to come: stop it here too, rather than ask again.
            status, detail = EmailStatus.SUPPRESSED, str(exc)
        except (MailError, httpx.HTTPError, OSError) as exc:
            status, detail = EmailStatus.FAILED, str(exc)

        async with self._sessions() as session:
            if status is EmailStatus.SUPPRESSED:
                await suppress(
                    session, message.to, _STREAM_SCOPES[message.stream], SuppressionReason.PROVIDER
                )
            # Only from ``sending``: a webhook may have moved it on already.
            sending = EmailLog.status == EmailStatus.SENDING.value
            await session.execute(
                update(EmailLog)
                .where(EmailLog.id == row.id)
                .values(
                    provider_id=func.coalesce(EmailLog.provider_id, provider_id),
                    status=case((sending, status.value), else_=EmailLog.status),
                    detail=case((sending, detail), else_=EmailLog.detail),
                    updated_at=func.now(),
                )
            )
            await session.commit()
        if status is EmailStatus.FAILED:
            log.error("mail.failed", error=detail)
        else:
            log.info(f"mail.{status.value}", provider_id=provider_id)
        return status


def courier_for(settings: Settings, sessions: Sessions) -> Courier:
    return Courier(settings, sessions, mailer_for(settings))


@asynccontextmanager
async def courier_scope(settings: Settings) -> AsyncIterator[Courier]:
    """A courier with an engine of its own, for a CLI command or a job."""
    engine = create_engine(settings)
    try:
        yield courier_for(settings, create_session_factory(engine))
    finally:
        await engine.dispose()


# --- the provider's side --------------------------------------------------------------

#: The status each event moves a message to.
_EVENT_STATUSES = {
    EventKind.DELIVERED: EmailStatus.DELIVERED,
    EventKind.SOFT_BOUNCE: EmailStatus.SOFT_BOUNCED,
    EventKind.HARD_BOUNCE: EmailStatus.BOUNCED,
    EventKind.COMPLAINT: EmailStatus.COMPLAINED,
}


@dataclass(frozen=True, slots=True)
class Recorded:
    """What one webhook changed."""

    email_log_id: int | None
    """The message's row; None when no row is the message's."""
    status: EmailStatus | None
    """Its status now, if the event moved it."""
    suppression: SuppressionScope | None
    """What the address is suppressed for now, if the event suppressed it."""
    lifted: bool = False


async def _find(session: AsyncSession, event: Event) -> int | None:
    if event.reference is not None:
        found: int | None = await session.scalar(
            select(EmailLog.id).where(EmailLog.id == event.reference)
        )
        if found is not None:
            return found
    if event.message_id is not None:
        by_provider: int | None = await session.scalar(
            select(EmailLog.id).where(EmailLog.provider_id == event.message_id)
        )
        return by_provider
    return None


async def record_event(session: AsyncSession, event: Event) -> Recorded:
    """Apply one webhook. Safe to apply twice: a status only moves up, and a
    suppression only widens. Does not commit."""
    row_id = await _find(session, event)
    status: EmailStatus | None = None
    target = _EVENT_STATUSES.get(event.kind)
    if row_id is not None and target is not None:
        moved = await session.scalar(
            update(EmailLog)
            .where(EmailLog.id == row_id, _rank(EmailLog.status) < STATUS_RANKS[target])
            .values(
                status=target.value,
                detail=event.detail,
                provider_id=func.coalesce(EmailLog.provider_id, event.message_id),
                updated_at=func.now(),
            )
            .returning(EmailLog.id)
        )
        status = target if moved is not None else None

    scope: SuppressionScope | None = None
    lifted = False
    if event.recipient is not None:
        match event.kind:
            case EventKind.HARD_BOUNCE:
                scope = SuppressionScope.ALL
                await suppress(session, event.recipient, scope, SuppressionReason.HARD_BOUNCE)
            case EventKind.COMPLAINT:
                scope = _STREAM_SCOPES[event.stream]
                await suppress(session, event.recipient, scope, SuppressionReason.COMPLAINT)
            case EventKind.SUPPRESSED:
                assert event.reason is not None  # parse_event sets one
                scope = (
                    SuppressionScope.ALL
                    if event.reason is SuppressionReason.HARD_BOUNCE
                    else _STREAM_SCOPES[event.stream]
                )
                await suppress(session, event.recipient, scope, event.reason)
            case EventKind.REACTIVATED:
                lifted = await lift(session, event.recipient, _STREAM_SCOPES[event.stream])
    return Recorded(email_log_id=row_id, status=status, suppression=scope, lifted=lifted)
