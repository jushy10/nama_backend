import logging
from datetime import date

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domains.research.rate_limit_quota.models import (
    increment_usage_if_below,
    insert_usage,
    usage_exists,
)
from app.domains.research.rate_limit_quota.repository import QuotaRepository

logger = logging.getLogger(__name__)


class DbQuotaRepository(QuotaRepository):
    """SQLAlchemy counter, one row per (pool, client, day). Writes are bounded by the
    quota itself, so a plain table suffices (no Redis) and the count survives deploys.
    Fails open on a DB fault — a cost guard, not a correctness rule; the slowapi
    per-IP limits stay the hard backstop."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def try_consume(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        try:
            if self._increment(pool, client_key, day, limit):
                return True
            if usage_exists(self._db, pool, client_key, day):
                return False  # at the limit — the only path that denies
            return self._insert_first_use(pool, client_key, day, limit)
        except SQLAlchemyError as exc:
            self._db.rollback()
            logger.warning("generation quota check failed (%s): %s", pool, exc)
            return True

    def _increment(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        # Atomic conditional UPDATE: two concurrent requests can't both spend the last one.
        if increment_usage_if_below(self._db, pool, client_key, day, limit):
            self._db.commit()
            return True
        self._db.rollback()
        return False

    def _insert_first_use(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        if limit < 1:
            return False
        try:
            insert_usage(self._db, pool, client_key, day)
            self._db.commit()
            return True
        except IntegrityError:
            # Lost the day's first-row race — retry the conditional increment.
            self._db.rollback()
            return self._increment(pool, client_key, day, limit)
