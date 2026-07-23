from datetime import date, datetime, timezone
from typing import Callable

from app.domains.research.rate_limit_quota.repository import QuotaRepository
from app.domains.shared.exceptions import QuotaExceeded


class ConsumeGenerationQuota:
    """Spend one metered AI generation from a client's daily budget for one pool.
    Other slices' use cases take this and call it right before their model call, so
    only a real generation pays — never a cache hit or a rejected symbol."""

    def __init__(
        self,
        repository: QuotaRepository,
        pool: str,
        daily_limit: int,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._repository = repository
        self._pool = pool
        self._daily_limit = daily_limit
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def execute(self, client_id: str | None) -> None:
        """Raises QuotaExceeded when the day's budget is spent. A missing client id
        is a no-op (non-HTTP callers: tests, crons)."""
        if client_id is None:
            return
        # The column is VARCHAR(64); a forged oversized header must not turn into a 500.
        client_key = client_id[:64]
        if not self._repository.try_consume(
            self._pool, client_key, self._today(), self._daily_limit
        ):
            raise QuotaExceeded()
