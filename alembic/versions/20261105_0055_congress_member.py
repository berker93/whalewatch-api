"""congress member: the registry, and which member each filed name is

``congress_member`` and ``congress_member_term``: everyone who has sat in
Congress since 2008, from the ``unitedstates/congress-legislators`` dataset,
and their terms. ``congress_filed_name``: a name as a chamber's index filed
it, and the member it is — matched exact or normalised by a run, or by a
person, never by trigram. ``congress_member_candidate``: who it might be,
waiting for a person. ``congress_filing.filed_name_id`` links each filing to
its name. See :mod:`app.db.models.congress_member`.

``admin_action`` gains ``member_matched`` and ``member_rejected``.

Every filing starts unlinked: ``seed-congress-members`` links and matches them.

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-05 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported: a migration says what the schema was when
# it ran, whatever the models say later.
ACTIONS_BEFORE = (
    "login",
    "logout",
    "override_set",
    "override_ended",
    "grandfathered",
    "ungrandfathered",
    "admin_created",
    "admin_disabled",
    "ptr_row_accepted",
    "ptr_row_rejected",
)
ACTIONS_AFTER = (*ACTIONS_BEFORE, "member_matched", "member_rejected")
CHAMBERS = ("house", "senate")
NAME_STATUSES = ("matched", "review", "unmatched")
NAME_METHODS = ("exact", "normalised", "manual")
CANDIDATE_STATUSES = ("pending", "accepted", "rejected", "superseded")
CANDIDATE_METHODS = ("exact", "normalised", "trigram")
AUTO_ACCEPT = 0.9


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "congress_member",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bioguide_id", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("middle_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("suffix", sa.Text(), nullable=True),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column(
            "other_last_names",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("chamber", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("district", sa.SmallInteger(), nullable=True),
        sa.Column("party", sa.Text(), nullable=True),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint("bioguide_id"),
        sa.CheckConstraint("bioguide_id ~ '^[A-Z][0-9]{6}$'", name="bioguide_id_is_an_id"),
        sa.CheckConstraint(_in("chamber", CHAMBERS), name="chamber_is_known"),
        sa.CheckConstraint("state ~ '^[A-Z]{2}$'", name="state_is_a_code"),
        sa.CheckConstraint(
            "chamber = 'house' OR district IS NULL", name="only_the_house_has_districts"
        ),
    )
    op.create_table(
        "congress_member_term",
        sa.Column(
            "member_id",
            sa.BigInteger(),
            sa.ForeignKey("congress_member.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chamber", sa.Text(), primary_key=True),
        sa.Column("start_date", sa.Date(), primary_key=True),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("district", sa.SmallInteger(), nullable=True),
        sa.Column("party", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("chamber", CHAMBERS), name="chamber_is_known"),
        sa.CheckConstraint("end_date >= start_date", name="ends_after_it_starts"),
        sa.CheckConstraint("state ~ '^[A-Z]{2}$'", name="state_is_a_code"),
        sa.CheckConstraint(
            "chamber = 'house' OR district IS NULL", name="only_the_house_has_districts"
        ),
    )
    op.create_table(
        "congress_filed_name",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("chamber", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "member_id",
            sa.BigInteger(),
            sa.ForeignKey("congress_member.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by",
            sa.BigInteger(),
            sa.ForeignKey("admin_user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.UniqueConstraint("chamber", "state", "name_key", postgresql_nulls_not_distinct=True),
        sa.CheckConstraint(_in("chamber", CHAMBERS), name="chamber_is_known"),
        sa.CheckConstraint(_in("status", NAME_STATUSES), name="status_is_known"),
        sa.CheckConstraint(
            "method IS NULL OR " + _in("method", NAME_METHODS), name="method_is_known"
        ),
        sa.CheckConstraint("name_key <> ''", name="name_key_is_not_empty"),
        sa.CheckConstraint(
            "(status = 'matched') = (member_id IS NOT NULL)", name="matched_names_a_member"
        ),
        sa.CheckConstraint("(member_id IS NULL) = (method IS NULL)", name="a_match_has_a_method"),
        sa.CheckConstraint(
            f"method IS NULL OR method = 'manual' OR confidence >= {AUTO_ACCEPT}",
            name="auto_match_is_confident",
        ),
        sa.CheckConstraint(
            "method <> 'manual' OR reviewed_at IS NOT NULL", name="manual_match_was_reviewed"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_is_a_fraction"
        ),
    )
    op.create_index(
        "ix_congress_filed_name_open",
        "congress_filed_name",
        ["status"],
        postgresql_where=sa.text("status <> 'matched'"),
    )
    op.create_index("ix_congress_filed_name_member_id", "congress_filed_name", ["member_id"])
    op.create_table(
        "congress_member_candidate",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "filed_name_id",
            sa.BigInteger(),
            sa.ForeignKey("congress_filed_name.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            sa.BigInteger(),
            sa.ForeignKey("congress_member.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("status", CANDIDATE_STATUSES), name="status_is_known"),
        sa.CheckConstraint(_in("method", CANDIDATE_METHODS), name="method_is_known"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_is_a_fraction"),
        sa.CheckConstraint(
            "(status = 'pending') = (closed_at IS NULL)", name="closed_when_not_pending"
        ),
    )
    op.create_index(
        "uq_congress_member_candidate_pending",
        "congress_member_candidate",
        ["filed_name_id", "member_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_congress_member_candidate_filed_name_id",
        "congress_member_candidate",
        ["filed_name_id"],
    )

    op.add_column(
        "congress_filing",
        sa.Column(
            "filed_name_id",
            sa.BigInteger(),
            sa.ForeignKey("congress_filed_name.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_congress_filing_filed_name_id", "congress_filing", ["filed_name_id"])

    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_AFTER))


def downgrade() -> None:
    """Loses the registry, every match and every review of one. A re-seed
    rebuilds the registry and the automatic matches; the reviews are gone.
    Refused while the admin log names a member review: the log is append-only,
    and the narrower CHECK would not hold."""
    op.drop_constraint("action_is_known", "admin_action", type_="check")
    op.create_check_constraint("action_is_known", "admin_action", _in("action", ACTIONS_BEFORE))
    op.drop_index("ix_congress_filing_filed_name_id", table_name="congress_filing")
    op.drop_column("congress_filing", "filed_name_id")
    op.drop_table("congress_member_candidate")
    op.drop_table("congress_filed_name")
    op.drop_table("congress_member_term")
    op.drop_table("congress_member")
