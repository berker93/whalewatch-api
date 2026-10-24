"""privacy: lawful bases, consent, and deletions that leave a record

``consent_record`` is the ledger of each user's position on each purpose the
site processes their data for, with the lawful basis it rests on: ``account``
on contract, ``product_email`` on legitimate interest, ``marketing_email`` on
consent. Rows are only ever added; the latest per purpose is where the user
stands.

``account_deletion`` records each deletion from request to purge, with no
foreign key to ``app_user`` and nothing about the person, so it survives the
purge it records.

``signup_request`` gains what the form agreed to (``terms_version``,
``privacy_version``, ``marketing_email``), for ``create-user`` to carry over.

Accounts that exist already were made by ``create-user`` or registered before
the form asked for terms. Each gets an ``account`` row with no terms version
(nothing was accepted) and a ``product_email`` row, granted, as legitimate
interest presumes until the user objects. No ``marketing_email`` row: nobody
was asked, so nobody consented. An account already marked deleted gets a
deletion row, due thirty days from its mark.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent_record",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("terms_version", sa.Text(), nullable=True),
        sa.Column("privacy_version", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_consent_record"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name="fk_consent_record_user_id_app_user",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(purpose = 'account' AND basis = 'contract') OR "
            "(purpose = 'product_email' AND basis = 'legitimate_interest') OR "
            "(purpose = 'marketing_email' AND basis = 'consent')",
            name="purpose_has_its_basis",
        ),
        sa.CheckConstraint(
            "source IN ('register', 'create_user', 'preferences')", name="source_is_known"
        ),
        sa.CheckConstraint("purpose <> 'account' OR granted", name="account_is_granted"),
    )
    op.create_index("ix_consent_record_user_id", "consent_record", ["user_id", "purpose", "id"])

    op.create_table(
        "account_deletion",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erased", JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_account_deletion"),
        sa.CheckConstraint("requested_by IN ('user', 'operator')", name="requested_by_is_known"),
        sa.CheckConstraint(
            "purged_at IS NULL OR restored_at IS NULL", name="purged_or_restored_not_both"
        ),
        sa.CheckConstraint("(purged_at IS NULL) = (erased IS NULL)", name="purge_says_what"),
    )
    op.create_index(
        "uq_account_deletion_user_id",
        "account_deletion",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("purged_at IS NULL AND restored_at IS NULL"),
    )

    op.add_column("signup_request", sa.Column("terms_version", sa.Text(), nullable=True))
    op.add_column("signup_request", sa.Column("privacy_version", sa.Text(), nullable=True))
    op.add_column(
        "signup_request",
        sa.Column("marketing_email", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.execute("""
        INSERT INTO consent_record (user_id, purpose, basis, granted, source, recorded_at)
        SELECT id, purpose, basis, true, 'create_user', created_at
        FROM app_user
        CROSS JOIN (VALUES
            ('account', 'contract'),
            ('product_email', 'legitimate_interest')
        ) AS purposes (purpose, basis)
        ORDER BY id, purpose
    """)
    op.execute("""
        INSERT INTO account_deletion (user_id, requested_by, requested_at, purge_after)
        SELECT id, 'operator', deleted_at, deleted_at + interval '30 days'
        FROM app_user
        WHERE deleted_at IS NOT NULL
    """)


def downgrade() -> None:
    op.drop_column("signup_request", "marketing_email")
    op.drop_column("signup_request", "privacy_version")
    op.drop_column("signup_request", "terms_version")
    op.drop_index("uq_account_deletion_user_id", "account_deletion")
    op.drop_table("account_deletion")
    op.drop_index("ix_consent_record_user_id", "consent_record")
    op.drop_table("consent_record")
