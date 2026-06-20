"""Database setup: engine, session factory, Base, and the get_db dependency
used by the endpoints.

The connection target comes from the ``DATABASE_URL`` environment variable so
the same code runs against local SQLite (default), the in-memory SQLite used by
the test suite, and PostgreSQL on Amazon RDS in production. Nothing about the
target is hardcoded — set ``DATABASE_URL`` to switch backends.

Examples::

    # Local dev (default when DATABASE_URL is unset)
    sqlite:///./nama.db

    # PostgreSQL on RDS, verifying the server's TLS certificate
    postgresql+psycopg://USER:PASSWORD@HOST:5432/nama?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-ca.pem
"""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./nama.db")
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# check_same_thread=False only applies to SQLite (it lets the connection be
# shared across FastAPI's threadpool). Postgres must not receive it.
_connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    # Cheaply checks a pooled connection is still alive before handing it out,
    # so connections dropped by RDS (failover, idle timeout) are recycled
    # transparently instead of surfacing as errors. No-op cost for SQLite.
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for ORM models."""


def get_db() -> Iterator[Session]:
    """Yield a request-scoped session and close it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
