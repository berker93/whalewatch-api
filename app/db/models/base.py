"""The declarative base every ORM model inherits from.

Nothing here is cosmetic. The naming convention below is the reason a
constraint dropped in migration 0042 can be named at all: without it,
SQLAlchemy leaves unnamed constraints to Postgres, which invents names from its
own counters (``users_email_key``, ``users_email_key1``) that depend on the
order objects happened to be created in. Two environments built by different
routes — one by ``create_all`` in a test, one by twenty migrations in
production — end up with the same schema under different constraint names, and
``op.drop_constraint("uq_users_email")`` then works locally and fails in prod.

The convention makes the name a pure function of the table and columns, so
Alembic autogenerate emits the same name everywhere and a later migration can
refer to it by hand.

It has to be attached to the MetaData *before* any model is defined, which is
why it lives on the base class rather than being applied later.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Postgres identifiers truncate at 63 characters. These templates can exceed
# that on a long fk (table + column + referred table); if that happens, name the
# constraint explicitly at the call site rather than letting Postgres truncate.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all models.

    Alembic's ``target_metadata`` is ``Base.metadata``, so a model that does not
    inherit from this class is invisible to autogenerate — it will not be
    created, and worse, autogenerate will propose *dropping* its table if one
    exists. Every model in :mod:`app.db.models` inherits from here.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
