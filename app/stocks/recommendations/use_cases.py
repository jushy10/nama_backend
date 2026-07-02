"""Application use cases for the recommendations slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of yfinance, HTTP, or SQLAlchemy:

- ``GetStockRecommendations`` — the read path. Normalizes the symbol and returns the
  trends through the ``RecommendationProvider`` (wired in production as the DB cache over
  yfinance, so the read hits Yahoo only on a miss).
- ``SyncRecommendations`` — the out-of-band refresh. Walks the already-stored rows
  least-recently-refreshed first and renews them from the live provider, so users see the
  current month's split without a request ever waiting on a vendor round-trip. Invoked by
  the cron endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.recommendations.entities import AnalystRecommendations
from app.stocks.recommendations.ports import RecommendationProvider
from app.stocks.recommendations.repository import RecommendationsRepository


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use
    case — so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetStockRecommendations:
    """Use case: retrieve a stock's analyst recommendation trends by its symbol.

    Best-effort: a symbol no analyst covers yields an empty run rather than an error, so
    the endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: RecommendationProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> AnalystRecommendations:
        return self._provider.get_recommendations(_normalize_symbol(symbol))


@dataclass(frozen=True)
class RecommendationsSyncReport:
    """The outcome of one refresh run: how many stocks were renewed, how many the
    provider couldn't serve this run (or returned empty for), and the per-run cap."""

    refreshed: int
    failed: int
    limit: int


class SyncRecommendations:
    """Renew stored recommendation trends from the live source, stalest stocks first."""

    # Default stocks per run; the caller (the cron endpoint) can override per invocation.
    # Kept modest so the sequential Yahoo calls stay gentle on its rate limits.
    DEFAULT_LIMIT = 200

    def __init__(
        self,
        provider: RecommendationProvider,
        repository: RecommendationsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> RecommendationsSyncReport:
        """Refresh up to ``limit`` stalest stocks (default ``DEFAULT_LIMIT``), returning
        a summary. Never raises for a single symbol's failure — the run continues and the
        failure is counted, so one bad symbol doesn't abort the whole sweep."""
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        for target in self._repository.refresh_targets(capped):
            try:
                recommendations = self._provider.get_recommendations(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped
                # coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result has nothing to merge (the upsert would write no rows,
            # so the stock's refresh stamp would never advance and it would jam the front
            # of the stale queue). Skip it and count a failure so the next run retries;
            # the stored months keep serving in the meantime.
            if recommendations.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, recommendations)
            refreshed += 1
        return RecommendationsSyncReport(refreshed=refreshed, failed=failed, limit=capped)
