"""position change exits

Exits become rows of ``position_change``: ``exit`` joins the action
vocabulary, and an exit row has to hold nothing. Plus ``filer_period``, a view
of every ``(filer, period)`` published in ``position_snapshot``, which is what
the exit query reads to find each filer's next period. See
:class:`~app.db.models.position_change.ChangeAction` and
:mod:`app.derived.position_change`.

No rows are written here, for 0009's reason. Until the next ``recompute``,
``position_change`` is as 0012 left it, without exits.

The constraint and view SQL are written out rather than imported from the
model, for 0006's reason: they are history, and the model's ``ACTION_CHECK``
already says something different from what this file's downgrade restores.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01 23:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS_BEFORE = "action IN ('new', 'add', 'trim', 'hold')"
ACTIONS_AFTER = "action IN ('new', 'add', 'trim', 'hold', 'exit')"
EXIT_HOLDS_NOTHING = "action <> 'exit' OR (shares = 0 AND value_usd = 0 AND weight_pct = 0)"

# suspect is the same on every row of a period, so bool_or is any one of them.
FILER_PERIOD = """
CREATE VIEW filer_period AS
SELECT filer_id, period_of_report, bool_or(suspect) AS suspect
FROM position_snapshot
GROUP BY filer_id, period_of_report
"""


def upgrade() -> None:
    # Bare names: the ck template prefixes them, and drop_constraint runs the
    # name through it too (see 0003's downgrade).
    op.drop_constraint("action_is_known", "position_change", type_="check")
    op.create_check_constraint("action_is_known", "position_change", ACTIONS_AFTER)
    op.create_check_constraint("an_exit_holds_nothing", "position_change", EXIT_HOLDS_NOTHING)
    op.execute(FILER_PERIOD)


def downgrade() -> None:
    """Deletes the exit rows, which the narrower ``CHECK`` would refuse.

    Nothing is lost: they are derived, and the next ``recompute`` after an
    upgrade writes them again.
    """
    op.execute("DROP VIEW filer_period")
    op.execute("DELETE FROM position_change WHERE action = 'exit'")
    op.drop_constraint("an_exit_holds_nothing", "position_change", type_="check")
    op.drop_constraint("action_is_known", "position_change", type_="check")
    op.create_check_constraint("action_is_known", "position_change", ACTIONS_BEFORE)
