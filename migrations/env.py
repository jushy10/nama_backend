"""Alembic environment.

Reuses the application's ``DATABASE_URL`` and ``Base.metadata`` so migrations
always target the same database the app does and ``--autogenerate`` can diff
against the live ORM models. Importing ``app.models`` registers every model on
``Base.metadata`` (the import is what wires them up, even though it looks unused).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.db import DATABASE_URL, Base
import app.models  # noqa: F401  (registers models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and apply migrations."""
    # render_as_batch lets ALTER TABLE work on SQLite too (it rebuilds tables),
    # so the same migrations run locally and on Postgres/RDS.
    is_sqlite = DATABASE_URL.startswith("sqlite")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
