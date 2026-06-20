"""SQLite database setup: engine, session factory, Base, and the get_db
dependency used by the endpoints.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = "sqlite:///./nama.db"

# check_same_thread=False lets the SQLite connection be shared across FastAPI's
# threadpool. The file is created on first connect.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
