"""Database setup: engine, session factory, Base, and the get_db dependency.

The target comes from the DATABASE_URL environment variable, so the app runs on
local SQLite by default and on PostgreSQL (RDS) when that variable is set, e.g.

    postgresql+psycopg://user:pass@host:5432/nama?sslmode=require

Tests don't set it, so they stay on fast in-memory SQLite.
"""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./nama.db")

# check_same_thread only applies to SQLite (it lets the connection be shared
# across FastAPI's threadpool). Postgres must not receive it.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,  # recycle connections dropped by RDS (failover/idle timeout)
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
