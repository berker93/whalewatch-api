"""notification feed: newest first, every row or the unread only

The notification centre lists a user's notifications newest first, paged by
``(created_at, id)``, and can be asked for the unread ones only. Two indexes
serve that: ``ix_notification_feed`` over every row, and
``ix_notification_unread``, the partial index 0042 made for the count,
rebuilt with ``id`` as its last key so the unread list reads in keyset order
off it with no sort.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-27 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNREAD = sa.text("read_at IS NULL")


def upgrade() -> None:
    op.create_index(
        "ix_notification_feed",
        "notification",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.drop_index("ix_notification_unread", "notification")
    op.create_index(
        "ix_notification_unread",
        "notification",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=UNREAD,
    )


def downgrade() -> None:
    op.drop_index("ix_notification_unread", "notification")
    op.create_index(
        "ix_notification_unread",
        "notification",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=UNREAD,
    )
    op.drop_index("ix_notification_feed", "notification")
