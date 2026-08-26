"""Enumerations that exist in the database as types, not as conventions.

There is exactly one of these today, and the bar for adding another is high. A
native Postgres enum buys a check constraint that cannot be bypassed and a
column that reads as itself in ``psql``; it costs a migration for every new
value (``ALTER TYPE ... ADD VALUE``, which before Postgres 12 could not run in a
transaction and still cannot be reversed — there is no ``DROP VALUE``).

So: enum when the set is closed by someone else's rules and changing it is a
schema event. Text plus a ``CHECK`` when the set is ours and might grow. That is
why ``amendment_kind`` is an enum — the two values are the two boxes on EDGAR's
cover page — and why ``sshprnamt_type``, ``put_call`` and ``resolution_source``
are text with check constraints on the tables that use them.
"""

from enum import StrEnum


class AmendmentKind(StrEnum):
    """What a ``13F-HR/A`` claims to do to the period it amends.

    This is the single most consequential field on an amendment, and getting it
    backwards is silent. A **restatement** replaces the period's holdings
    wholesale: load it alongside the original and every position is counted
    twice. A **new holdings** amendment adds rows the original omitted —
    typically positions released from confidential treatment — and treating
    *that* as a restatement throws away everything the original reported.

    Both failures produce a portfolio that looks entirely plausible. Neither
    raises anything.

    The values are the normalised form of EDGAR's own ``<amendmentType>``:
    ``RESTATEMENT`` and ``NEW HOLDINGS``. Normalised rather than raw because the
    space in the second one makes every SQL literal in the codebase quotable but
    ugly, and because these two are a closed set — unlike ``transaction_code``
    on Form 4, which is stored exactly as filed precisely because it is not.

    ``None`` on the column is the third state and the common one: the filing is
    not an amendment at all.
    """

    RESTATEMENT = "restatement"
    NEW_HOLDINGS = "new_holdings"
