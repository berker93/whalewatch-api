"""Account emails: verify an address, confirm an email change, tell the old address,
reset a password, say an account was deleted, warn that a monthly quota is 80% spent,
and each step of a failed renewal's dunning.

Two backends, by ``EMAIL_BACKEND``. ``log`` writes each message to the log,
link included, and sends nothing: in development the link is in the API's log
(``make logs s=api``). ``smtp`` sends through ``SMTP_HOST``, with the standard
library's client in a thread, since it blocks.

Sending is best effort. A message that cannot be sent is logged and the request
that asked for it still succeeds: the change it describes is already committed,
and the user can ask again. The links point at whalewatch-web (``WEB_URL``),
which posts the token back to the API.
"""

import asyncio
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import urlencode

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    async def send(self, message: Message) -> None: ...


class LogMailer:
    """Writes each message to the log, and sends nothing."""

    async def send(self, message: Message) -> None:
        logger.info("mail.logged", to=message.to, subject=message.subject, body=message.body)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, message: Message) -> None:
        await asyncio.to_thread(self._send, message)

    def _send(self, message: Message) -> None:
        settings = self._settings
        email = EmailMessage()
        email["From"] = settings.email_from
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.body)
        assert settings.smtp_host is not None  # Settings refuses smtp without it
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
            if settings.smtp_starttls:
                client.starttls()
            if settings.smtp_username and settings.smtp_password:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            client.send_message(email)


