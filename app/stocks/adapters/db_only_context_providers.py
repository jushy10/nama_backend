"""Interface Adapters: DB-only (no live fall-through) views of the earnings and
recommendations caches, for the AI-analysis context.

The analysis endpoint layers the quarterly/annual earnings and the recommendation
trends onto its prompt as **best-effort context**. Its own read endpoints reach
that data through a read-through cache that, on a miss, fetches live from Yahoo,
stores, and returns — the right behaviour there, where the data *is* the response.
In the analysis path it is the wrong trade: a synchronous Yahoo fetch (rate-limited,
paced, and blocked from data-centre IPs) can add several seconds to a request for a
symbol the cron hasn't populated yet — all to enrich context the analysis is happy
to omit.

These adapters wrap the same persistence repositories but read **DB-only**: a stored
timeline is served, a miss yields an *empty* one (never a live fetch), and a
cache-read failure degrades to empty too. They implement the very same provider
ports the read-through caches do, so they slot into the analysis wiring unchanged —
the use case still sees a provider, just one that never blocks on Yahoo. Keeping the
caches current stays entirely the crons' job (the same division of labour the
read-through caches already rely on).
"""

import logging

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.annual.ports import AnnualEarningsProvider
from app.stocks.earnings.annual.repository import AnnualEarningsRepository
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.recommendations.entities import AnalystRecommendations
from app.stocks.recommendations.ports import RecommendationProvider
from app.stocks.recommendations.repository import RecommendationsRepository

logger = logging.getLogger(__name__)


class DbOnlyQuarterlyEarningsProvider(QuarterlyEarningsProvider):
    """Serve the stored quarterly timeline; a miss (or read error) yields empty."""

    def __init__(self, repo: QuarterlyEarningsRepository) -> None:
        self._repo = repo

    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning(
                "quarterly cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else QuarterlyEarningsTimeline(symbol, ())


class DbOnlyAnnualEarningsProvider(AnnualEarningsProvider):
    """Serve the stored annual timeline; a miss (or read error) yields empty."""

    def __init__(self, repo: AnnualEarningsRepository) -> None:
        self._repo = repo

    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning("annual cache read failed for %s", symbol, exc_info=True)
            stored = None
        return stored if stored is not None else AnnualEarningsTimeline(symbol, ())


class DbOnlyRecommendationsProvider(RecommendationProvider):
    """Serve the stored recommendation run; a miss (or read error) yields empty."""

    def __init__(self, repo: RecommendationsRepository) -> None:
        self._repo = repo

    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        try:
            stored = self._repo.get(symbol)
        except Exception:  # noqa: BLE001 — best-effort context, never sink the analysis
            logger.warning(
                "recommendations cache read failed for %s", symbol, exc_info=True
            )
            stored = None
        return stored if stored is not None else AnalystRecommendations(symbol, ())