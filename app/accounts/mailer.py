"""What hands a message to a mail provider, and how it says it could not.

:class:`Mailer` is the provider seam: :class:`app.accounts.postmark.PostmarkMailer`
in staging and production, :class:`LogMailer` in development, an outbox in
tests. A mailer only sends. Whether a message may be sent, and the record of
it, are :class:`app.accounts.delivery.Courier`'s, which is what everything
else calls.
"""

from typing import TYPE_CHECKING, Protocol

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.accounts.mail import Message

logger = get_logger(__name__)


class MailError(Exception):
    """The provider did not take the message, or did not say whether it did."""


class InactiveRecipientError(MailError):
    """The provider has stopped sending to the address on the message's stream,
    after a hard bounce or a complaint there: nothing was sent."""


class Mailer(Protocol):
    async def send(self, message: "Message", *, reference: str) -> str | None:
        """Hand ``message`` over, and return the provider's id for it.

        :param reference: The ``email_log`` row's id, which the provider is
            given as metadata and hands back in each webhook about the message.
        :raises MailError: Not sent, or not known to be.
        """
        ...


class LogMailer:
    """Writes each message to the log, text body and links included, and sends nothing."""

    async def send(self, message: "Message", *, reference: str) -> str | None:
        logger.info(
            "mail.logged",
            to=message.to,
            kind=message.kind.value,
            subject=message.subject,
            body=message.body,
            reference=reference,
        )
        return None