def mailer_for(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.email_backend == "smtp" else LogMailer()


async def deliver(mailer: Mailer, message: Message) -> None:
    """Send ``message``, logging rather than raising when it cannot be sent."""
    try:
        await mailer.send(message)
    except (OSError, smtplib.SMTPException) as exc:
        logger.error("mail.failed", to=message.to, subject=message.subject, error=repr(exc))


def link(settings: Settings, path: str, token: str) -> str:
    """``WEB_URL`` + ``path`` + ``?token=``: a page of whalewatch-web."""
    return f"{settings.web_url}{path}?{urlencode({'token': token})}"


# --- the messages ---------------------------------------------------------------

#: The pages of whalewatch-web each link opens. They post the token back.
VERIFY_EMAIL_PATH = "/verify-email"
CONFIRM_EMAIL_PATH = "/account/confirm-email"
RESET_PASSWORD_PATH = "/reset-password"
ACCOUNT_PATH = "/account"


def email_verification(settings: Settings, *, to: str, token: str, hours: int) -> Message:
    return Message(
        to=to,
        subject="Verify your WhaleWatch email",
        body=(
            "Welcome to WhaleWatch. Verify this address to start saving stocks, "
            f"investors and insiders to your watchlist, within {hours} hours:\n"
            f"{link(settings, VERIFY_EMAIL_PATH, token)}\n\n"
            "If you didn't make an account, ignore this message."
        ),
    )


def email_change_confirmation(settings: Settings, *, to: str, token: str, hours: int) -> Message:
    return Message(
        to=to,
        subject="Confirm your new WhaleWatch email",
        body=(
            "Someone asked to change the email on a WhaleWatch account to this address.\n\n"
            f"Confirm it here within {hours} hours:\n{link(settings, CONFIRM_EMAIL_PATH, token)}"
            "\n\nIf it wasn't you, ignore this message and nothing changes."
        ),
    )


def email_change_notice(*, to: str, new_email: str) -> Message:
    return Message(
        to=to,
        subject="Your WhaleWatch email is changing",
        body=(
            f"A change of your WhaleWatch login to {new_email} was asked for. It takes "
            "effect once the new address confirms it.\n\nIf it wasn't you, change your "
            "password now: whoever asked is signed in to your account."
        ),
    )


def password_reset(settings: Settings, *, to: str, token: str, minutes: int) -> Message:
    return Message(
        to=to,
        subject="Reset your WhaleWatch password",
        body=(
            f"Choose a new password here within {minutes} minutes:\n"
            f"{link(settings, RESET_PASSWORD_PATH, token)}\n\n"
            "If you didn't ask for this, ignore this message and your password stays "
            "as it is."
        ),
    )


def account_deleted(*, to: str, purge_after: date, cancelled_plans: Sequence[str] = ()) -> Message:
    """:param cancelled_plans: The paid plans whose subscriptions the deletion cancelled."""
    cancelled = "".join(
        f"Your {plan} subscription is cancelled: it won't renew, and you won't be "
        "charged again.\n\n"
        for plan in cancelled_plans
    )
    return Message(
        to=to,
        subject="Your WhaleWatch account is deleted",
        body=(
            "Your WhaleWatch account was deleted, and you were signed out everywhere. "
            f"What it held is erased for good on {purge_after:%d %B %Y}; until then "
            "WhaleWatch support can restore it on request.\n\n"
            f"{cancelled}"
            "If you didn't delete it, contact support now: whoever did knew your password."
        ),
    )


def quota_warning(
    settings: Settings, *, to: str, what: str, used: int, limit: int, resets_on: date
) -> Message:
    """:param what: The quota, for a person: "API calls"."""
    return Message(
        to=to,
        subject=f"You've used 80% of this month's WhaleWatch {what}",
        body=(
            f"Your WhaleWatch account has made {used:,} of the {limit:,} {what} its plan "
            f"allows this month. Once all {limit:,} are made, further calls are refused "
            f"until the month starts again on {resets_on:%d %B %Y}.\n\n"
            f"Your usage, and the plans with more, are on your account page:\n"
            f"{settings.web_url}{ACCOUNT_PATH}\n\n"
            "This is the only warning this month."
        ),
    )


# --- dunning: a renewal whose payment failed ------------------------------------------
#
# Each links to the account page, which opens Paddle's customer portal: a
# portal link expires within the day, so none is put in an email.


def _day(at: datetime) -> str:
    return f"{at:%d %B %Y} (UTC)"


def payment_failed(settings: Settings, *, to: str, plan: str, grace_ends_at: datetime) -> Message:
    return Message(
        to=to,
        subject="Your WhaleWatch payment didn't go through",
        body=(
            f"We couldn't take the payment to renew your WhaleWatch {plan} plan. "
            "Our payment provider, Paddle, will try the card again over the coming days.\n\n"
            f"You keep {plan} until {_day(grace_ends_at)}. To keep it after that, "
            "update your card from your account page:\n"
            f"{settings.web_url}{ACCOUNT_PATH}\n\n"
            "If the payment hasn't gone through by then, your account moves to Free. "
            "Nothing you've saved is deleted."
        ),
    )


def grace_ending(settings: Settings, *, to: str, plan: str, grace_ends_at: datetime) -> Message:
    return Message(
        to=to,
        subject=f"Your WhaleWatch {plan} plan ends on {grace_ends_at:%d %B}",
        body=(
            f"The payment to renew your WhaleWatch {plan} plan still hasn't gone "
            f"through. On {_day(grace_ends_at)} your account moves to Free.\n\n"
            f"Update your card from your account page to keep {plan}:\n"
            f"{settings.web_url}{ACCOUNT_PATH}"
        ),
    )


def downgraded(settings: Settings, *, to: str, plan: str) -> Message:
    return Message(
        to=to,
        subject="Your WhaleWatch account is now on Free",
        body=(
            f"The payment to renew your WhaleWatch {plan} plan didn't go through, so "
            "your account is on Free from today. Nothing you've saved is deleted.\n\n"
            f"If the payment goes through later, {plan} comes back by itself. You can "
            f"update your card, or choose a plan again, from your account page:\n"
            f"{settings.web_url}{ACCOUNT_PATH}"
        ),
    )


def payment_recovered(settings: Settings, *, to: str, plan: str) -> Message:
    return Message(
        to=to,
        subject="Your WhaleWatch payment went through",
        body=(
            f"Thanks: the payment for your WhaleWatch {plan} plan went through, and "
            f"{plan} is yours again with nothing lost.\n\n"
            f"Your plan and payments are on your account page:\n{settings.web_url}{ACCOUNT_PATH}"
        ),
    )
