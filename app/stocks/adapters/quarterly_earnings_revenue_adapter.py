"""Interface Adapter: the earnings endpoint's revenue overlay, served from the
quarterly-earnings slice.

Replaces the SEC EDGAR revenue provider: the quarterly-earnings slice already stores
each reported quarter's revenue (Yahoo via yfinance, behind the persistent DB cache
and the merge-preserving cron), so the legacy ``/stocks/{symbol}/earnings`` endpoint
reads the same rows instead of fetching and parsing a second vendor — one source of
truth for reported revenue.

It reads through the ``QuarterlyEarningsProvider`` port (wired in production as the
DB cache over yfinance), so a symbol never cached fills lazily on first view exactly
like the quarterly endpoint itself, and a populated one never leaves the DB. The
inner provider already speaks domain exceptions, so failures pass straight through —
the earnings use case treats the overlay as best-effort and just omits it.
"""

from datetime import date

from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.ports import RevenueHistoryProvider


class QuarterlyEarningsRevenueProvider(RevenueHistoryProvider):
    """Projects the quarterly-earnings timeline into the per-quarter revenue map."""

    def __init__(self, provider: QuarterlyEarningsProvider) -> None:
        self._provider = provider

    def get_quarterly_revenue(self, symbol: str) -> dict[date, float]:
        timeline = self._provider.get_quarterly_earnings(symbol)
        return {
            quarter.period_end: quarter.revenue_actual
            for quarter in timeline.quarters
            if quarter.period_end is not None and quarter.revenue_actual is not None
        }
