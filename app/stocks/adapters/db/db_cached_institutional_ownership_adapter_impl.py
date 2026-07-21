import logging

from app.stocks.company.institutional_ownership.entities import InstitutionalOwnership
from app.stocks.company.institutional_ownership.interfaces import InstitutionalOwnershipAdapter
from app.stocks.company.institutional_ownership.interfaces import (
    InstitutionalOwnershipRepositoryAdapter,
)

logger = logging.getLogger(__name__)


class InstitutionalOwnershipAdapterImpl(InstitutionalOwnershipAdapter):
    def __init__(
        self,
        inner: InstitutionalOwnershipAdapter,
        repo: InstitutionalOwnershipRepositoryAdapter,
    ) -> None:
        self._inner = inner
        self._repo = repo

    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        stored = self._safe_get(symbol)
        if stored is not None:
            return stored  # a populated symbol is served straight from the DB, any age
        # Miss: nothing stored → fetch from the live source, store it, and return it. A live failure
        # here has nothing to fall back on, so it propagates (→ 502).
        ownership = self._inner.get_institutional_ownership(symbol)
        if not ownership.is_empty:
            self._safe_upsert(symbol, ownership)
        return ownership

    def _safe_get(self, symbol: str) -> InstitutionalOwnership | None:
        # A cache read must never break the read: on any error, treat it as a miss and fall through
        # to the live source.
        try:
            return self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "institutional-ownership cache read failed for %s", symbol, exc_info=True
            )
            return None

    def _safe_upsert(self, symbol: str, ownership: InstitutionalOwnership) -> None:
        # Caching is best-effort; a write failure must not fail the request the caller already has a
        # good answer for. (Name comes from the sync job, not this feed, so it's left untouched.)
        try:
            self._repo.upsert(symbol, None, ownership)
        except Exception:  # noqa: BLE001 — cache resilience, not error handling
            logger.warning(
                "institutional-ownership cache write failed for %s", symbol, exc_info=True
            )
