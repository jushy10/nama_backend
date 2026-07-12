"""Interface Adapter: a read-through database cache in front of any InstitutionalOwnershipProvider.

The read path calls the database first; only on a **miss** (no stored holder rows for the symbol)
does it hit Yahoo, store the result, and return it. A symbol that already has rows is always served
straight from the DB — the read never re-fetches based on age. Keeping stored rows current is
entirely the out-of-band cron's job (``SyncInstitutionalOwnership``), which merges fresh holdings in
on its schedule. This keeps the endpoint off Yahoo, which rate-limits and blocks data-centre IPs.

It implements ``InstitutionalOwnershipProvider``, so it slots into the wiring exactly where the bare
yfinance provider would, with the use case none the wiser.

Resilience mirrors the news cache: a cache *read* failure degrades to a miss (falls through to the
live source), a cache *write* failure is swallowed (the caller still gets the fresh fetch), and an
empty live result is not stored (a symbol with no institutional coverage re-checks the live source
on its next view rather than being cached as empty).
"""

import logging

from app.stocks.institutional_ownership.entities import InstitutionalOwnership
from app.stocks.institutional_ownership.ports import InstitutionalOwnershipProvider
from app.stocks.institutional_ownership.repository import (
    InstitutionalOwnershipRepository,
)

logger = logging.getLogger(__name__)


class DbCachedInstitutionalOwnershipProvider(InstitutionalOwnershipProvider):
    """A read-through DB cache: serve stored rows, else fetch from the inner provider and store."""

    def __init__(
        self,
        inner: InstitutionalOwnershipProvider,
        repo: InstitutionalOwnershipRepository,
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
