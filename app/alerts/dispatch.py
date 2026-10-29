"""The pairs a rebuild touched, handed to the alerts task once its transaction commits.

``recompute`` runs inside its caller's transaction: the load of a filing,
``recompute``'s own job, ``detect-splits``. It cannot commit, and nothing it
rebuilt is anyone's news until the caller does. So it calls :func:`after_commit`,
which keeps the pairs on the session, and a listener on the session's commit
sends them to the ``emit_alerts`` task. A rollback forgets them. Every caller,
present and future, gets this by calling ``recompute``, and none has to
remember it.

Never in the way of ingestion. The send is one message to the broker, without
retries, and a broker that cannot take it is a warning in the log: the load has
committed and stays committed. Those pairs are then not told about until a
rebuild touches them again, which ``recompute`` by hand, for a filer and a
period, does. Matching rules, writing notifications and sending email all
happen in the task, on the compute queue, never in the transaction that loads
a filing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.derived.scope import Pair

logger = get_logger(__name__)

#: Where a session keeps what its transaction has touched, in ``Session.info``.
_PENDING: Final = "alerts.pending"


@dataclass(slots=True)
class _Pending:
    pairs: set[Pair] = field(default_factory=set)
    everything: bool = False


def after_commit(session: AsyncSession, pairs: frozenset[Pair] | None) -> None:
    """Send ``pairs`` to the alerts task when ``session``'s transaction commits;
    ``None`` for every pair, as ``recompute --all`` rebuilds. Called more than
    once in one transaction, the pairs add up and go in one message."""
    sync = session.sync_session
    pending = sync.info.get(_PENDING)
    if pending is None:
        pending = sync.info[_PENDING] = _Pending()
        if not event.contains(sync, "after_commit", _committed):
            event.listen(sync, "after_commit", _committed)
            event.listen(sync, "after_rollback", _rolled_back)
    if pairs is None:
        pending.everything = True
    else:
        pending.pairs |= pairs


def _committed(session: Session) -> None:
    pending: _Pending | None = session.info.pop(_PENDING, None)
    if pending is None or not (pending.everything or pending.pairs):
        return
    pairs = None if pending.everything else frozenset(pending.pairs)
    try:
        enqueue(pairs)
    except Exception as failure:
        # The message, not the traceback: a refused connection's says no more.
        logger.warning(
            "alerts.enqueue_failed",
            error=f"{type(failure).__name__}: {failure}",
            pairs=None if pairs is None else len(pairs),
        )


def _rolled_back(session: Session) -> None:
    session.info.pop(_PENDING, None)


def enqueue(pairs: frozenset[Pair] | None) -> None:
    """One ``emit_alerts`` message for ``pairs``, or for every pair. Replaced in tests."""
    # Imported here: the tasks import the jobs that rebuild, which import this.
    from app.jobs.tasks import emit_alerts

    emit_alerts.apply_async(kwargs=message(pairs), retry=False)
    logger.info("alerts.enqueued", pairs=None if pairs is None else len(pairs))


def message(pairs: frozenset[Pair] | None) -> dict[str, Any]:
    """The task's arguments: JSON, so dates as ISO strings."""
    if pairs is None:
        return {"everything": True}
    return {"pairs": [[filer_id, period.isoformat()] for filer_id, period in sorted(pairs)]}
