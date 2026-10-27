"""What the site emails: verify an address, tell an account someone registered with its
address, confirm an email change, tell the old address,
reset a password, say an account was deleted, warn that a monthly quota is 80% spent,
each step of a failed renewal's dunning, and an alert's notification.

Each builder returns a :class:`Message`, written once as blocks and drawn as
both an HTML and a plain-text body (:mod:`app.accounts.email_layout`). Its
:class:`~app.db.models.EmailKind` fixes its stream: everything here but
:func:`alert` is the account's own mail, on the transactional stream, with no
unsubscribe link, since a password reset is not something to opt out of. An
alert is on the broadcast stream, carries its list's unsubscribe link in the
footer and the one-click ``List-Unsubscribe`` header, and is only sent while
the user is still on that list.

Sending is :mod:`app.accounts.delivery`'s. The links point at whalewatch-web
(``WEB_URL``), which posts the token back to the API; the one-click header
points at the API itself (``API_URL``), which a mail provider posts to.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import quote, urlencode

from app.accounts.email_layout import Block, Button, FooterLine, FooterLink, html_body, text_body
from app.accounts.unsubscribe import MailingList, alert_label, token_for
from app.core.config import Settings
from app.db.models import STREAMS, AlertKind, EmailKind, Stream


@dataclass(frozen=True, slots=True)
class Unsubscribe:
    """The list a broadcast message is on, and the two ways off it."""

    mailing_list: MailingList
    page_url: str
    """The footer's link: a whalewatch-web page that says what it unsubscribes
    from and asks once. A link scanner that follows it changes nothing."""
    one_click_url: str
    """The ``List-Unsubscribe`` header's: the API, which a POST unsubscribes (RFC 8058)."""


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    user_id: int
    """The account it is about, which ``email_log`` files it under. Not always
    the address's: an email change's confirmation goes to the new address."""
    kind: EmailKind
    subject: str
    body: str
    """The plain-text body."""
    html: str
    unsubscribe: Unsubscribe | None = None
    """Set on, and only on, a broadcast message."""

    def __post_init__(self) -> None:
        if (self.unsubscribe is not None) != (self.stream is Stream.BROADCAST):
            raise ValueError(f"{self.kind} is {self.stream}: an unsubscribe link goes with that")

    @property
    def stream(self) -> Stream:
        return STREAMS[self.kind]


def link(settings: Settings, path: str, token: str) -> str:
    """``WEB_URL`` + ``path`` + ``?token=``: a page of whalewatch-web."""
    return f"{settings.web_url}{path}?{urlencode({'token': token})}"


# --- the messages ---------------------------------------------------------------

#: The pages of whalewatch-web each link opens. They post the token back.
VERIFY_EMAIL_PATH = "/verify-email"
CONFIRM_EMAIL_PATH = "/account/confirm-email"
RESET_PASSWORD_PATH = "/reset-password"
ACCOUNT_PATH = "/account"
UNSUBSCRIBE_PATH = "/unsubscribe"

#: The API route the one-click header posts to (app/api/routers/email.py).
ONE_CLICK_PATH = "/v1/email/unsubscribe"

#: The footer of the account's own mail, which no setting stops.
_ACCOUNT_FOOTER: Final[Sequence[FooterLine]] = (
    "This email is about your WhaleWatch account, so it is sent whatever your email settings say.",
)


def _message(
    settings: Settings,
    *,
    to: str,
    user_id: int,
    kind: EmailKind,
    subject: str,
    blocks: Sequence[Block],
    unsubscribe: Unsubscribe | None = None,
    why: str | None = None,
) -> Message:
    """:param why: For a broadcast message, why the user gets it: the footer's
    first line, before the unsubscribe link."""
    footer: Sequence[FooterLine] = _ACCOUNT_FOOTER
    if unsubscribe is not None:
        footer = (
            f"You get this because {why}.",
            FooterLink("Unsubscribe from these emails", unsubscribe.page_url),
            FooterLink("Change what WhaleWatch emails you", f"{settings.web_url}{ACCOUNT_PATH}"),
        )
    first = next((b for b in blocks if isinstance(b, str)), subject)
    return Message(
        to=to,
        user_id=user_id,
        kind=kind,
        subject=subject,
        body=text_body(blocks, footer),
        html=html_body(subject, blocks, footer, preheader=first),
        unsubscribe=unsubscribe,
    )


