"""What the site may do with a user's data, and how an account is deleted.

Lawful bases
------------
Each purpose rests on one basis (:data:`app.db.models.privacy.BASES`):

``account``, on contract
    The account the user signed up for, and the emails it cannot work
    without. Accepting the terms is the contract; deleting the account ends
    it. Never withdrawn any other way.
``product_email``, on legitimate interest
    Emails about the product the user uses. On from registration, as the
    basis presumes; the user may object, and an objection is a row with
    ``granted`` false.
``marketing_email``, on consent
    Off unless the user ticks a box that is unticked by default and is not
    the terms' box. Withdrawn as easily as given, from the account.

Every change is a new ``consent_record`` row and the latest row per purpose is
where the user stands (:func:`standing`): the history is the evidence. A
sender of either kind of email asks :func:`may_email` first, which
:class:`app.accounts.delivery.Courier` does at the moment it sends. Nothing
sends either yet. An email's unsubscribe link withdraws its purpose with a row
whose source is ``unsubscribe`` (:mod:`app.accounts.unsubscribe`).

:data:`TERMS_VERSION` and :data:`PRIVACY_VERSION` name the documents
whalewatch-web publishes, by the date each took effect. Publishing a new one
there means changing it here, so that what a row says was accepted is what
was shown.

Deleting an account
-------------------
:func:`delete_account` marks the account deleted at once: it signs nobody
in, its address may make a new account, its sessions, links and API keys end. Its rows
stay for :data:`DELETION_GRACE`, in which an operator can undo it
(:func:`restore_account`). Then :func:`purge_due` erases the account and
everything that hangs off it, and the ``account_deletion`` row that recorded
the request records the purge, and outlives it.

A paid subscription is cancelled in Paddle as the account is deleted
(:func:`app.accounts.billing.cancel_for_deleted`), by the caller once the
deletion has committed. Until Paddle has cancelled it, the account is not
purged: its subscription row is the only record of what to cancel.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.api_keys import revoke_all
from app.accounts.billing import still_billing
from app.accounts.sessions import SessionStore, now
from app.core.logging import get_logger
from app.db.models import (
    BASES,
    AccountDeletion,
    AccountToken,
    AlertRule,
    ApiKey,
    AppUser,
    ConsentRecord,
    ConsentSource,
    DeletionRequester,
    EmailLog,
    Notification,
    PlanOverride,
    Purpose,
    SavedFilter,
    SignupRequest,
    Subscription,
    UsageCounter,
    UserActivityDay,
    WatchlistItem,
)

logger = get_logger(__name__)

#: The terms of service in force: the date whalewatch-web's took effect.
TERMS_VERSION: Final = "2026-10-24"

#: The privacy policy in force, likewise.
PRIVACY_VERSION: Final = "2026-10-24"

#: How long a deleted account's rows are kept before they are erased.
DELETION_GRACE: Final = timedelta(days=30)

#: What a user may change from their account: not ``account``, which ends by
#: deleting the account.
CHOOSABLE: Final = (Purpose.PRODUCT_EMAIL, Purpose.MARKETING_EMAIL)


# --- the ledger ---------------------------------------------------------------


def _row(
    user_id: int,
    purpose: Purpose,
    granted: bool,
    source: ConsentSource,
    *,
    terms_version: str | None,
    privacy_version: str | None,
) -> ConsentRecord:
    return ConsentRecord(
        user_id=user_id,
        purpose=purpose.value,
        basis=BASES[purpose].value,
        granted=granted,
        terms_version=terms_version if purpose is Purpose.ACCOUNT else None,
        privacy_version=privacy_version,
        source=source.value,
    )


def record_signup(
    session: AsyncSession,
    user_id: int,
    *,
    marketing_email: bool,
    source: ConsentSource,
    terms_version: str | None = TERMS_VERSION,
    privacy_version: str | None = PRIVACY_VERSION,
) -> None:
    """A new account's three rows: the contract, product email on, and
    marketing email as the box was left.

    :param terms_version: What was accepted; None for an account an operator
        made with no acceptance on file.
    """
    for purpose, granted in (
        (Purpose.ACCOUNT, True),
        (Purpose.PRODUCT_EMAIL, True),
        (Purpose.MARKETING_EMAIL, marketing_email),
    ):
        session.add(
            _row(
                user_id,
                purpose,
                granted,
                source,
                terms_version=terms_version,
                privacy_version=privacy_version,
            )
        )


async def standing(session: AsyncSession, user_id: int) -> dict[Purpose, ConsentRecord]:
    """The latest row per purpose: where the user stands. A purpose with no
    row is missing, which for an email purpose is a no."""
    rows = await session.scalars(
        select(ConsentRecord)
        .where(ConsentRecord.user_id == user_id)
        .order_by(ConsentRecord.purpose, ConsentRecord.id.desc())
        .distinct(ConsentRecord.purpose)
    )
    return {Purpose(row.purpose): row for row in rows}


async def history(session: AsyncSession, user_id: int) -> list[ConsentRecord]:
    """Every row, oldest first: what an export lists."""
    rows = await session.scalars(
        select(ConsentRecord).where(ConsentRecord.user_id == user_id).order_by(ConsentRecord.id)
    )
    return list(rows)


async def choose(
    session: AsyncSession,
    user_id: int,
    choices: dict[Purpose, bool],
    *,
    source: ConsentSource = ConsentSource.PREFERENCES,
) -> dict[Purpose, ConsentRecord]:
    """Record the user's choices, one row per purpose whose answer changed,
    and return where they stand. Nothing is written for an answer the same as
    the last: a form sent twice is one choice.

    :param source: Where they chose: the account's settings, or an email's
        unsubscribe link.
    """
    current = await standing(session, user_id)
    for purpose, granted in choices.items():
        if purpose not in CHOOSABLE:
            raise ValueError(f"{purpose} is not the user's to choose")
        last = current.get(purpose)
        if last is not None and last.granted == granted:
            continue
        session.add(
            _row(
                user_id,
                purpose,
                granted,
                source,
                terms_version=None,
                privacy_version=PRIVACY_VERSION,
            )
        )
    await session.flush()
    return await standing(session, user_id)


def may_email(user: AppUser, standing: dict[Purpose, ConsentRecord], purpose: Purpose) -> bool:
    """Whether an email for ``purpose`` may go to ``user``: a live account
    whose latest row for it grants it."""
    if purpose not in CHOOSABLE:
        raise ValueError(f"account email is sent under the contract: {purpose} needs no check")
    record = standing.get(purpose)
    return user.deleted_at is None and record is not None and record.granted


# --- deletion -------------------------------------------------------------------


async def delete_account(
    session: AsyncSession,
    store: SessionStore,
    user: AppUser,
    *,
    requested_by: DeletionRequester,
) -> AccountDeletion:
    """Mark ``user`` deleted, end their sessions, links and API keys, and
    record the request. Does not commit. A restore brings back none of them.

    Sessions end before the caller commits, as for a password change: a Redis
    that cannot be reached raises :class:`~app.accounts.sessions.SessionStoreError`
    and leaves the account as it was, rather than deleted with its sessions
    still in.
    """
    at = now()
    user.deleted_at = at
    await session.execute(delete(AccountToken).where(AccountToken.user_id == user.id))
    await revoke_all(session, user.id)
    deletion = AccountDeletion(
        user_id=user.id,
        requested_by=requested_by.value,
        requested_at=at,
        purge_after=at + DELETION_GRACE,
    )
    session.add(deletion)
    await session.flush()
    await store.end_all(user.id, keep=None)
    return deletion


class RestoreError(Exception):
    """Why an account cannot be restored, in a sentence for the operator."""


async def restore_account(session: AsyncSession, email: str) -> AppUser:
    """Undo the latest deletion of the account ``email`` was the login of,
    before its purge. Does not commit.

    :raises RestoreError: No deleted account had the address, or its purge has
        happened, or a live account has the address now.
    """
    deletion = await session.scalar(
        select(AccountDeletion)
        .join(AppUser, AppUser.id == AccountDeletion.user_id)
        .where(
            AppUser.email == email,
            AppUser.deleted_at.is_not(None),
            AccountDeletion.purged_at.is_(None),
            AccountDeletion.restored_at.is_(None),
        )
        .order_by(AccountDeletion.requested_at.desc())
        .limit(1)
        .with_for_update(of=AccountDeletion)
    )
    if deletion is None:
        raise RestoreError(f"no deleted account waiting for its purge had the login {email}")
    taken = await session.scalar(
        select(AppUser.id).where(AppUser.email == email, AppUser.deleted_at.is_(None))
    )
    if taken is not None:
        raise RestoreError(f"{email} is the login of another account now")
    user = await session.get(AppUser, deletion.user_id)
    assert user is not None  # joined above
    user.deleted_at = None
    deletion.restored_at = now()
    # Sessions do not autoflush: a purge in the same transaction must see it.
    await session.flush()
    return user


@dataclass(frozen=True, slots=True)
class Purged:
    """One account erased."""

    user_id: int
    erased: dict[str, int]
    """Rows erased, by table."""


#: The tables an account's rows are erased from with it, by its foreign key's
#: CASCADE: counted first, so the audit row says what went.
_CASCADED: Final = (
    WatchlistItem,
    SavedFilter,
    ConsentRecord,
    AccountToken,
    Subscription,
    ApiKey,
    UsageCounter,
    PlanOverride,
    UserActivityDay,
    AlertRule,
    Notification,
    EmailLog,
)


async def purge_due(session: AsyncSession, *, at: datetime | None = None) -> list[Purged]:
    """Erase every account whose deletion's grace has run out, and record each
    purge on its deletion row. Does not commit.

    ``SKIP LOCKED``, so two runs never purge one account twice, nor wait on a
    restore holding its row. An account whose Paddle subscription is not
    cancelled yet waits, logged, for the run after it is.
    """
    at = at or now()
    held = (
        await session.scalars(
            select(AccountDeletion.user_id).where(
                AccountDeletion.purge_after <= at,
                AccountDeletion.purged_at.is_(None),
                AccountDeletion.restored_at.is_(None),
                still_billing(AccountDeletion.user_id),
            )
        )
    ).all()
    if held:
        logger.error("accounts.purge_held_for_billing", user_ids=list(held))
    due = (
        await session.scalars(
            select(AccountDeletion)
            .where(
                AccountDeletion.purge_after <= at,
                AccountDeletion.purged_at.is_(None),
                AccountDeletion.restored_at.is_(None),
                ~still_billing(AccountDeletion.user_id),
            )
            .order_by(AccountDeletion.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    purged = []
    for deletion in due:
        erased: dict[str, int] = {}
        for model in _CASCADED:
            erased[model.__tablename__] = (
                await session.scalar(
                    select(func.count()).select_from(model).where(model.user_id == deletion.user_id)
                )
                or 0
            )
        result = await session.execute(
            delete(AppUser).where(AppUser.id == deletion.user_id, AppUser.deleted_at.is_not(None))
        )
        erased[AppUser.__tablename__] = result.rowcount  # type: ignore[attr-defined]
        await session.execute(
            update(AccountDeletion)
            .where(AccountDeletion.id == deletion.id)
            .values(purged_at=at, erased=erased)
        )
        purged.append(Purged(user_id=deletion.user_id, erased=erased))
    return purged


async def forget_signup_request(session: AsyncSession, email: str) -> SignupRequest | None:
    """Delete the signup request ``email`` made, once it has its account: it
    holds the address and name the account now holds, for no other reason.
    Returns it, for its answers to be carried over."""
    return await session.scalar(
        delete(SignupRequest).where(SignupRequest.email == email).returning(SignupRequest)
    )
