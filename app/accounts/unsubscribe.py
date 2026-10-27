"""Unsubscribing: the list each broadcast email is on, and the signed links that leave it.

A list is what one unsubscribe stops (:class:`MailingList`):

``alert_rule:<id>``
    One alert rule's email: each notification of a rule whose channels include
    ``email``, the season digest included. Leaving takes ``email`` out of the
    rule's channels and the bell keeps it; a rule that only emailed is turned
    off instead (``is_active``), so it does not start ringing a bell nobody
    asked for, and turning it back on brings its email back with it.
``product_email``, ``marketing_email``
    A consent purpose (:mod:`app.accounts.privacy`). Leaving writes a
    ``consent_record`` row withdrawing it, from source ``unsubscribe``.
    Nothing sends either yet; the lists exist so the first sender has them.

Every list is on the broadcast stream. The account's own mail (verify, reset,
billing) is on no list and has no unsubscribe link: leaving a digest cannot
stop a password reset, because nothing connects the two.

The token
---------
``<user id>.<list>.<signature>``: an HMAC-SHA256 of ``unsubscribe:<user
id>.<list>`` under ``EMAIL_UNSUBSCRIBE_SECRET``, base64url without padding.
It never expires: a link in a digest from two years ago must still work,
which is what RFC 8058 and the big mailbox providers' sender rules expect. It
is not tied to an address, so the link in mail sent before an email change
still unsubscribes the account. Anyone holding it can unsubscribe that
account from that one list, and do nothing else.

Where a user stands on a list, and leaving it, are
:mod:`app.accounts.email_lists`: this module is the links alone, and imports
nothing that sends.
"""

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.config import Settings
from app.db.models import FILER_KINDS, AlertKind, Purpose


class ListKind(StrEnum):
    ALERT_RULE = "alert_rule"
    PRODUCT_EMAIL = "product_email"
    MARKETING_EMAIL = "marketing_email"


#: The consent purpose each purpose list is.
PURPOSES: Final = {
    ListKind.PRODUCT_EMAIL: Purpose.PRODUCT_EMAIL,
    ListKind.MARKETING_EMAIL: Purpose.MARKETING_EMAIL,
}

_LIST_ID: Final = re.compile(r"(?P<kind>[a-z_]+)(?::(?P<rule>[1-9][0-9]{0,17}))?")


@dataclass(frozen=True, slots=True)
class MailingList:
    kind: ListKind
    rule_id: int | None = None
    """The alert rule, for an ``alert_rule`` list; None for the others."""

    def __post_init__(self) -> None:
        if (self.rule_id is not None) != (self.kind is ListKind.ALERT_RULE):
            raise ValueError("an alert_rule list names its rule, and only it does")

    @classmethod
    def for_rule(cls, rule_id: int) -> "MailingList":
        return cls(ListKind.ALERT_RULE, rule_id)

    @classmethod
    def parse(cls, text: str) -> "MailingList":
        """:raises ValueError: Not a list's id."""
        match = _LIST_ID.fullmatch(text)
        if match is None:
            raise ValueError(f"not a list: {text!r}")
        rule = match["rule"]
        return cls(ListKind(match["kind"]), int(rule) if rule else None)

    @property
    def id(self) -> str:
        """``alert_rule:42``, ``product_email``: as it is in a token."""
        return self.kind.value if self.rule_id is None else f"{self.kind}:{self.rule_id}"


# --- tokens -----------------------------------------------------------------------


class InvalidTokenError(ValueError):
    """Not a token this site signed."""


def _signature(settings: Settings, payload: str) -> str:
    assert settings.email_unsubscribe_secret is not None  # Settings fills it in
    digest = hmac.new(
        settings.email_unsubscribe_secret.get_secret_value().encode(),
        f"unsubscribe:{payload}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def token_for(settings: Settings, user_id: int, mailing_list: MailingList) -> str:
    payload = f"{user_id}.{mailing_list.id}"
    return f"{payload}.{_signature(settings, payload)}"


def read_token(settings: Settings, token: str) -> tuple[int, MailingList]:
    """The user and the list ``token`` unsubscribes.

    :raises InvalidTokenError: Malformed, or not signed by this site's secret.
    """
    payload, _, signature = token.rpartition(".")
    user, _, list_id = payload.partition(".")
    if not (user.isdigit() and len(user) <= 18 and list_id and signature):
        raise InvalidTokenError("not an unsubscribe token")
    if not hmac.compare_digest(signature, _signature(settings, payload)):
        raise InvalidTokenError("not signed by this site")
    try:
        return int(user), MailingList.parse(list_id)
    except ValueError:
        # Signed, so ours: a list this code no longer knows.
        raise InvalidTokenError("a list that no longer exists") from None


# --- where a user stands --------------------------------------------------------


#: What each kind of alert is about, for a person: "{} Berkshire Hathaway".
_ALERT_LABELS: Final = {
    AlertKind.FILER_FILED: "new 13F filings by {}",
    AlertKind.FILER_POSITION_CHANGE: "position changes by {}",
    AlertKind.STOCK_OWNERSHIP_CHANGE: "changes in who owns {}",
    AlertKind.INSIDER_CLUSTER: "insiders buying {} together",
    AlertKind.SEASON_DIGEST: "the 13F season digest",
}

PURPOSE_LABELS: Final = {
    ListKind.PRODUCT_EMAIL: "news about WhaleWatch",
    ListKind.MARKETING_EMAIL: "offers from WhaleWatch",
}


def alert_label(kind: AlertKind, target: str | None) -> str:
    """``new 13F filings by Berkshire Hathaway``; with no target, a followed one."""
    if kind is AlertKind.SEASON_DIGEST:
        return _ALERT_LABELS[kind]
    if target is None:
        target = "an investor you follow" if kind in FILER_KINDS else "a stock you follow"
    return _ALERT_LABELS[kind].format(target)