def email_verification(
    settings: Settings, *, to: str, user_id: int, token: str, hours: int
) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.VERIFY_EMAIL,
        subject="Verify your WhaleWatch email",
        blocks=(
            "Welcome to WhaleWatch. Verify this address to start saving stocks, "
            f"investors and insiders to your watchlist. The link works for {hours} hours.",
            Button("Verify your email", link(settings, VERIFY_EMAIL_PATH, token)),
            "If you didn't make an account, ignore this message.",
        ),
    )


def registration_attempt(settings: Settings, *, to: str, user_id: int) -> Message:
    """To an account's own address, when someone registers with it: registering
    answers the same either way, so only the inbox hears which happened."""
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.REGISTRATION_ATTEMPT,
        subject="Someone tried to make a WhaleWatch account with your email",
        blocks=(
            "Someone asked to make a WhaleWatch account with this address, which "
            "already has one. Nothing was changed, and no new account was made.",
            "If it was you, log in instead. If you've forgotten your password, ask for "
            "a reset link from the login page.",
            Button("Log in", settings.web_url or ""),
            "If it wasn't you, ignore this message: your account is as it was.",
        ),
    )


def email_change_confirmation(
    settings: Settings, *, to: str, user_id: int, token: str, hours: int
) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.EMAIL_CHANGE_CONFIRMATION,
        subject="Confirm your new WhaleWatch email",
        blocks=(
            "Someone asked to change the email on a WhaleWatch account to this address. "
            f"Confirm it within {hours} hours.",
            Button("Confirm this email", link(settings, CONFIRM_EMAIL_PATH, token)),
            "If it wasn't you, ignore this message and nothing changes.",
        ),
    )


def email_change_notice(settings: Settings, *, to: str, user_id: int, new_email: str) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.EMAIL_CHANGE_NOTICE,
        subject="Your WhaleWatch email is changing",
        blocks=(
            f"A change of your WhaleWatch login to {new_email} was asked for. It takes "
            "effect once the new address confirms it.",
            "If it wasn't you, change your password now: whoever asked is signed in to "
            "your account.",
        ),
    )


def password_reset(
    settings: Settings, *, to: str, user_id: int, token: str, minutes: int
) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.PASSWORD_RESET,
        subject="Reset your WhaleWatch password",
        blocks=(
            f"Choose a new password within {minutes} minutes.",
            Button("Choose a new password", link(settings, RESET_PASSWORD_PATH, token)),
            "If you didn't ask for this, ignore this message and your password stays as it is.",
        ),
    )


def account_deleted(
    settings: Settings,
    *,
    to: str,
    user_id: int,
    purge_after: date,
    cancelled_plans: Sequence[str] = (),
) -> Message:
    """:param cancelled_plans: The paid plans whose subscriptions the deletion cancelled."""
    cancelled = [
        f"Your {plan} subscription is cancelled: it won't renew, and you won't be charged again."
        for plan in cancelled_plans
    ]
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.ACCOUNT_DELETED,
        subject="Your WhaleWatch account is deleted",
        blocks=(
            "Your WhaleWatch account was deleted, and you were signed out everywhere. "
            f"What it held is erased for good on {purge_after:%d %B %Y}; until then "
            "WhaleWatch support can restore it on request.",
            *cancelled,
            "If you didn't delete it, contact support now: whoever did knew your password.",
        ),
    )


def quota_warning(
    settings: Settings,
    *,
    to: str,
    user_id: int,
    what: str,
    used: int,
    limit: int,
    resets_on: date,
) -> Message:
    """:param what: The quota, for a person: "API calls"."""
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.QUOTA_WARNING,
        subject=f"You've used 80% of this month's WhaleWatch {what}",
        blocks=(
            f"Your WhaleWatch account has made {used:,} of the {limit:,} {what} its plan "
            f"allows this month. Once all {limit:,} are made, further calls are refused "
            f"until the month starts again on {resets_on:%d %B %Y}.",
            "Your usage, and the plans with more, are on your account page.",
            Button("Open your account", f"{settings.web_url}{ACCOUNT_PATH}"),
            "This is the only warning this month.",
        ),
    )


# --- dunning: a renewal whose payment failed ------------------------------------------
#
# Each links to the account page, which opens Paddle's customer portal: a
# portal link expires within the day, so none is put in an email.


def _day(at: datetime) -> str:
    return f"{at:%d %B %Y} (UTC)"


def _account_button(settings: Settings) -> Button:
    return Button("Open your account", f"{settings.web_url}{ACCOUNT_PATH}")


