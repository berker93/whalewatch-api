"""Where a user stands on each mailing list, and leaving one.

The lists and their tokens are :mod:`app.accounts.unsubscribe`. An
unsubscribe is honoured from the moment it commits:
:class:`app.accounts.delivery.Courier` asks :func:`is_subscribed` just before
each send, never earlier, so no email queued before it goes after it.
"""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import privacy
from app.accounts.unsubscribe import PURPOSE_LABELS, PURPOSES, ListKind, MailingList, alert_label
from app.db.models import AlertKind, AlertRule, AppUser, Channel, ConsentSource, Filer, Security


@dataclass(frozen=True, slots=True)
class Standing:
    mailing_list: MailingList
    label: str
    """What the list sends, for a person: ``the 13F season digest``."""
    subscribed: bool
    """Whether the next email on it would be sent."""


async def _user(session: AsyncSession, user_id: int) -> AppUser | None:
    user = await session.get(AppUser, user_id)
    return user if user is not None and user.deleted_at is None else None


async def standing(session: AsyncSession, user_id: int, mailing_list: MailingList) -> Standing:
    """Where ``user_id`` stands on the list: subscribed or not, and what it is.
    A list whose account or rule is gone sends nothing, so is left already."""
    user = await _user(session, user_id)
    if mailing_list.kind is ListKind.ALERT_RULE:
        row = (
            await session.execute(
                select(AlertRule, Filer.name, Security.ticker, Security.name)
                .outerjoin(Filer, Filer.id == AlertRule.filer_id)
                .outerjoin(Security, Security.id == AlertRule.security_id)
                .where(AlertRule.id == mailing_list.rule_id, AlertRule.user_id == user_id)
            )
        ).first()
        if row is None:
            return Standing(mailing_list, "an alert that has since been deleted", False)
        rule, filer, ticker, security = row
        label = alert_label(AlertKind(rule.kind), filer or ticker or security)
        subscribed = user is not None and rule.is_active and Channel.EMAIL in rule.channels
        return Standing(mailing_list, label, subscribed)
    purpose = PURPOSES[mailing_list.kind]
    granted = False
    if user is not None:
        granted = privacy.may_email(user, await privacy.standing(session, user_id), purpose)
    return Standing(mailing_list, PURPOSE_LABELS[mailing_list.kind], granted)


async def is_subscribed(session: AsyncSession, user_id: int, mailing_list: MailingList) -> bool:
    return (await standing(session, user_id, mailing_list)).subscribed


async def leave(session: AsyncSession, user_id: int, mailing_list: MailingList) -> Standing:
    """Take ``user_id`` off the list, and say where they stand now. Leaving a
    list already left changes nothing. Does not commit."""
    if mailing_list.kind is ListKind.ALERT_RULE:
        rule = await session.scalar(
            select(AlertRule)
            .where(AlertRule.id == mailing_list.rule_id, AlertRule.user_id == user_id)
            .with_for_update()
        )
        if rule is not None and Channel.EMAIL in rule.channels:
            if rule.channels == [Channel.EMAIL]:
                values: dict[str, object] = {"is_active": False}
            else:
                values = {"channels": [c for c in rule.channels if c != Channel.EMAIL]}
            await session.execute(update(AlertRule).where(AlertRule.id == rule.id).values(values))
            await session.refresh(rule)
    elif await _user(session, user_id) is not None:
        await privacy.choose(
            session,
            user_id,
            {PURPOSES[mailing_list.kind]: False},
            source=ConsentSource.UNSUBSCRIBE,
        )
    return await standing(session, user_id, mailing_list)
