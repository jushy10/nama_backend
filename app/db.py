import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./nama.db")

# check_same_thread only applies to SQLite (it lets the connection be shared
# across FastAPI's threadpool). Postgres must not receive it.
_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

_engine_kwargs: dict = {
    "connect_args": _connect_args,
    "pool_pre_ping": True,  # recycle connections dropped by RDS (failover/idle timeout)
}

# Connection-pool sizing (Postgres/RDS only — SQLite uses its own pool and would
# reject these). Each sync `def` endpoint runs in Starlette's threadpool and
# checks out one connection for the length of its DB work, so the pool ceiling
# (pool_size + max_overflow) is the app's concurrent-DB-request ceiling per task;
# beyond it, requests block up to pool_timeout waiting for a connection. Defaults
# give 20 per task (up from SQLAlchemy's 15). Keep the total across all tasks —
# autoscaling_max_capacity API tasks + the on-demand sync task, each with its own
# pool — under RDS max_connections (~112 on db.t4g.micro). Env-tunable so the
# ceiling can move without a code change.
if not _is_sqlite:
    _engine_kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "10"))
    _engine_kwargs["max_overflow"] = int(os.environ.get("DB_MAX_OVERFLOW", "10"))
    _engine_kwargs["pool_timeout"] = int(os.environ.get("DB_POOL_TIMEOUT", "30"))

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
