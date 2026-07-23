import logging
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domains.research.rate_limit_quota.interfaces import QuotaRepositoryAdapter
from app.domains.research.rate_limit_quota.models import GenerationUsageRecord

logger = logging.getLogger(__name__)


class QuotaRepositoryAdapterImpl(QuotaRepositoryAdapter):
    """SQLAlchemy counter, one row per (pool, client, day). Writes are bounded by the
    quota itself, so a plain table suffices (no Redis) and the count survives deploys.
    Fails open on a DB fault — a cost guard, not a correctness rule; the slowapi
    per-IP limits stay the hard backstop."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def try_consume(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        try:
            if self._increment_if_under_limit(pool, client_key, day, limit):
                return True
            if self._row_exists(pool, client_key, day):
                return False  # at the limit — the only path that denies
            return self._insert_first_use(pool, client_key, day, limit)
        except SQLAlchemyError as exc:
            self._db.rollback()
            logger.warning("generation quota check failed (%s): %s", pool, exc)
            return True

    def _increment_if_under_limit(
        self, pool: str, client_key: str, day: date, limit: int
    ) -> bool:
        # Atomic conditional UPDATE: two concurrent requests can't both spend the last one.
        result = self._db.execute(
            update(GenerationUsageRecord)
            .where(
                GenerationUsageRecord.pool == pool,
                GenerationUsageRecord.client_key == client_key,
                GenerationUsageRecord.usage_date == day,
                GenerationUsageRecord.count < limit,
            )
            .values(count=GenerationUsageRecord.count + 1)
        )
        if result.rowcount:
            self._db.commit()
            return True
        self._db.rollback()
        return False

    def _row_exists(self, pool: str, client_key: str, day: date) -> bool:
        return (
            self._db.execute(
                select(GenerationUsageRecord.id).where(
                    GenerationUsageRecord.pool == pool,
                    GenerationUsageRecord.client_key == client_key,
                    GenerationUsageRecord.usage_date == day,
                )
            ).first()
            is not None
        )

    def _insert_first_use(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        if limit < 1:
            return False
        try:
            self._db.add(
                GenerationUsageRecord(
                    pool=pool, client_key=client_key, usage_date=day, count=1
                )
            )
            self._db.commit()
            return True
        except IntegrityError:
            # Lost the day's first-row race — retry the conditional increment.
            self._db.rollback()
            return self._increment_if_under_limit(pool, client_key, day, limit)
