"""search trigram indexes

``GET /v1/search`` matches what was typed against four columns with
``pg_trgm``'s ``%`` and ``<%`` operators, and against their starts with
``ILIKE 'q%'``. A ``gin_trgm_ops`` index serves all three. ``security.name``
has had one since 0017; this adds the other three.

* ``security USING gin (ticker gin_trgm_ops)``. The one that matters. The
  search ORs its conditions on ``name`` and ``ticker`` together, and one arm
  no index can serve turns the whole ``OR`` into a sequential scan of every
  security, with the trigram operators run on each: 50 ms on the dev database
  for a typo, against 0.5 ms as a ``BitmapOr`` of index scans. Every ticker is
  null until enrichment runs, so for now it is a small, empty index.
* ``filer USING gin (display_name gin_trgm_ops)`` and ``(manager_name ...)``.
  The AC asks for an index on every searched column. On 100 filers the planner
  reads the table instead, and is right to: the whole of it is two pages. They
  start mattering if the tracked universe grows by a couple of orders of
  magnitude, and cost nothing until then.

And ``COST 100`` on the trigram functions, from the ``COST 1`` the extension
declares them at, which is what an integer comparison costs. Measured, a call
is about 1.5 µs, two hundred times that. Believing them nearly free, the
planner reads all of ``security`` and runs the operators on every row rather
than ask the index: 72 ms for ``jp morgan``, against 1.7 ms by the index. At 20
and above it asks the index for every search tried; 100 is the measured cost,
rounded down. Two caveats, both because these are the extension's functions,
not ours:

* ``pg_dump`` does not dump a property changed on an extension's member, and
  ``ALTER EXTENSION pg_trgm UPDATE`` may reset it. After a restore or an
  upgrade, run this migration's ``upgrade()`` statements again. The search's
  integration tests check the cost, so a test database built without it fails.
* It needs to be run by the extension's owner. In compose that is the
  migrating user, who created it; on a managed Postgres it may not be.

``ANALYZE security`` afterwards, as in 0018, so the planner knows from the
start how few tickers there are.

Built without ``CONCURRENTLY``, for 0017's reason.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-03 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (index, table, column)
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_security_ticker_trgm", "security", "ticker"),
    ("ix_filer_display_name_trgm", "filer", "display_name"),
    ("ix_filer_manager_name_trgm", "filer", "manager_name"),
)

#: pg_trgm's functions of (text, text) the search calls, as operators and to
#: score: %, <%, %>, <<%, %>>, and the three it ranks with.
TRIGRAM_FUNCTIONS: tuple[str, ...] = (
    "similarity_op",
    "word_similarity_op",
    "word_similarity_commutator_op",
    "strict_word_similarity_op",
    "strict_word_similarity_commutator_op",
    "similarity",
    "word_similarity",
    "strict_word_similarity",
)
#: In units of cpu_operator_cost. See the docstring.
TRIGRAM_COST = 100


def upgrade() -> None:
    # 0017 created the extension. Again here, so this migration does not
    # depend on 0017's downgrade having left it behind.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in INDEXES:
        op.create_index(
            name,
            table,
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )
    for function in TRIGRAM_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {function}(text, text) COST {TRIGRAM_COST}")
    op.execute("ANALYZE security")
    op.execute("ANALYZE filer")


def downgrade() -> None:
    """Drops the three indexes and puts the extension's costs back. The search
    still works without them, by sequential scan, which on ``security`` is
    over the 50 ms budget."""
    for function in TRIGRAM_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {function}(text, text) COST 1")
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
