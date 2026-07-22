import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import Base

# Import the models so their tables register on Base.metadata (needed for
# `alembic revision --autogenerate`). Imported for the side effect only.
from app.domains.listings.anchor import models as stocks_models  # noqa: F401
from app.domains.financials.earnings.quarterly import models as quarterly_earnings_models  # noqa: F401
from app.domains.financials.earnings.annual import models as annual_earnings_models  # noqa: F401
from app.domains.coverage.recommendations import models as recommendations_models  # noqa: F401
from app.domains.coverage.news import models as news_models  # noqa: F401
from app.domains.etfs import models as etf_models  # noqa: F401
from app.domains.research.analysis import models as analysis_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read the URL at run time, mirroring app/db.py's default, rather than reusing
# the app's import-time engine — so the CLI and the tests both honour whatever
# DATABASE_URL is set when migrations actually run.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./nama.db")
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER in place; batch mode rebuilds tables instead.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
