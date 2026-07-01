"""Application use cases for the annual-earnings slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of yfinance, HTTP, or SQLAlchemy:

- ``GetAnnualEarnings`` — the read path. Normalizes the symbol and returns the timeline
  through the ``AnnualEarningsProvider`` (wired in production as the DB cache over yfinance,
  so the read hits Yahoo only on a miss).
- ``SyncAnnualEarnings`` — the out-of-band refresh. Walks the already-stored rows
  stalest-first and renews them from the live provider, so users see current years without a
  request ever waiting on a vendor round-trip. Invoked by the cron endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.annual.repository import AnnualEarningsRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use case
    — so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetAnnualEarnings:
    """Use case: retrieve a stock's per-year earnings timeline by its symbol.

    Best-effort: an uncovered symbol yields an empty timeline rather than an error, so the
    endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: AnnualEarningsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> AnnualEarningsTimeline:
        return self._provider.get_annual_earnings(_normalize_symbol(symbol))


@dataclass(frozen=True)
class AnnualEarningsSyncReport:
    """The outcome of one refresh run: how many stocks were renewed, how many the provider
    couldn't serve this run (or returned empty for), and the per-run cap."""

    refreshed: int
    failed: int
    limit: int


class SyncAnnualEarnings:
    """Renew stored annual earnings from the live source, stalest stocks first."""

    # Default stocks per run; the caller (the cron endpoint) can override per invocation.
    # Kept modest so the sequential Yahoo calls stay gentle on its rate limits.
    DEFAULT_LIMIT = 200

    def __init__(
        self,
        provider: AnnualEarningsProvider,
        repository: AnnualEarningsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> AnnualEarningsSyncReport:
        """Refresh up to ``limit`` stalest stocks (default ``DEFAULT_LIMIT``), returning a
        summary. Never raises for a single symbol's failure — the run continues and the
        failure is counted, so one bad symbol doesn't abort the whole sweep."""
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        for target in self._repository.refresh_targets(capped):
            try:
                timeline = self._provider.get_annual_earnings(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped
                # coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result must not wipe the stored window — an empty upsert would
            # delete every year. Skip it and count a failure so the next run retries; the
            # stored rows keep serving in the meantime.
            if timeline.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, timeline)
            refreshed += 1
        return AnnualEarningsSyncReport(refreshed=refreshed, failed=failed, limit=capped)
