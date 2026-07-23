import logging
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domains.research.quota.models import GenerationUsageRecord
from app.domains.shared.interfaces import GenerationQuotaAdapter

logger = logging.getLogger(__name__)


class GenerationQuotaAdapterImpl(GenerationQuotaAdapter):
    """DB-backed daily counter, one row per (pool, client, UTC day). Writes are bounded
    by the quota itself, so a plain table suffices (no Redis) and the count survives
    deploys. Fails open on a DB fault — a cost guard, not a correctness rule; the
    slowapi per-IP limits stay the hard backstop."""

    def __init__(
        self,
        db: Session,
        pool: str,
        daily_limit: int,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._db = db
        self._pool = pool
        self._daily_limit = daily_limit
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def try_consume(self, client_id: str) -> bool:
        # The column is VARCHAR(64); a forged oversized header must not turn into a 500.
        client_key = client_id[:64]
        try:
            if self._increment_if_under_limit(client_key):
                return True
            if self._row_exists(client_key):
                return False  # at the limit — the only path that denies
            return self._insert_first_use(client_key)
        except SQLAlchemyError as exc:
            self._db.rollback()
            logger.warning("generation quota check failed (%s): %s", self._pool, exc)
            return True

    def _increment_if_under_limit(self, client_key: str) -> bool:
        # Atomic conditional UPDATE: two concurrent requests can't both spend the last one.
        result = self._db.execute(
            update(GenerationUsageRecord)
            .where(
                GenerationUsageRecord.pool == self._pool,
                GenerationUsageRecord.client_key == client_key,
                GenerationUsageRecord.usage_date == self._today(),
                GenerationUsageRecord.count < self._daily_limit,
            )
            .values(count=GenerationUsageRecord.count + 1)
        )
        if result.rowcount:
            self._db.commit()
            return True
        self._db.rollback()
        return False

    def _row_exists(self, client_key: str) -> bool:
        return (
            self._db.execute(
                select(GenerationUsageRecord.id).where(
                    GenerationUsageRecord.pool == self._pool,
                    GenerationUsageRecord.client_key == client_key,
                    GenerationUsageRecord.usage_date == self._today(),
                )
            ).first()
            is not None
        )

    def _insert_first_use(self, client_key: str) -> bool:
        if self._daily_limit < 1:
            return False
        try:
            self._db.add(
                GenerationUsageRecord(
                    pool=self._pool,
                    client_key=client_key,
                    usage_date=self._today(),
                    count=1,
                )
            )
            self._db.commit()
            return True
        except IntegrityError:
            # Lost the day's first-row race — retry the conditional increment.
            self._db.rollback()
            return self._increment_if_under_limit(client_key)