def payment_failed(
    settings: Settings, *, to: str, user_id: int, plan: str, grace_ends_at: datetime
) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.PAYMENT_FAILED,
        subject="Your WhaleWatch payment didn't go through",
        blocks=(
            f"We couldn't take the payment to renew your WhaleWatch {plan} plan. "
            "Our payment provider, Paddle, will try the card again over the coming days.",
            f"You keep {plan} until {_day(grace_ends_at)}. To keep it after that, "
            "update your card from your account page.",
            _account_button(settings),
            "If the payment hasn't gone through by then, your account moves to Free. "
            "Nothing you've saved is deleted.",
        ),
    )


def grace_ending(
    settings: Settings, *, to: str, user_id: int, plan: str, grace_ends_at: datetime
) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.GRACE_ENDING,
        subject=f"Your WhaleWatch {plan} plan ends on {grace_ends_at:%d %B}",
        blocks=(
            f"The payment to renew your WhaleWatch {plan} plan still hasn't gone "
            f"through. On {_day(grace_ends_at)} your account moves to Free.",
            f"Update your card from your account page to keep {plan}.",
            _account_button(settings),
        ),
    )


def downgraded(settings: Settings, *, to: str, user_id: int, plan: str) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.DOWNGRADED,
        subject="Your WhaleWatch account is now on Free",
        blocks=(
            f"The payment to renew your WhaleWatch {plan} plan didn't go through, so "
            "your account is on Free from today. Nothing you've saved is deleted.",
            f"If the payment goes through later, {plan} comes back by itself. You can "
            "update your card, or choose a plan again, from your account page.",
            _account_button(settings),
        ),
    )


def payment_recovered(settings: Settings, *, to: str, user_id: int, plan: str) -> Message:
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.PAYMENT_RECOVERED,
        subject="Your WhaleWatch payment went through",
        blocks=(
            f"Thanks: the payment for your WhaleWatch {plan} plan went through, and "
            f"{plan} is yours again with nothing lost.",
            "Your plan and payments are on your account page.",
            _account_button(settings),
        ),
    )


# --- alerts: the broadcast stream -----------------------------------------------------


def unsubscribe_for(settings: Settings, user_id: int, mailing_list: MailingList) -> Unsubscribe:
    token = token_for(settings, user_id, mailing_list)
    return Unsubscribe(
        mailing_list=mailing_list,
        page_url=link(settings, UNSUBSCRIBE_PATH, token),
        one_click_url=f"{settings.api_url}{ONE_CLICK_PATH}?{urlencode({'token': token})}",
    )


def notification_path(kind: AlertKind, payload: Mapping[str, Any]) -> str | None:
    """The whalewatch-web path a notification opens, at the quarter it is
    about: the web's ``notificationPath``, which the bell uses, kept in step."""
    slug, ticker, period = payload.get("filer_slug"), payload.get("ticker"), payload.get("period")

    def at_period(path: str) -> str:
        return f"{path}?{urlencode({'period': period})}" if period else path

    match kind:
        case AlertKind.FILER_FILED | AlertKind.FILER_POSITION_CHANGE:
            return at_period(f"/investors/{quote(slug, safe='')}") if slug else None
        case AlertKind.STOCK_OWNERSHIP_CHANGE:
            return at_period(f"/stocks/{quote(ticker, safe='')}") if ticker else None
        case AlertKind.INSIDER_CLUSTER:
            return f"/insiders?{urlencode({'ticker': ticker})}" if ticker else "/insiders"
        case AlertKind.SEASON_DIGEST:
            return at_period("/overview")


def alert(
    settings: Settings,
    *,
    to: str,
    user_id: int,
    rule_id: int,
    kind: AlertKind,
    title: str,
    body: str,
    payload: Mapping[str, Any],
) -> Message:
    """One notification of rule ``rule_id``, by email: what the bell shows,
    with a button to the view it is about. Takes a notification's fields
    rather than the row, so it is drawn as it was written.

    The sender decides the rule wants email (its channels). Whether the user
    is still on the rule's list when it goes is the courier's to check, then.
    """
    path = notification_path(kind, payload)
    blocks: list[Block] = [body] if body.strip() else []
    if path is not None:
        blocks.append(Button("Open in WhaleWatch", f"{settings.web_url}{path}"))
    return _message(
        settings,
        to=to,
        user_id=user_id,
        kind=EmailKind.ALERT,
        subject=title,
        blocks=blocks,
        unsubscribe=unsubscribe_for(settings, user_id, MailingList.for_rule(rule_id)),
        why=f"you asked to be emailed about {alert_label(kind, None)}",
    )
