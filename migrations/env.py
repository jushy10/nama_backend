"""Alembic environment.

Reuses the application's ``engine`` and ``Base.metadata`` so migrations always
target the same database the app does and ``--autogenerate`` can diff against the
live ORM models. Importing ``app.models`` registers every model on
``Base.metadata`` (the import is what wires them up, even though it looks unused).
"""

from logging.config import fileConfig

from alembic import context

from app.db import IS_SQLITE, Base, engine
import app.models  # noqa: F401  (registers models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Connect to the database and apply migrations."""
    # render_as_batch lets ALTER TABLE work on SQLite too (it rebuilds tables),
    # so the same migrations run locally and on Postgres/RDS.
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=IS_SQLITE,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
