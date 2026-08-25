"""Alembic environment, wired to the application's own Settings.

The URL is not in alembic.ini. It is assembled here from the same
:class:`~app.core.config.Settings` object the app connects with, so there is
exactly one definition of "which database" and rotating a password touches one
place. A DSN in the ini file would also mean a committed password.

The one caller that does not want that URL is the integration test suite, whose
database is a container that did not exist when Settings was built. It hands
this module an open connection through ``config.attributes["connection"]`` and
everything below defers to it — so the schema those tests run against is built
by these migrations, in this order, rather than by ``create_all``.

Autogenerate is configured to compare types and server defaults, which the
defaults do not. Without them a column changed from ``varchar(20)`` to
``varchar(40)``, or a ``server_default`` added to an existing column, produces
an empty migration — Alembic reports "no changes detected" and the schema
silently diverges from the models. With them on, autogenerate gets noisier
(Postgres normalises defaults, so it will occasionally propose a no-op change);
that noise is the correct trade, because every migration in this project is
read and hand-edited before it is committed.

Which is the other thing to say here: autogenerate is a draft. It cannot see
generated columns, native enums beyond CREATE TYPE, partitioning, materialised
views, or anything expressed in raw DDL, and it will happily emit a
``drop_table`` for objects it does not understand. Read what it wrote, then
write the downgrade yourself.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.models.base import Base

# Imported for the side effect of populating Base.metadata: a model class only
# registers when its module is executed, so a model package missing from
# app/db/models/__init__.py is invisible to autogenerate — and worse, an
# existing table for it looks like a table to drop.
import app.db.models  # noqa: F401  # isort: skip

config = context.config

# A caller running migrations in-process — the integration test suite against
# its throwaway container — puts a live, already-open Connection here and calls
# ``command.upgrade``. This is Alembic's documented way to share a connection
# with programmatic callers, and it is what makes "the test database is built by
# the same migrations production runs" true rather than aspirational.
#
# Two things follow from it, both handled below: the URL must not be rebuilt
# from Settings (the caller's connection already decides which database), and
# nothing here may open a second one.
injected_connection: Connection | None = config.attributes.get("connection")

# fileConfig() calls logging.config.fileConfig, which disables every logger it
# does not name — including this application's. Harmless for the CLI, where
# alembic owns the process, and destructive in-process, where it would silently
# switch off the app's logging for the rest of the test session. So the caller
# that supplies its own connection also owns its own logging.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

if injected_connection is None:
    # `database_url` percent-encodes the credentials, so a password containing
    # "@" arrives here as "%40" — and configparser reads "%4" as an
    # interpolation and raises. set_main_option passes the value straight to
    # ConfigParser.set without escaping, so doubling the percent signs is
    # required, not defensive. The value is un-escaped again on the way back out
    # through get_section() below.
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    ``alembic upgrade head --sql`` is how a migration gets reviewed as DDL, or
    handed to a DBA who will apply it during a window. No connection is opened,
    so this works with Postgres unreachable.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # See the module docstring: off by default, and off is how schemas drift.
        compare_type=True,
        compare_server_default=True,
    )

    # Postgres has transactional DDL, so the whole upgrade — every revision in
    # the chain plus the alembic_version bump — commits or rolls back as one.
    # A failure halfway through `upgrade head` leaves the database on the last
    # good revision rather than on a half-applied one.
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open a connection and run the migrations on it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool, not the app's QueuePool: this process opens one connection,
        # runs DDL, and exits. Pooling would only leave a connection behind for
        # Postgres to reap.
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        # In a finally so a failed migration still closes its connection —
        # otherwise a CI job that fails here holds an idle connection, and any
        # lock it took, until the socket times out.
        await connectable.dispose()


def run_migrations_online() -> None:
    """Apply migrations against a live database.

    Two routes in, and the difference is who owns the connection. From the CLI
    there is no loop running, so this one is created and disposed here. From an
    in-process caller the connection was opened on a loop that is *already*
    running, and ``asyncio.run`` inside a running loop raises — so the injected
    connection is used directly and synchronously. It arrives as a sync-facade
    ``Connection`` (via ``AsyncConnection.run_sync``), which is exactly what
    ``context.configure`` wants either way.
    """
    if injected_connection is not None:
        do_run_migrations(injected_connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
