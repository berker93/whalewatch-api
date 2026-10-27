"""paddle billing: subscriptions kept in step with Paddle's webhooks, and dunning

``subscription`` gains the Paddle subscription and customer it mirrors, the
``occurred_at`` of the last event applied to it (events arrive in any order,
and an older one never overwrites a newer), and a failed renewal's dunning:
since when it is past due, when its grace ends, and the last email sent about
it. ``paddle_event`` records every webhook event once, by Paddle's
``event_id``: a repeat finds its row and changes nothing. It keeps no payload,
so no customer's details outlive their account in it.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from app.db.models: a migration says what
# the schema was when it ran, whatever the models say later.
DUNNING_STEPS = ("payment_failed", "grace_ending", "downgraded")
OUTCOMES = ("applied", "stale", "ignored", "unmatched")


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.add_column("subscription", sa.Column("paddle_subscription_id", sa.Text(), nullable=True))
    op.add_column("subscription", sa.Column("paddle_customer_id", sa.Text(), nullable=True))
    op.add_column(
        "subscription", sa.Column("paddle_event_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscription", sa.Column("past_due_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "subscription", sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("subscription", sa.Column("dunning_notified", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_subscription_paddle_subscription_id", "subscription", ["paddle_subscription_id"]
    )
    op.create_index("ix_subscription_paddle_customer_id", "subscription", ["paddle_customer_id"])
    op.create_check_constraint(
        "dunning_step_is_known",
        "subscription",
        f"dunning_notified IS NULL OR dunning_notified IN ({_quoted(DUNNING_STEPS)})",
    )
    op.create_check_constraint(
        "grace_has_a_start",
        "subscription",
        "(past_due_since IS NULL) = (grace_ends_at IS NULL)",
    )

    op.create_table(
        "paddle_event",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_paddle_event"),
        sa.CheckConstraint(f"outcome IN ({_quoted(OUTCOMES)})", name="outcome_is_known"),
    )
    op.create_index("ix_paddle_event_entity_id", "paddle_event", ["entity_id"])


def downgrade() -> None:
    op.drop_index("ix_paddle_event_entity_id", table_name="paddle_event")
    op.drop_table("paddle_event")
    op.drop_constraint("grace_has_a_start", "subscription", type_="check")
    op.drop_constraint("dunning_step_is_known", "subscription", type_="check")
    op.drop_index("ix_subscription_paddle_customer_id", table_name="subscription")
    op.drop_constraint("uq_subscription_paddle_subscription_id", "subscription", type_="unique")
    for column in (
        "dunning_notified",
        "grace_ends_at",
        "past_due_since",
        "paddle_event_at",
        "paddle_customer_id",
        "paddle_subscription_id",
    ):
        op.drop_column("subscription", column)
